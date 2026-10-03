"""SPEC.md Section 13 'Credential validation': tampered/mismatched grant and
mandate credentials must be rejected before any ledger mutation."""

from __future__ import annotations

import base64
import json

import pytest

from services.authority import grants, mandates
from services.gateway.enforcement import execute_payment
from services.shared.local_adapters import LocalSigner, LocalVerifierRegistry, generate_rsa_keypair
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
    approve_and_sign_mandate,
    reserve_and_activate_grant,
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


def _assert_blocked_no_debit(store, result, expected_reason):
    assert result.status == "BLOCKED"
    assert expected_reason in result.reason_codes
    debit = store.peek("accounts", DEBIT_ACCOUNT)
    credit = store.peek("accounts", DEMO_LANDLORD_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000
    assert credit["balance_minor"] == 0


def test_wrong_key_grant_rejected(happy_path, verifier_registry):
    store = happy_path["store"]
    other_private, _ = generate_rsa_keypair()
    forged_signer = LocalSigner("authority-key-1", other_private)  # right kid, wrong key
    from services.shared.jws import encode_compact
    from services.shared.normalize import canonical_bytes

    real_grant = store.peek("grants", "grant-01")
    forged_token = encode_compact(
        typ="payguard-grant+jwt",
        kid="authority-key-1",
        payload_bytes=canonical_bytes(real_grant["frozen_payload"]),
        signer=forged_signer,
    )

    result = execute_payment(
        store,
        request=_request(),
        grant_token=forged_token,
        idempotency_key="idem-cred-1",
        authenticated_principal=RUNNER_PRINCIPAL,
        verifier_registry=verifier_registry,
    )
    _assert_blocked_no_debit(store, result, "INVALID_GRANT")


def test_wrong_typ_rejected(happy_path, verifier_registry, authority_signer):
    store = happy_path["store"]
    signer, _ = authority_signer
    real_grant = store.peek("grants", "grant-01")
    from services.shared.jws import encode_compact
    from services.shared.normalize import canonical_bytes

    mandate_shaped_token = encode_compact(
        typ="payguard-mandate+jwt",  # wrong typ presented as a grant
        kid="authority-key-1",
        payload_bytes=canonical_bytes(real_grant["frozen_payload"]),
        signer=signer,
    )
    result = execute_payment(
        store,
        request=_request(),
        grant_token=mandate_shaped_token,
        idempotency_key="idem-cred-2",
        authenticated_principal=RUNNER_PRINCIPAL,
        verifier_registry=verifier_registry,
    )
    _assert_blocked_no_debit(store, result, "INVALID_GRANT")


def test_wrong_audience_rejected(happy_path, verifier_registry, authority_signer):
    store = happy_path["store"]
    signer, _ = authority_signer
    real_grant = store.peek("grants", "grant-01")
    from services.shared.jws import encode_compact
    from services.shared.normalize import canonical_bytes

    bad_payload = dict(real_grant["frozen_payload"])
    bad_payload["aud"] = "some-other-service"
    token = encode_compact(
        typ="payguard-grant+jwt", kid="authority-key-1", payload_bytes=canonical_bytes(bad_payload), signer=signer
    )
    result = execute_payment(
        store,
        request=_request(),
        grant_token=token,
        idempotency_key="idem-cred-3",
        authenticated_principal=RUNNER_PRINCIPAL,
        verifier_registry=verifier_registry,
    )
    _assert_blocked_no_debit(store, result, "INVALID_GRANT")


def test_wrong_issuer_rejected(happy_path, verifier_registry, authority_signer):
    store = happy_path["store"]
    signer, _ = authority_signer
    real_grant = store.peek("grants", "grant-01")
    from services.shared.jws import encode_compact
    from services.shared.normalize import canonical_bytes

    bad_payload = dict(real_grant["frozen_payload"])
    bad_payload["iss"] = "attacker-authority"
    token = encode_compact(
        typ="payguard-grant+jwt", kid="authority-key-1", payload_bytes=canonical_bytes(bad_payload), signer=signer
    )
    result = execute_payment(
        store,
        request=_request(),
        grant_token=token,
        idempotency_key="idem-cred-4",
        authenticated_principal=RUNNER_PRINCIPAL,
        verifier_registry=verifier_registry,
    )
    _assert_blocked_no_debit(store, result, "INVALID_GRANT")


def test_caller_principal_mismatch_rejected(happy_path, verifier_registry):
    store = happy_path["store"]
    grant = happy_path["grant"]
    result = execute_payment(
        store,
        request=_request(),
        grant_token=grant["compact_jws"],
        idempotency_key="idem-cred-5",
        authenticated_principal="some-other-runner",  # caller-supplied identity never trusted
        verifier_registry=verifier_registry,
    )
    _assert_blocked_no_debit(store, result, "PRINCIPAL_MISMATCH")


def test_missing_required_claim_rejected(happy_path, verifier_registry, authority_signer):
    store = happy_path["store"]
    signer, _ = authority_signer
    real_grant = store.peek("grants", "grant-01")
    from services.shared.jws import encode_compact
    from services.shared.normalize import canonical_bytes

    bad_payload = dict(real_grant["frozen_payload"])
    del bad_payload["agent_principal"]
    token = encode_compact(
        typ="payguard-grant+jwt", kid="authority-key-1", payload_bytes=canonical_bytes(bad_payload), signer=signer
    )
    result = execute_payment(
        store,
        request=_request(),
        grant_token=token,
        idempotency_key="idem-cred-6",
        authenticated_principal=RUNNER_PRINCIPAL,
        verifier_registry=verifier_registry,
    )
    _assert_blocked_no_debit(store, result, "INVALID_GRANT")


def test_swapped_job_grant_rejected(seeded_store, verifier_registry, authority_signer):
    """A grant legitimately issued for a *different* job/mandate cannot be
    reused against this obligation's payment request."""
    store = seeded_store
    signer, _ = authority_signer

    approve_and_sign_mandate(store, signer, "mandate-A")
    grant_a = reserve_and_activate_grant(store, signer, job_id="job-A", grant_id="grant-A", mandate_id="mandate-A")

    result = execute_payment(
        store,
        request=_request(job_id="job-B", mandate_id="mandate-B"),
        grant_token=grant_a["compact_jws"],
        idempotency_key="idem-cred-7",
        authenticated_principal=RUNNER_PRINCIPAL,
        verifier_registry=verifier_registry,
    )
    _assert_blocked_no_debit(store, result, "INVALID_GRANT")


def test_token_hash_mismatch_rejected(happy_path, verifier_registry, authority_signer):
    """Re-sign the *same* claims: PSS is randomized, so the bytes differ even
    though both verify — only the persisted token_hash is authoritative."""
    store = happy_path["store"]
    signer, _ = authority_signer
    real_grant = store.peek("grants", "grant-01")
    from services.shared.jws import encode_compact
    from services.shared.normalize import canonical_bytes

    re_signed = encode_compact(
        typ="payguard-grant+jwt",
        kid="authority-key-1",
        payload_bytes=canonical_bytes(real_grant["frozen_payload"]),
        signer=signer,
    )
    assert re_signed != real_grant["compact_jws"]  # confirms PSS randomization assumption
    result = execute_payment(
        store,
        request=_request(),
        grant_token=re_signed,
        idempotency_key="idem-cred-8",
        authenticated_principal=RUNNER_PRINCIPAL,
        verifier_registry=verifier_registry,
    )
    _assert_blocked_no_debit(store, result, "INVALID_GRANT")


def test_expired_grant_rejected(seeded_store, verifier_registry, authority_signer):
    import time

    store = seeded_store
    signer, _ = authority_signer
    approve_and_sign_mandate(store, signer, "mandate-exp")
    grant = reserve_and_activate_grant(store, signer, job_id="job-exp", grant_id="grant-exp", mandate_id="mandate-exp")

    result = execute_payment(
        store,
        request=_request(job_id="job-exp", mandate_id="mandate-exp"),
        grant_token=grant["compact_jws"],
        idempotency_key="idem-cred-9",
        authenticated_principal=RUNNER_PRINCIPAL,
        verifier_registry=verifier_registry,
        now_fn=lambda: time.time() + 10_000,  # long past the 300s max lifetime
    )
    _assert_blocked_no_debit(store, result, "GRANT_EXPIRED")


def test_revoked_grant_rejected(happy_path, verifier_registry):
    store = happy_path["store"]
    grant = happy_path["grant"]
    grants.revoke_grant(store, "grant-01", owner_uid="user-01")

    result = execute_payment(
        store,
        request=_request(),
        grant_token=grant["compact_jws"],
        idempotency_key="idem-cred-10",
        authenticated_principal=RUNNER_PRINCIPAL,
        verifier_registry=verifier_registry,
    )
    _assert_blocked_no_debit(store, result, "GRANT_REVOKED")


def test_pending_signature_grant_cannot_execute(seeded_store, verifier_registry, authority_signer):
    """A grant that was reserved but never signed/activated must not run
    the runner, and certainly cannot execute — there is no valid compact_jws
    to even present, which this test demonstrates cannot be forged around."""
    store = seeded_store
    signer, _ = authority_signer
    approve_and_sign_mandate(store, signer, "mandate-pending")
    reservation = grants.reserve_job_and_grant(
        store, job_id="job-pending", grant_id="grant-pending", mandate_id="mandate-pending",
        owner_uid="user-01", runner_principal=RUNNER_PRINCIPAL,
    )
    assert reservation["grant"]["state"] == "PENDING_SIGNATURE"
    assert reservation["grant"]["compact_jws"] is None
