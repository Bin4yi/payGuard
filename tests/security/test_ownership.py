"""SPEC.md Section 13 'Ownership': cross-user access to another owner's
mandate, job, or grant must fail."""

from __future__ import annotations

import pytest

from services.authority import grants, mandates
from services.authority.mandates import MandateError
from tests.conftest import approve_and_sign_mandate, make_draft, reserve_and_activate_grant


def test_cannot_confirm_another_users_mandate(seeded_store, authority_signer):
    store = seeded_store
    signer, _ = authority_signer
    draft = mandates.create_draft(store, make_draft("mandate-x"))

    with pytest.raises(MandateError):
        mandates.confirm_mandate(
            store, mandate_id="mandate-x", owner_uid="attacker-uid", version=draft["version"], nonce=draft["nonce"]
        )


def test_cannot_revoke_another_users_mandate(seeded_store, authority_signer):
    store = seeded_store
    signer, _ = authority_signer
    approve_and_sign_mandate(store, signer, "mandate-y")

    with pytest.raises(MandateError):
        mandates.revoke_mandate(store, "mandate-y", owner_uid="attacker-uid")

    # Legitimate owner can still revoke afterwards — the attacker's failed
    # attempt did not corrupt state.
    result = mandates.revoke_mandate(store, "mandate-y", owner_uid="user-01")
    assert result["state"] == "REVOKED"


def test_cannot_reserve_grant_for_another_users_mandate(seeded_store, authority_signer):
    store = seeded_store
    signer, _ = authority_signer
    approve_and_sign_mandate(store, signer, "mandate-z")

    with pytest.raises(grants.GrantError):
        grants.reserve_job_and_grant(
            store, job_id="job-z", grant_id="grant-z", mandate_id="mandate-z",
            owner_uid="attacker-uid", runner_principal="runner-service-01",
        )


def test_cannot_revoke_another_users_grant(seeded_store, authority_signer):
    store = seeded_store
    signer, _ = authority_signer
    approve_and_sign_mandate(store, signer, "mandate-w")
    reserve_and_activate_grant(store, signer, job_id="job-w", grant_id="grant-w", mandate_id="mandate-w")

    with pytest.raises(grants.GrantError):
        grants.revoke_grant(store, "grant-w", owner_uid="attacker-uid")
