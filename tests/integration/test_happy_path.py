"""End-to-end: an authorized request pays exactly once (Phase 1 exit evidence)."""

from __future__ import annotations

from services.gateway.enforcement import execute_payment
from services.shared.schemas import PaymentRequest
from tests.conftest import (
    AMOUNT_MINOR,
    CURRENCY,
    DEBIT_ACCOUNT,
    DEMO_LANDLORD_ACCOUNT,
    DESTINATION_ID,
    OBLIGATION_ID,
    PAYMENT_REFERENCE,
    RUNNER_PRINCIPAL,
)


def _request(**overrides) -> PaymentRequest:
    base = dict(
        job_id="job-01",
        mandate_id="mandate-01",
        obligation_id=OBLIGATION_ID,
        debit_account_id=DEBIT_ACCOUNT,
        destination_account_id=DESTINATION_ID,
        destination_version=1,
        amount_minor=AMOUNT_MINOR,
        currency=CURRENCY,
        payment_reference=PAYMENT_REFERENCE,
    )
    base.update(overrides)
    return PaymentRequest(**base)


def test_authorized_request_pays_once(happy_path, verifier_registry):
    store = happy_path["store"]
    grant = happy_path["grant"]

    result = execute_payment(
        store,
        request=_request(),
        grant_token=grant["compact_jws"],
        idempotency_key="idem-1",
        authenticated_principal=RUNNER_PRINCIPAL,
        verifier_registry=verifier_registry,
    )

    assert result.status == "COMMITTED"
    assert result.payment_id is not None
    assert result.receipt_status == "PENDING"

    debit = store.peek("accounts", DEBIT_ACCOUNT)
    credit = store.peek("accounts", DEMO_LANDLORD_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000 - AMOUNT_MINOR
    assert credit["balance_minor"] == AMOUNT_MINOR

    obligation = store.peek("obligations", OBLIGATION_ID)
    assert obligation["state"] == "PAID"
    assert obligation["payment_id"] == result.payment_id

    consumed_grant = store.peek("grants", "grant-01")
    assert consumed_grant["state"] == "CONSUMED"


def test_altered_amount_produces_no_debit(happy_path, verifier_registry):
    store = happy_path["store"]
    grant = happy_path["grant"]

    result = execute_payment(
        store,
        request=_request(amount_minor=AMOUNT_MINOR + 1),
        grant_token=grant["compact_jws"],
        idempotency_key="idem-1",
        authenticated_principal=RUNNER_PRINCIPAL,
        verifier_registry=verifier_registry,
    )

    assert result.status == "BLOCKED"
    assert "SCOPE_MISMATCH" in result.reason_codes
    debit = store.peek("accounts", DEBIT_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000
