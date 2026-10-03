"""SPEC.md Section 13: the configuration A/B broad simulator must be a
*meaningful* baseline (it accepts an otherwise-valid alternate destination —
otherwise "the gateway blocked it" would be true of any strawman), while
every other control it shares with the real gateway still holds. Also
guards that this module is never wired into the public demo API.
"""

from __future__ import annotations

import pathlib

import pytest

from services.authority import destinations
from services.eval.broad_simulator import execute_payment_broad
from services.shared.schemas import PaymentRequest
from tests.conftest import (
    AMOUNT_MINOR,
    CURRENCY,
    DEBIT_ACCOUNT,
    DESTINATION_ID,
    OBLIGATION_ID,
    OWNER_UID,
    PAYMENT_REFERENCE,
    RUNNER_PRINCIPAL,
)

ALTERNATE_DESTINATION_ID = "acct-alternate-legit-landlord"
ALTERNATE_DEMO_ACCOUNT = "demo-alternate-legit-landlord"


def _request(**overrides) -> PaymentRequest:
    base = dict(
        job_id="job-01", mandate_id="mandate-01", obligation_id=OBLIGATION_ID, debit_account_id=DEBIT_ACCOUNT,
        destination_account_id=DESTINATION_ID, destination_version=1, amount_minor=AMOUNT_MINOR, currency=CURRENCY,
        payment_reference=PAYMENT_REFERENCE,
    )
    base.update(overrides)
    return PaymentRequest(**base)


def test_broad_simulator_accepts_an_otherwise_valid_alternate_destination(happy_path, verifier_registry):
    """This is the property that makes A/B a meaningful baseline at all:
    an alternate, structurally valid destination that was never approved
    still gets paid, because this configuration does not compare the
    request's tuple to the mandate's."""
    store = happy_path["store"]
    grant = happy_path["grant"]

    store.run_transaction(lambda t: t.set("accounts", ALTERNATE_DEMO_ACCOUNT, {
        "account_id": ALTERNATE_DEMO_ACCOUNT, "owner_uid": "someone-else", "currency": CURRENCY, "balance_minor": 0,
    }))
    destinations.create_destination_version(
        store, destination_account_id=ALTERNATE_DESTINATION_ID, version=1, owner_uid=OWNER_UID,
        demo_account_id=ALTERNATE_DEMO_ACCOUNT, label="Not the approved landlord",
    )

    result = execute_payment_broad(
        store, request=_request(destination_account_id=ALTERNATE_DESTINATION_ID, destination_version=1),
        grant_token=grant["compact_jws"], idempotency_key="idem-broad-1",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert result.status == "COMMITTED"
    credit = store.peek("accounts", ALTERNATE_DEMO_ACCOUNT)
    assert credit["balance_minor"] == AMOUNT_MINOR


def test_broad_simulator_still_rejects_principal_mismatch(happy_path, verifier_registry):
    store = happy_path["store"]
    grant = happy_path["grant"]
    result = execute_payment_broad(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-broad-2",
        authenticated_principal="some-other-runner", verifier_registry=verifier_registry,
    )
    assert result.status == "BLOCKED"
    assert "PRINCIPAL_MISMATCH" in result.reason_codes


def test_broad_simulator_still_prevents_a_second_transfer_for_one_obligation(happy_path, verifier_registry):
    store = happy_path["store"]
    grant = happy_path["grant"]
    first = execute_payment_broad(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-broad-3",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert first.status == "COMMITTED"

    second = execute_payment_broad(
        store, request=_request(destination_account_id=ALTERNATE_DESTINATION_ID, destination_version=1),
        grant_token=grant["compact_jws"], idempotency_key="idem-broad-4",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert second.status == "BLOCKED"
    assert "OBLIGATION_PAID" in second.reason_codes


def test_broad_simulator_still_rejects_a_revoked_grant(happy_path, verifier_registry):
    from services.authority import grants

    store = happy_path["store"]
    grant = happy_path["grant"]
    grants.revoke_grant(store, "grant-01", owner_uid=OWNER_UID)
    result = execute_payment_broad(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-broad-5",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert result.status == "BLOCKED"
    assert "GRANT_REVOKED" in result.reason_codes


def test_broad_simulator_is_never_imported_by_the_public_demo_api():
    """A static guard: this bypass path (and its baseline-agent counterpart)
    must never be reachable from the actual demo backend. If this ever
    fails, someone wired an eval-only bypass into a live-looking surface —
    stop and fix that before anything else.
    """
    api_dir = pathlib.Path(__file__).resolve().parents[2] / "services" / "api"
    for py_file in api_dir.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        assert "broad_simulator" not in text, f"{py_file} references the eval-only broad simulator"
        assert "baseline_orchestrator" not in text, f"{py_file} references the eval-only baseline orchestrator"
