"""SPEC.md Section 8: revocation/execution serialize on the same records —
whichever commits first wins. SPEC.md Section 6: an expired grant can never
execute, and expiry cannot be silently extended."""

from __future__ import annotations

import time

import pytest

from services.authority import grants, mandates
from services.gateway.enforcement import execute_payment
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


def test_revoke_before_execute_blocks_execution(happy_path, verifier_registry):
    store = happy_path["store"]
    grant = happy_path["grant"]

    grants.revoke_grant(store, "grant-01", owner_uid=OWNER_UID)

    result = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-rev-1",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert result.status == "BLOCKED"
    assert "GRANT_REVOKED" in result.reason_codes
    debit = store.peek("accounts", DEBIT_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000


def test_execute_before_revoke_commits_and_later_revoke_is_a_noop_on_payment(happy_path, verifier_registry):
    store = happy_path["store"]
    grant = happy_path["grant"]

    result = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-rev-2",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert result.status == "COMMITTED"

    # Revocation after commit cannot be requested on a CONSUMED grant.
    with pytest.raises(grants.GrantError):
        grants.revoke_grant(store, "grant-01", owner_uid=OWNER_UID)

    obligation = store.peek("obligations", OBLIGATION_ID)
    assert obligation["state"] == "PAID"
    assert obligation["payment_id"] == result.payment_id


def test_expired_grant_never_executes_even_with_valid_signature(seeded_store, authority_signer, verifier_registry):
    store = seeded_store
    signer, _ = authority_signer
    approve_and_sign_mandate(store, signer, "mandate-01")
    grant = reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")

    result = execute_payment(
        store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="idem-exp-1",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
        now_fn=lambda: time.time() + 301,  # one second past the 300s grant_expiry_seconds
    )
    assert result.status == "BLOCKED"
    assert "GRANT_EXPIRED" in result.reason_codes


def test_expiry_before_activation_requires_fresh_confirmation(seeded_store, authority_signer):
    """SPEC.md Section 6: if the frozen token expires before activation,
    require fresh user confirmation; never extend its times or silently mint
    a replacement."""
    store = seeded_store
    signer, _ = authority_signer

    draft = mandates.create_draft(
        store,
        mandates.DraftInput(
            mandate_id="mandate-shortlived", owner_uid=OWNER_UID, instruction_id="instruction-01",
            instruction_version=1, obligation_id=OBLIGATION_ID, debit_account_id=DEBIT_ACCOUNT,
            payee_id="payee-01", destination_account_id=DESTINATION_ID, destination_version=1,
            amount_mode="exact", amount_minor=AMOUNT_MINOR, currency=CURRENCY, payment_reference=PAYMENT_REFERENCE,
            grant_expiry_seconds=1,  # expires almost immediately
            selected_document_ids=(), policy_version="rent-v1", supersedes_mandate_id=None,
        ),
    )
    mandates.confirm_mandate(
        store, mandate_id="mandate-shortlived", owner_uid=OWNER_UID, version=draft["version"], nonce=draft["nonce"]
    )
    mandates.sign_mandate(store, "mandate-shortlived", signer)

    grants.reserve_job_and_grant(
        store, job_id="job-shortlived", grant_id="grant-shortlived", mandate_id="mandate-shortlived",
        owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
    )
    time.sleep(1.1)
    with pytest.raises(grants.GrantError, match="frozen_token_expired_before_activation"):
        grants.sign_and_activate_grant(store, "grant-shortlived", signer)


def test_revocation_during_signing_prevents_activation(seeded_store, authority_signer):
    """A grant revoked while PENDING_SIGNATURE can never activate, even if a
    signing attempt is already in flight — the compare-and-set in
    sign_and_activate_grant loses the race and discards the signature."""
    store = seeded_store
    signer, _ = authority_signer
    approve_and_sign_mandate(store, signer, "mandate-race")
    grants.reserve_job_and_grant(
        store, job_id="job-race", grant_id="grant-race", mandate_id="mandate-race",
        owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
    )

    grants.revoke_grant(store, "grant-race", owner_uid=OWNER_UID)

    activated = grants.sign_and_activate_grant(store, "grant-race", signer)
    assert activated["state"] == "REVOKED"  # signature discarded, never persisted as ACTIVE
