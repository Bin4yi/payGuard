"""SPEC.md Section 13 'Numeric parsing': ambiguous numeric/shape input must
be rejected by schema validation before it ever reaches gateway logic."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from services.shared.normalize import NormalizationError, reject_duplicate_keys
from services.shared.schemas import AMOUNT_CEILING_MINOR, PaymentRequest
from tests.conftest import CURRENCY, DEBIT_ACCOUNT, DESTINATION_ID, OBLIGATION_ID, PAYMENT_REFERENCE


def _base(**overrides) -> dict:
    base = dict(
        job_id="job-01",
        mandate_id="mandate-01",
        obligation_id=OBLIGATION_ID,
        debit_account_id=DEBIT_ACCOUNT,
        destination_account_id=DESTINATION_ID,
        destination_version=1,
        amount_minor=5_000_000,
        currency=CURRENCY,
        payment_reference=PAYMENT_REFERENCE,
    )
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "bad_amount",
    [-1, 0, -5_000_000, 5_000_000.0, "5000000", True, False, AMOUNT_CEILING_MINOR + 1],
    ids=["negative", "zero", "negative_large", "float", "string", "bool_true", "bool_false", "over_ceiling"],
)
def test_rejects_ambiguous_amount(bad_amount):
    with pytest.raises(ValidationError):
        PaymentRequest(**_base(amount_minor=bad_amount))


def test_accepts_valid_amount_at_ceiling():
    req = PaymentRequest(**_base(amount_minor=AMOUNT_CEILING_MINOR))
    assert req.amount_minor == AMOUNT_CEILING_MINOR


def test_rejects_unknown_field():
    with pytest.raises(ValidationError):
        PaymentRequest(**_base(idempotency_key="sneaky-body-field"))


def test_rejects_unknown_top_level_field_generic():
    with pytest.raises(ValidationError):
        PaymentRequest(**_base(extra_field="should_not_be_silently_ignored"))


def test_rejects_action_override_attempt():
    """action is fixed by the endpoint, never a client-controlled field."""
    with pytest.raises(ValidationError):
        PaymentRequest(**_base(action="rent.pay.but.bigger"))


def test_rejects_destination_version_zero_or_negative():
    with pytest.raises(ValidationError):
        PaymentRequest(**_base(destination_version=0))
    with pytest.raises(ValidationError):
        PaymentRequest(**_base(destination_version=-1))


def test_rejects_non_ascii_reference():
    with pytest.raises(ValidationError):
        PaymentRequest(**_base(payment_reference="RENT-2026-09-–01"))


def test_rejects_duplicate_json_keys_in_raw_body():
    raw = '{"amount_minor": 5000000, "amount_minor": 999999999}'
    with pytest.raises(NormalizationError):
        reject_duplicate_keys(raw)
