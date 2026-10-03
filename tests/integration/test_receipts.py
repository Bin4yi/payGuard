"""SPEC.md Section 9: receipt signing, chaining, and in-app verification."""

from __future__ import annotations

import pytest

from services.gateway.enforcement import execute_payment
from services.receipts import signing, verify
from services.shared.local_adapters import LocalSigner, LocalVerifierRegistry, generate_rsa_keypair
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


@pytest.fixture
def receipt_signer():
    private_key, public_key = generate_rsa_keypair()
    signer = LocalSigner("receipt-key-1", private_key)
    registry = LocalVerifierRegistry({"receipt-key-1": public_key})
    return signer, registry


def test_committed_and_blocked_events_sign_and_verify(happy_path, verifier_registry, receipt_signer):
    store = happy_path["store"]
    grant = happy_path["grant"]
    receipt_signing_key, receipt_registry = receipt_signer

    blocked = execute_payment(
        store, request=_request(amount_minor=AMOUNT_MINOR + 1), grant_token=grant["compact_jws"],
        idempotency_key="idem-r1", authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert blocked.status == "BLOCKED"

    committed = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-r2",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert committed.status == "COMMITTED"

    signed = signing.drain_workflow(store, "job-01", receipt_signing_key)
    assert len(signed) == 2
    assert signed[0]["sequence"] == 1
    assert signed[1]["sequence"] == 2

    result = verify.verify_chain(
        store, "job-01", receipt_registry=receipt_registry, authority_registry=verifier_registry
    )
    assert result.all_verified
    assert result.completeness_unknown is False
    assert result.highest_verified_sequence == 2
    assert [e.status for e in result.entries] == ["VERIFIED", "VERIFIED"]


def test_signed_is_not_verified_until_checked(happy_path, verifier_registry, receipt_signer):
    """SIGNED (a JWS is stored) and VERIFIED (a verifier checked it) are
    different statements — signing alone never counts as verification."""
    store = happy_path["store"]
    grant = happy_path["grant"]
    receipt_signing_key, receipt_registry = receipt_signer

    execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-r3",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    event = store.peek("workflow_events", "job-01:1")
    assert event["signing_state"] == "PENDING"  # not SIGNED yet — no worker has run

    outcome = signing.sign_next_pending(store, "job-01", receipt_signing_key)
    assert outcome.signed
    event = store.peek("workflow_events", "job-01:1")
    assert event["signing_state"] == "SIGNED"  # now SIGNED

    # But nobody has *verified* it yet until verify_chain actually runs.
    result = verify.verify_chain(store, "job-01", receipt_registry=receipt_registry)
    assert result.all_verified


def test_truncated_suffix_is_completeness_unknown_not_tampered(happy_path, verifier_registry, receipt_signer):
    """Only sequence 1 of 2 outbox events has been signed so far (e.g. the
    receipt worker hasn't caught up). This is an ordinary pending suffix —
    every present receipt still verifies — but completeness cannot be
    claimed because sequence 2 has not been produced yet."""
    store = happy_path["store"]
    grant = happy_path["grant"]
    receipt_signing_key, receipt_registry = receipt_signer

    execute_payment(
        store, request=_request(amount_minor=AMOUNT_MINOR + 1), grant_token=grant["compact_jws"],
        idempotency_key="idem-r4", authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-r5",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    signing.sign_next_pending(store, "job-01", receipt_signing_key)  # sign only sequence 1

    result = verify.verify_chain(
        store, "job-01", receipt_registry=receipt_registry, authority_registry=verifier_registry
    )
    assert [e.status for e in result.entries] == ["VERIFIED"]
    assert result.gaps == []  # no *internal* gap — just nothing past the highest signed sequence
    assert result.completeness_unknown is True  # but completeness still cannot be claimed


def test_removed_middle_event_is_detected_as_a_gap(happy_path, verifier_registry, receipt_signer):
    store = happy_path["store"]
    grant = happy_path["grant"]
    receipt_signing_key, receipt_registry = receipt_signer

    for i in range(3):
        execute_payment(
            store, request=_request(amount_minor=AMOUNT_MINOR + 1 + i), grant_token=grant["compact_jws"],
            idempotency_key=f"idem-gap-{i}", authenticated_principal=RUNNER_PRINCIPAL,
            verifier_registry=verifier_registry,
        )
    signed = signing.drain_workflow(store, "job-01", receipt_signing_key)
    assert len(signed) == 3

    # Simulate an attacker (or a corrupted export) removing the middle receipt.
    store.run_transaction(lambda t: t.delete("receipts", "job-01:2"))

    result = verify.verify_chain(store, "job-01", receipt_registry=receipt_registry)
    assert result.gaps == [2]
    assert result.completeness_unknown is True
    statuses = {e.sequence: e.status for e in result.entries}
    assert statuses[2] == "MISSING"
    assert statuses[3] == "CHAIN_BROKEN"  # sequence 3 no longer chains to the (now missing) sequence 2


def test_tampered_stored_jws_bytes_detected(happy_path, verifier_registry, receipt_signer):
    store = happy_path["store"]
    grant = happy_path["grant"]
    receipt_signing_key, receipt_registry = receipt_signer

    execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-tamper-1",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    signing.sign_next_pending(store, "job-01", receipt_signing_key)

    receipt = store.peek("receipts", "job-01:1")
    tampered = dict(receipt)
    header, payload, sig = tampered["compact_jws"].split(".")
    tampered["compact_jws"] = f"{header}.{payload}.{sig[:-4]}AAAA"  # corrupt the signature bytes
    store.run_transaction(lambda t: t.set("receipts", "job-01:1", tampered))

    result = verify.verify_chain(store, "job-01", receipt_registry=receipt_registry)
    assert result.entries[0].status in ("SIGNATURE_INVALID", "DIGEST_MISMATCH")
    assert result.completeness_unknown is True


def test_tampered_attempt_record_detected_via_supporting_digest(happy_path, verifier_registry, receipt_signer):
    """Altering the *exported* payment_attempts record after the fact must
    fail its referenced digest check, even though the receipt's own
    signature is untouched (SPEC.md Section 9: 'Altering an unsigned
    supporting record must fail its referenced digest check')."""
    store = happy_path["store"]
    grant = happy_path["grant"]
    receipt_signing_key, receipt_registry = receipt_signer

    result = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-tamper-2",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    signing.sign_next_pending(store, "job-01", receipt_signing_key)

    attempt = store.peek("payment_attempts", result.attempt_id)
    tampered = dict(attempt)
    tampered["request"] = dict(tampered["request"])
    tampered["request"]["amount_minor"] = AMOUNT_MINOR + 999  # forge the exported evidence
    store.run_transaction(lambda t: t.set("payment_attempts", result.attempt_id, tampered))

    verification = verify.verify_chain(
        store, "job-01", receipt_registry=receipt_registry, authority_registry=verifier_registry
    )
    assert verification.entries[0].status == "TAMPERED_SUPPORTING_RECORD"
    assert verification.completeness_unknown is True


def test_swapped_receipt_key_rejected(happy_path, verifier_registry, receipt_signer):
    """A receipt signed with a key not in the trusted receipt registry
    (e.g. someone swapped in an attacker-controlled key) must not verify —
    installing trust is independent of whatever the export claims."""
    store = happy_path["store"]
    grant = happy_path["grant"]
    _, receipt_registry = receipt_signer

    other_private, _ = generate_rsa_keypair()
    attacker_signer = LocalSigner("receipt-key-1", other_private)  # same kid, different key

    execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-tamper-3",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    signing.sign_next_pending(store, "job-01", attacker_signer)

    result = verify.verify_chain(store, "job-01", receipt_registry=receipt_registry)
    assert result.entries[0].status == "SIGNATURE_INVALID"
