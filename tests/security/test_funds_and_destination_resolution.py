"""INSUFFICIENT_FUNDS and destination-resolution edge cases (SPEC.md Section 7)."""

from __future__ import annotations

from services.gateway.enforcement import execute_payment
from services.shared.schemas import PaymentRequest
from tests.conftest import (
    AMOUNT_MINOR,
    CURRENCY,
    DEBIT_ACCOUNT,
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


def test_insufficient_funds_blocks_and_leaves_balances_untouched(happy_path, verifier_registry):
    store = happy_path["store"]
    grant = happy_path["grant"]

    # Drain the tenant account below the mandate's fixed amount.
    debit = store.peek("accounts", DEBIT_ACCOUNT)
    debit["balance_minor"] = AMOUNT_MINOR - 1
    store.run_transaction(lambda t: t.set("accounts", DEBIT_ACCOUNT, debit))

    result = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-funds-1",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert result.status == "BLOCKED"
    assert "INSUFFICIENT_FUNDS" in result.reason_codes

    obligation = store.peek("obligations", OBLIGATION_ID)
    assert obligation["state"] == "UNPAID"
    assert store.peek("accounts", DEBIT_ACCOUNT)["balance_minor"] == AMOUNT_MINOR - 1


def test_exact_balance_boundary_succeeds(happy_path, verifier_registry):
    store = happy_path["store"]
    grant = happy_path["grant"]

    debit = store.peek("accounts", DEBIT_ACCOUNT)
    debit["balance_minor"] = AMOUNT_MINOR  # exactly enough, no more
    store.run_transaction(lambda t: t.set("accounts", DEBIT_ACCOUNT, debit))

    result = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-funds-2",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert result.status == "COMMITTED"
    assert store.peek("accounts", DEBIT_ACCOUNT)["balance_minor"] == 0
