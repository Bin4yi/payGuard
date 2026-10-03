"""SPEC.md Section 13 'Scope mutation': altering any payment field must
produce no unauthorised transfer — every case blocks, and the ledger is
untouched."""

from __future__ import annotations

import pytest

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


MUTATIONS = [
    pytest.param({"destination_account_id": "acct-attacker-99"}, "DESTINATION_CHANGED", id="payee"),
    pytest.param({"destination_version": 2}, "DESTINATION_CHANGED", id="destination_version"),
    pytest.param({"debit_account_id": "acct-someone-else"}, "SCOPE_MISMATCH", id="debit_account"),
    pytest.param({"amount_minor": AMOUNT_MINOR + 1}, "SCOPE_MISMATCH", id="amount"),
    pytest.param({"amount_minor": AMOUNT_MINOR - 1}, "SCOPE_MISMATCH", id="amount_lower"),
    pytest.param({"payment_reference": "RENT-2026-10-01"}, "SCOPE_MISMATCH", id="reference"),
    pytest.param({"obligation_id": "rent-2026-10-01"}, "INVALID_GRANT", id="obligation"),
]


@pytest.mark.parametrize("mutation,expected_reason", MUTATIONS)
def test_mutated_field_is_blocked(happy_path, verifier_registry, mutation, expected_reason):
    store = happy_path["store"]
    grant = happy_path["grant"]

    result = execute_payment(
        store,
        request=_request(**mutation),
        grant_token=grant["compact_jws"],
        idempotency_key="idem-scope-1",
        authenticated_principal=RUNNER_PRINCIPAL,
        verifier_registry=verifier_registry,
    )

    assert result.status == "BLOCKED"
    assert expected_reason in result.reason_codes

    debit = store.peek("accounts", DEBIT_ACCOUNT)
    credit = store.peek("accounts", DEMO_LANDLORD_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000
    assert credit["balance_minor"] == 0
    obligation = store.peek("obligations", OBLIGATION_ID)
    assert obligation["state"] == "UNPAID"


def test_currency_mutation_is_blocked(happy_path, verifier_registry):
    """currency is a Literal["LKR"] field — pydantic itself refuses any other
    value before the request ever reaches the gateway, which is an even
    stronger guarantee than a gateway-level SCOPE_MISMATCH."""
    with pytest.raises(Exception):
        _request(currency="USD")
