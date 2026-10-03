"""Job/grant reservation and activation. SPEC.md Section 6."""

from __future__ import annotations

import hashlib
import time

from services.shared.crypto import Signer
from services.shared.jws import encode_compact
from services.shared.normalize import canonical_bytes
from services.shared.schemas import GrantClaims
from services.shared.states import GrantState, JobStatus, MandateState, ObligationState, SigningState
from services.shared.store import Store

MANDATES = "mandates"
GRANTS = "grants"
JOBS = "jobs"
OBLIGATIONS = "obligations"

GRANT_AUD = "payguard-payment-gateway"


class GrantError(Exception):
    pass


def reserve_job_and_grant(
    store: Store,
    *,
    job_id: str,
    grant_id: str,
    mandate_id: str,
    owner_uid: str,
    runner_principal: str,
) -> dict:
    """`POST /jobs`: reserve one job + one PENDING_SIGNATURE grant (SPEC.md 6).

    Each approved mandate may issue at most one grant; repeated calls with
    the same ``job_id`` return the existing job/grant rather than erroring.
    """

    def txn(t):
        existing_job = t.get(JOBS, job_id)
        if existing_job is not None:
            return {"job": existing_job, "grant": t.get(GRANTS, existing_job["grant_id"])}

        mandate = t.get(MANDATES, mandate_id)
        if mandate is None:
            raise GrantError("mandate_not_found")
        if mandate["owner_uid"] != owner_uid:
            raise GrantError("not_owned")
        if mandate["state"] != MandateState.APPROVED.value:
            raise GrantError("mandate_not_approved")
        if mandate.get("issued_grant_id"):
            raise GrantError("mandate_already_issued_a_grant")

        obligation = t.get(OBLIGATIONS, mandate["obligation_id"])
        if obligation is None or obligation["state"] != ObligationState.UNPAID.value:
            raise GrantError("obligation_not_unpaid")
        if obligation.get("current_mandate_id") != mandate_id:
            raise GrantError("mandate_not_current_for_obligation")
        if obligation.get("current_grant_id"):
            existing_grant = t.get(GRANTS, obligation["current_grant_id"])
            if existing_grant and existing_grant["state"] in (
                GrantState.PENDING_SIGNATURE.value,
                GrantState.ACTIVE.value,
            ):
                raise GrantError("obligation_already_has_pending_or_active_grant")

        now = int(time.time())
        expiry = now + mandate["grant_expiry_seconds"]

        frozen_payload = {
            "iss": "payguard-authority",
            "aud": GRANT_AUD,
            "sub": owner_uid,
            "jti": grant_id,
            "iat": now,
            "nbf": now,
            "exp": expiry,
            "agent_principal": runner_principal,
            "mandate_id": mandate_id,
            "mandate_version": mandate["version"],
            "mandate_digest": mandate.get("digest") or "",
            "job_id": job_id,
            "obligation_id": mandate["obligation_id"],
            "action": "rent.pay",
            "debit_account_id": mandate["debit_account_id"],
            "destination_account_id": mandate["destination_account_id"],
            "destination_version": mandate["destination_version"],
            "amount_mode": mandate["amount_mode"],
            "amount_minor": mandate["amount_minor"],
            "currency": mandate["currency"],
            "payment_reference": mandate["payment_reference"],
            "max_executions": 1,
            "policy_version": mandate["policy_version"],
        }

        grant = {
            "grant_id": grant_id,
            "mandate_id": mandate_id,
            "mandate_digest": mandate.get("digest"),
            "job_id": job_id,
            "obligation_id": mandate["obligation_id"],
            "owner_uid": owner_uid,
            "principal": runner_principal,
            "frozen_payload": frozen_payload,
            "compact_jws": None,
            "token_hash": None,
            "expiry": expiry,
            "state": GrantState.PENDING_SIGNATURE.value,
        }
        t.set(GRANTS, grant_id, grant)

        job = {
            "job_id": job_id,
            "owner_uid": owner_uid,
            "mandate_id": mandate_id,
            "obligation_id": mandate["obligation_id"],
            "allowed_documents": list(mandate["selected_document_ids"]),
            "grant_id": grant_id,
            "runner": runner_principal,
            "status": JobStatus.WAITING_SIGNATURE.value,
        }
        t.set(JOBS, job_id, job)

        mandate = dict(mandate)
        mandate["issued_grant_id"] = grant_id
        t.set(MANDATES, mandate_id, mandate)

        obligation = dict(obligation)
        obligation["current_grant_id"] = grant_id
        t.set(OBLIGATIONS, mandate["obligation_id"], obligation)

        return {"job": job, "grant": grant}

    return store.run_transaction(txn)


