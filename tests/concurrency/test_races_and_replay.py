"""SPEC.md Section 13 'Races' and 'Replay', and PROJECT.md Section 6:
25 simultaneous requests for one obligation must yield exactly one transfer,
and no combination of retried/replaced credentials can produce a second one.
"""

from __future__ import annotations

import threading

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


def _run_concurrently(fns):
    results = [None] * len(fns)
    errors = [None] * len(fns)
    threads = []

    def _runner(i, fn):
        try:
            results[i] = fn()
        except Exception as exc:  # pragma: no cover - surfaced via assertion below
            errors[i] = exc

    for i, fn in enumerate(fns):
        th = threading.Thread(target=_runner, args=(i, fn))
        threads.append(th)
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    return results, errors


@pytest.mark.parametrize("repeat", range(5))
def test_25_concurrent_requests_yield_exactly_one_transfer(happy_path, verifier_registry, repeat):
    """25 threads race the *same* payment tuple, each with its own key.

    Only one can win the obligation lock and physically debit/credit. Every
    other thread observes the obligation already paid with a matching tuple
    from its own originating job/mandate/grant, so per SPEC.md Section 7 step
    5 ("A new key can retrieve the same result") it also reports COMMITTED
    with the *same* payment_id — this is a read-only replay, not a second
    transfer. So "exactly one transfer" is verified by the ledger having a
    single transfer record and one balance movement, not by counting BLOCKED
    results (there may legitimately be zero of those here).
    """
    store = happy_path["store"]
    grant = happy_path["grant"]

    def attempt(i):
        return lambda: execute_payment(
            store,
            request=_request(),
            grant_token=grant["compact_jws"],
            idempotency_key=f"idem-race-{i}",  # every thread uses a *different* key
            authenticated_principal=RUNNER_PRINCIPAL,
            verifier_registry=verifier_registry,
        )

    fns = [attempt(i) for i in range(25)]
    results, errors = _run_concurrently(fns)

    assert all(e is None for e in errors), errors
    committed = [r for r in results if r.status == "COMMITTED"]
    blocked = [r for r in results if r.status == "BLOCKED"]

    # Every commit (whether the winner or a same-tuple replay) must agree on
    # exactly one payment_id — never two different transfers.
    assert len(committed) >= 1
    assert len({r.payment_id for r in committed}) == 1
    assert all(rc == () for r in committed for rc in [r.reason_codes])
    assert all("OBLIGATION_PAID" in r.reason_codes or "IDEMPOTENCY_CONFLICT" in r.reason_codes for r in blocked)

    debit = store.peek("accounts", DEBIT_ACCOUNT)
    credit = store.peek("accounts", DEMO_LANDLORD_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000 - AMOUNT_MINOR
    assert credit["balance_minor"] == AMOUNT_MINOR

    transfers = [
        store.peek("ledger_transfers", pid) for pid in {r.payment_id for r in committed}
    ]
    assert len(transfers) == 1


def test_same_key_and_tuple_replay_returns_same_transfer(happy_path, verifier_registry):
    store = happy_path["store"]
    grant = happy_path["grant"]

    first = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-replay-1",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    second = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-replay-1",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )

    assert first.status == second.status == "COMMITTED"
    assert first.payment_id == second.payment_id

    debit = store.peek("accounts", DEBIT_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000 - AMOUNT_MINOR  # debited only once


def test_same_key_changed_body_is_a_conflict_not_a_second_debit(happy_path, verifier_registry):
    """A single mandate pins exactly one payment tuple, so a body change is
    *also* always a scope violation (SPEC.md Section 7 step 3 runs before the
    idempotency-key check in step 5) — it is caught as SCOPE_MISMATCH here,
    which is a stronger/more specific rejection than IDEMPOTENCY_CONFLICT and
    still satisfies the required outcome ("<= 1 transfer per obligation").
    See ``test_idempotency_conflict_when_stored_digest_differs`` below for a
    direct test of the IDEMPOTENCY_CONFLICT branch itself.
    """
    store = happy_path["store"]
    grant = happy_path["grant"]

    first = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-replay-2",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert first.status == "COMMITTED"

    conflicting = execute_payment(
        store, request=_request(payment_reference="RENT-2026-09-01-DIFFERENT"), grant_token=grant["compact_jws"],
        idempotency_key="idem-replay-2", authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert conflicting.status == "BLOCKED"
    assert "SCOPE_MISMATCH" in conflicting.reason_codes

    debit = store.peek("accounts", DEBIT_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000 - AMOUNT_MINOR


def test_idempotency_conflict_when_stored_digest_differs(happy_path, verifier_registry):
    """Directly exercises SPEC.md Section 7 step 5's IDEMPOTENCY_CONFLICT
    branch: an idempotency record already exists under this
    (owner, obligation, key) with a digest that does not match the current
    request's semantic tuple. This models a client-side key-reuse bug or a
    corrupted retry, independent of whether the current request would also
    fail scope binding for some other reason.
    """
    store = happy_path["store"]
    grant = happy_path["grant"]

    store.run_transaction(
        lambda t: t.set(
            "idempotency",
            "user-01:rent-2026-09-01:idem-poisoned",
            {
                "key": "idem-poisoned",
                "owner_uid": "user-01",
                "obligation_id": OBLIGATION_ID,
                "request_digest": "sha256:not-the-real-digest",
                "payment_id": "pay-does-not-exist",
            },
        )
    )

    result = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-poisoned",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert result.status == "BLOCKED"
    assert "IDEMPOTENCY_CONFLICT" in result.reason_codes

    debit = store.peek("accounts", DEBIT_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000  # no debit at all — obligation still unpaid


def test_new_key_after_payment_returns_existing_result_not_a_new_transfer(happy_path, verifier_registry):
    store = happy_path["store"]
    grant = happy_path["grant"]

    first = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-replay-3",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert first.status == "COMMITTED"

    same_tuple_new_key = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-replay-3-NEW",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert same_tuple_new_key.status == "COMMITTED"
    assert same_tuple_new_key.payment_id == first.payment_id

    debit = store.peek("accounts", DEBIT_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000 - AMOUNT_MINOR  # still only one debit


def test_consumed_grant_cannot_execute_a_new_transfer(happy_path, verifier_registry):
    """A CONSUMED grant only has the read-only matching-retry path; it can
    never re-enter fresh execution, even with the same tuple restated."""
    store = happy_path["store"]
    grant = happy_path["grant"]

    execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-replay-4",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    consumed = store.peek("grants", "grant-01")
    assert consumed["state"] == "CONSUMED"

    replay = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-replay-4-again",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert replay.status == "COMMITTED"
    assert replay.payment_id is not None

    debit = store.peek("accounts", DEBIT_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000 - AMOUNT_MINOR


def test_replacement_grant_cannot_pay_an_already_paid_obligation(seeded_store, authority_signer, verifier_registry):
    """A *different*, independently-issued grant for a replacement mandate
    cannot pay an obligation that a prior grant already paid."""
    from services.authority import grants, mandates
    from tests.conftest import approve_and_sign_mandate, reserve_and_activate_grant

    store = seeded_store
    signer, _ = authority_signer

    approve_and_sign_mandate(store, signer, "mandate-orig")
    grant1 = reserve_and_activate_grant(store, signer, job_id="job-orig", grant_id="grant-orig", mandate_id="mandate-orig")

    paid = execute_payment(
        store, request=_request(job_id="job-orig", mandate_id="mandate-orig"), grant_token=grant1["compact_jws"],
        idempotency_key="idem-repl-1", authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert paid.status == "COMMITTED"

    # Attempting a same-obligation replacement mandate now fails because the
    # obligation is already paid (not unpaid) — this is the correct refusal,
    # proven by asserting the specific error.
    draft = mandates.create_draft(
        store,
        mandates.DraftInput(
            mandate_id="mandate-repl", owner_uid="user-01", instruction_id="instruction-01", instruction_version=1,
            obligation_id=OBLIGATION_ID, debit_account_id=DEBIT_ACCOUNT, payee_id="payee-01",
            destination_account_id=DESTINATION_ID, destination_version=1, amount_mode="exact",
            amount_minor=AMOUNT_MINOR, currency=CURRENCY, payment_reference=PAYMENT_REFERENCE,
            grant_expiry_seconds=300, selected_document_ids=(), policy_version="rent-v1",
            supersedes_mandate_id="mandate-orig",
        ),
    )
    with pytest.raises(mandates.MandateError):
        mandates.confirm_mandate(
            store, mandate_id="mandate-repl", owner_uid="user-01", version=draft["version"], nonce=draft["nonce"]
        )

    debit = store.peek("accounts", DEBIT_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000 - AMOUNT_MINOR
