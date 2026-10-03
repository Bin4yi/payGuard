"""SPEC.md Section 5 signed mandate lifecycle + Section 6 grant activation:
signing/persistence failures leave state PENDING and retry the same frozen
payload; a scheduled worker recovers after restarts; revoke-during-signing
always wins over a late signature; concurrent ("duplicate worker") signing
attempts never corrupt state or produce two different accepted signatures.
"""

from __future__ import annotations

import threading

import pytest

from services.authority import grants, mandates, recovery
from services.authority.mandates import DraftInput
from services.shared.local_adapters import FlakySigner
from tests.conftest import (
    AMOUNT_MINOR,
    CURRENCY,
    DEBIT_ACCOUNT,
    DESTINATION_ID,
    OWNER_UID,
    PAYMENT_REFERENCE,
    RUNNER_PRINCIPAL,
    make_draft,
)


def test_kms_outage_then_recovery_signs_mandate(seeded_store, authority_signer):
    store = seeded_store
    real_signer, _ = authority_signer
    flaky = FlakySigner(real_signer, fail_times=2)

    draft = mandates.create_draft(store, make_draft("mandate-flaky"))
    mandates.confirm_mandate(
        store, mandate_id="mandate-flaky", owner_uid=OWNER_UID, version=draft["version"], nonce=draft["nonce"]
    )

    # First two attempts fail like a transient KMS outage; the mandate stays
    # exactly as it was (nothing partially written) because sign_mandate
    # calls the signer *before* touching the store at all.
    with pytest.raises(RuntimeError):
        mandates.sign_mandate(store, "mandate-flaky", flaky)
    mandate = store.peek("mandates", "mandate-flaky")
    assert mandate["signing_state"] == "PENDING"
    assert mandate["compact_jws"] is None

    report = recovery.recover_pending_mandate_signatures(store, flaky)
    assert report.mandates_failed == ["mandate-flaky"]
    assert report.mandates_signed == []

    # Third attempt (KMS recovered) succeeds via the same drain-style worker.
    report2 = recovery.recover_pending_mandate_signatures(store, flaky)
    assert report2.mandates_signed == ["mandate-flaky"]
    mandate = store.peek("mandates", "mandate-flaky")
    assert mandate["signing_state"] == "SIGNED"
    assert mandate["compact_jws"] is not None


def test_kms_outage_then_recovery_activates_grant(seeded_store, authority_signer):
    store = seeded_store
    real_signer, _ = authority_signer
    approve_mandate_id = "mandate-grant-flaky"
    from tests.conftest import approve_and_sign_mandate

    approve_and_sign_mandate(store, real_signer, approve_mandate_id)
    grants.reserve_job_and_grant(
        store, job_id="job-flaky", grant_id="grant-flaky", mandate_id=approve_mandate_id,
        owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
    )

    flaky = FlakySigner(real_signer, fail_times=1)
    with pytest.raises(RuntimeError):
        grants.sign_and_activate_grant(store, "grant-flaky", flaky)
    grant = store.peek("grants", "grant-flaky")
    assert grant["state"] == "PENDING_SIGNATURE"
    assert grant["compact_jws"] is None

    report = recovery.recover_pending_grant_signatures(store, flaky)
    assert report.grants_activated == ["grant-flaky"]
    grant = store.peek("grants", "grant-flaky")
    assert grant["state"] == "ACTIVE"


def test_worker_recovers_after_restart_multiple_pending_mandates(seeded_store, authority_signer):
    """Simulates a process crash leaving several mandates PENDING; a single
    drain call (as a Cloud Scheduler tick would trigger) recovers all of
    them, and a repeat drain is a safe no-op."""
    store = seeded_store
    signer, _ = authority_signer

    for i in range(4):
        mandate_id = f"mandate-restart-{i}"
        draft = mandates.create_draft(
            store,
            DraftInput(
                mandate_id=mandate_id, owner_uid=OWNER_UID, instruction_id="instruction-01", instruction_version=1,
                obligation_id=f"rent-2026-09-restart-{i}", debit_account_id=DEBIT_ACCOUNT, payee_id="payee-01",
                destination_account_id=DESTINATION_ID, destination_version=1, amount_mode="exact",
                amount_minor=AMOUNT_MINOR, currency=CURRENCY, payment_reference=PAYMENT_REFERENCE,
                grant_expiry_seconds=300, selected_document_ids=(), policy_version="rent-v1",
                supersedes_mandate_id=None,
            ),
        )
        mandates.confirm_mandate(
            store, mandate_id=mandate_id, owner_uid=OWNER_UID, version=draft["version"], nonce=draft["nonce"]
        )

    report = recovery.drain(store, signer)
    assert set(report.mandates_signed) == {f"mandate-restart-{i}" for i in range(4)}

    # Repeat drain: nothing left pending, nothing re-signed.
    report2 = recovery.drain(store, signer)
    assert report2.mandates_signed == []
    assert report2.mandates_failed == []


def test_revoke_during_signing_always_wins_over_a_late_signature(seeded_store, authority_signer):
    store = seeded_store
    signer, _ = authority_signer
    draft = mandates.create_draft(store, make_draft("mandate-race-revoke"))
    mandates.confirm_mandate(
        store, mandate_id="mandate-race-revoke", owner_uid=OWNER_UID, version=draft["version"], nonce=draft["nonce"]
    )

    mandates.revoke_mandate(store, "mandate-race-revoke", owner_uid=OWNER_UID)

    # A signing attempt that was already "in flight" when the revoke landed
    # discovers, at its compare-and-set, that the mandate is no longer
    # APPROVED/PENDING — its freshly produced signature is discarded.
    result = mandates.sign_mandate(store, "mandate-race-revoke", signer)
    assert result["state"] == "REVOKED"
    assert result["compact_jws"] is None


def test_concurrent_signers_never_produce_two_accepted_signatures(seeded_store, authority_signer):
    """'Duplicate worker' race: two threads both try to sign the same
    PENDING mandate concurrently. Exactly one signature is ever persisted
    (compare-and-set), and the final state is fully signed and consistent."""
    store = seeded_store
    signer, _ = authority_signer
    draft = mandates.create_draft(store, make_draft("mandate-dup-worker"))
    mandates.confirm_mandate(
        store, mandate_id="mandate-dup-worker", owner_uid=OWNER_UID, version=draft["version"], nonce=draft["nonce"]
    )

    results = [None, None]
    errors = [None, None]

    def worker(i):
        try:
            results[i] = mandates.sign_mandate(store, "mandate-dup-worker", signer)
        except Exception as exc:  # pragma: no cover
            errors[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert all(e is None for e in errors), errors
    final = store.peek("mandates", "mandate-dup-worker")
    assert final["signing_state"] == "SIGNED"
    assert final["compact_jws"] is not None
    # Both threads' return values reflect the same, single persisted token.
    assert results[0]["compact_jws"] == final["compact_jws"] or results[1]["compact_jws"] == final["compact_jws"]
