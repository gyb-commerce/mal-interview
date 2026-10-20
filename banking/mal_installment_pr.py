"""
PR #412 — Murabaha installment posting + receipt auto-matching

Summary
-------
Adds `InstallmentService` which posts a customer's installment payment against
a Murabaha financing contract, applies the late-payment charge when the
installment is overdue, and auto-extracts the paid amount from the uploaded
receipt using the LLM so Ops no longer has to key it in by hand.

Also adds `reverse_payment` for the Ops "undo" button and a reschedule helper
for customers who ask to extend the tenor.

Testing
-------
All tests pass ✅ (see test_installments.py). Coverage 100% on the new module.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

log = logging.getLogger("mal.installments")

LATE_RATE_PER_DAY = 0.005          # 0.5 % of outstanding per day overdue
BANK_INCOME_ACCOUNT = "4000-INCOME"
CUSTOMER_RECEIVABLE = "1200-RECV"
SETTLEMENT_ACCOUNT = "1010-SETTLE"


# ----------------------------------------------------------------------------
# Domain
# ----------------------------------------------------------------------------

@dataclass
class MurabahaContract:
    contract_id: str
    customer_iban: str
    cost_price: float
    profit: float                 # agreed at signing
    tenor_months: int
    installments_paid: int = 0

    @property
    def sale_price(self) -> float:
        return self.cost_price + self.profit

    @property
    def installment_amount(self) -> float:
        return round(self.sale_price / self.tenor_months, 2)

    @property
    def outstanding(self) -> float:
        return self.sale_price - self.installments_paid * self.installment_amount # The brackets should be there to follow


@dataclass
class Installment:
    contract_id: str
    number: int
    due_date: date
    due_amount: float
    status: str = "DUE"           # DUE | PARTIAL | PAID    # There should be a method to validate the ENUMS


@dataclass
class LedgerEntry:
    entry_id: int
    contract_id: str
    debit_account: str
    credit_account: str
    amount: float
    memo: str
    idempotency_key: Optional[str] = None
    posted_at: datetime = field(default_factory=datetime.utcnow)


class Ledger:
    """In-memory double-entry ledger (swap for the DB-backed one in prod)."""

    def __init__(self):
        self.entries: list[LedgerEntry] = []
        self._next_id = 1

    def has_key(self, key: str) -> bool:
        return any(e.idempotency_key == key for e in self.entries)

    def post(self, contract_id, debit, credit, amount, memo, key=None) -> LedgerEntry:
        entry = LedgerEntry(self._next_id, contract_id, debit, credit, amount, memo, key)
        self.entries.append(entry)
        self._next_id += 1
        return entry


# ----------------------------------------------------------------------------
# Receipt extraction (LLM)
# ----------------------------------------------------------------------------

class ReceiptExtractor:
    """Pulls the paid amount out of the receipt the customer uploaded."""

    def __init__(self, llm_client):
        self.llm = llm_client

    def extract(self, receipt_text: str) -> dict:
        prompt = (
            "You are a bank operations assistant. Extract the payment details from "
            "the receipt below and reply with JSON {amount_paid, currency, paid_on}.\n\n"
            f"RECEIPT:\n{receipt_text}"
        )
        raw = self.llm.complete(prompt)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.warning("extractor returned non-JSON, using empty result")
            return {}


# ----------------------------------------------------------------------------
# Service
# ----------------------------------------------------------------------------

class InstallmentService:

    def __init__(self, ledger: Ledger, extractor: ReceiptExtractor):
        self.ledger = ledger
        self.extractor = extractor

    def post_installment(
        self,
        contract: MurabahaContract,
        installment: Installment,
        receipt_text: str,
        idempotency_key: str,
        today: Optional[date] = None,
    ) -> Installment:
        today = today or date.today()

        if self.ledger.has_key(idempotency_key):
            log.info("duplicate request %s ignored", idempotency_key)
            return installment

        data = self.extractor.extract(receipt_text)
        amount_paid = float(data.get("amount_paid", 0.0))
        log.info(
            "posting %s for %s from IBAN %s (receipt: %s)",
            amount_paid, contract.contract_id, contract.customer_iban, data,
        )

        # 1. customer payment
        self.ledger.post(
            contract.contract_id, SETTLEMENT_ACCOUNT, CUSTOMER_RECEIVABLE,
            amount_paid, f"Installment {installment.number}", key=idempotency_key,
        )

        # 2. late charge, if overdue
        days_late = (today - installment.due_date).days
        if days_late > 0:
            charge = 0.0
            balance = contract.outstanding
            for _ in range(days_late):
                daily = round(balance * LATE_RATE_PER_DAY, 2)
                charge += daily
                balance += daily
            self.ledger.post(
                contract.contract_id, CUSTOMER_RECEIVABLE, BANK_INCOME_ACCOUNT,
                round(charge, 2), f"Late payment charge, {days_late} days",
            )

        # 3. update installment
        if amount_paid >= installment.due_amount:
            installment.status = "PAID"
            contract.installments_paid += 1
        else:
            installment.status = "PARTIAL"
        return installment

    def reverse_payment(self, contract_id: str, installment_number: int) -> int:
        """Ops 'undo': removes the entries so the books match the bank again."""
        memo = f"Installment {installment_number}"
        before = len(self.ledger.entries)
        self.ledger.entries = [
            e for e in self.ledger.entries
            if not (e.contract_id == contract_id and e.memo == memo)
        ]
        return before - len(self.ledger.entries)

    def reschedule(self, contract: MurabahaContract, extra_months: int) -> MurabahaContract:
        """Extend the tenor at the customer's request; profit scales with the new term."""
        contract.profit += contract.outstanding * 0.01 * extra_months
        contract.tenor_months += extra_months
        return contract