def sign_and_activate_grant(store: Store, grant_id: str, signer: Signer) -> dict:
    """Sign outside a transaction, then recheck+persist+activate (SPEC.md 6).

    If the frozen token's ``exp`` has passed before this runs, activation is
    refused outright — never silently extended — and the caller must obtain
    a fresh confirmation (SPEC.md 5 "Destination changes", expiry recovery).
    """
    current = store.peek(GRANTS, grant_id)
    if current is None:
        raise GrantError("not_found")
    if current["state"] != GrantState.PENDING_SIGNATURE.value:
        return current

    claims = GrantClaims(**current["frozen_payload"])
    payload_bytes = canonical_bytes(claims.model_dump(mode="json"))
    token = encode_compact(
        typ="payguard-grant+jwt", kid=signer.kid, payload_bytes=payload_bytes, signer=signer
    )
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()

    def txn(t):
        now = int(time.time())
        grant = t.get(GRANTS, grant_id)
        if grant is None:
            raise GrantError("not_found")
        if grant["state"] != GrantState.PENDING_SIGNATURE.value:
            return grant  # lost the race to revocation, or already activated
        if grant["expiry"] <= now:
            raise GrantError("frozen_token_expired_before_activation")

        mandate = t.get(MANDATES, grant["mandate_id"])
        if (
            mandate is None
            or mandate["state"] != MandateState.APPROVED.value
            or mandate["signing_state"] != SigningState.SIGNED.value
        ):
            raise GrantError("mandate_not_signed_approved")

        obligation = t.get(OBLIGATIONS, grant["obligation_id"])
        if (
            obligation is None
            or obligation["state"] != ObligationState.UNPAID.value
            or obligation.get("current_grant_id") != grant_id
        ):
            raise GrantError("obligation_pointer_mismatch")

        job = t.get(JOBS, grant["job_id"])
        if job is None or job["status"] != JobStatus.WAITING_SIGNATURE.value:
            raise GrantError("job_not_waiting_signature")

        grant = dict(grant)
        grant["compact_jws"] = token
        grant["token_hash"] = token_hash
        grant["state"] = GrantState.ACTIVE.value
        t.set(GRANTS, grant_id, grant)

        job = dict(job)
        job["status"] = JobStatus.READY.value
        t.set(JOBS, grant["job_id"], job)

        return grant

    return store.run_transaction(txn)


def revoke_grant(store: Store, grant_id: str, owner_uid: str) -> dict:
    """`POST /grants/{id}/revoke`: revoke current mandate + pending/active grant."""

    def txn(t):
        # --- reads first ---
        grant = t.get(GRANTS, grant_id)
        if grant is None:
            raise GrantError("not_found")
        if grant["owner_uid"] != owner_uid:
            raise GrantError("not_owned")
        if grant["state"] == GrantState.CONSUMED.value:
            raise GrantError("already_consumed")
        if grant["state"] == GrantState.REVOKED.value:
            return grant

        mandate = t.get(MANDATES, grant["mandate_id"])
        obligation = t.get(OBLIGATIONS, grant["obligation_id"])

        # --- then writes ---
        grant = dict(grant)
        grant["state"] = GrantState.REVOKED.value
        t.set(GRANTS, grant_id, grant)

        if mandate is not None and mandate["state"] == MandateState.APPROVED.value:
            mandate = dict(mandate)
            mandate["state"] = MandateState.REVOKED.value
            t.set(MANDATES, grant["mandate_id"], mandate)

        if obligation is not None and obligation.get("current_grant_id") == grant_id:
            obligation = dict(obligation)
            obligation["current_grant_id"] = None
            obligation["current_mandate_id"] = None
            t.set(OBLIGATIONS, grant["obligation_id"], obligation)

        return grant

    return store.run_transaction(txn)
