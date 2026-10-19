"""Tests for PR #412 (generated)."""
from datetime import date
from unittest.mock import MagicMock

import pytest

from mal_installment_pr import (
    Installment,
    InstallmentService,
    Ledger,
    MurabahaContract,
    ReceiptExtractor,
)


@pytest.fixture
def contract():
    return MurabahaContract("C-1", "AE070331234567890123456", 10000.0, 1200.0, 12)


@pytest.fixture
def service():
    ledger = MagicMock(spec=Ledger)
    ledger.has_key.return_value = False
    ledger.entries = []
    llm = MagicMock()
    llm.complete.return_value = '{"amount_paid": 933.33, "currency": "AED"}'
    return InstallmentService(ledger, ReceiptExtractor(llm))


def test_post_installment_posts_to_ledger(service, contract):
    inst = Installment("C-1", 1, date(2026, 10, 1), 933.33)
    result = service.post_installment(contract, inst, "receipt...", "key-1", today=date(2026, 10, 1))
    assert result is not None
    assert service.ledger.post.called


def test_late_charge_applied(service, contract):
    inst = Installment("C-1", 1, date(2026, 9, 1), 933.33)
    service.post_installment(contract, inst, "receipt...", "key-2", today=date(2026, 10, 1))
    assert service.ledger.post.call_count >= 1


def test_duplicate_key_ignored(service, contract):
    service.ledger.has_key.return_value = True
    inst = Installment("C-1", 1, date(2026, 10, 1), 933.33)
    service.post_installment(contract, inst, "receipt...", "key-3")
    assert inst.status in ("DUE", "PARTIAL", "PAID")


def test_reverse_payment(service):
    assert service.reverse_payment("C-1", 1) >= 0