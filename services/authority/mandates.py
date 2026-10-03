"""Mandate lifecycle: draft -> confirm (approve) -> sign -> revoke/replace.

SPEC.md Section 5 "Approval and mandate" and its "Signed mandate lifecycle".
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

from services.shared.crypto import Signer
from services.shared.digests import mandate_payload_digest_bytes
from services.shared.jws import encode_compact
from services.shared.normalize import canonical_bytes
from services.shared.schemas import MandatePayload
from services.shared.states import GrantState, MandateState, ObligationState, SigningState
from services.shared.store import Store

MANDATES = "mandates"
OBLIGATIONS = "obligations"
GRANTS = "grants"


class MandateError(Exception):
    pass


@dataclass
class DraftInput:
    mandate_id: str
    owner_uid: str
    instruction_id: str
    instruction_version: int
    obligation_id: str
    debit_account_id: str
    payee_id: str
    destination_account_id: str
    destination_version: int
    amount_mode: str
    amount_minor: int
    currency: str
    payment_reference: str
    grant_expiry_seconds: int
    selected_document_ids: tuple[str, ...]
    policy_version: str
    supersedes_mandate_id: str | None = None


def create_draft(store: Store, draft: DraftInput) -> dict:
    """Freeze a DRAFT snapshot with a confirmation nonce (lifecycle step 1).

    The server assigns and owns ``mandate_id``/``version``/nonce; the caller
    only supplies the consequential payment fields to display for approval.
    """
    nonce = secrets.token_urlsafe(24)

    def txn(t):
        existing = t.get(MANDATES, draft.mandate_id)
        if existing is not None:
            raise MandateError(f"mandate {draft.mandate_id} already exists")
        record = {
            "mandate_id": draft.mandate_id,
            "version": 1,
            "owner_uid": draft.owner_uid,
            "instruction_id": draft.instruction_id,
            "instruction_version": draft.instruction_version,
            "obligation_id": draft.obligation_id,
            "debit_account_id": draft.debit_account_id,
            "payee_id": draft.payee_id,
            "destination_account_id": draft.destination_account_id,
            "destination_version": draft.destination_version,
            "amount_mode": draft.amount_mode,
            "amount_minor": draft.amount_minor,
            "currency": draft.currency,
            "payment_reference": draft.payment_reference,
            "grant_expiry_seconds": draft.grant_expiry_seconds,
            "selected_document_ids": list(draft.selected_document_ids),
            "policy_version": draft.policy_version,
            "state": MandateState.DRAFT.value,
            "signing_state": None,
            "nonce": nonce,
            "nonce_used": False,
            "approved_at": None,
            "compact_jws": None,
            "digest": None,
            "key_id": None,
            "issued_grant_id": None,
            "execution_marker": None,
            "supersedes_mandate_id": draft.supersedes_mandate_id,
        }
        t.set(MANDATES, draft.mandate_id, record)
        return record

    return store.run_transaction(txn)


def confirm_mandate(store: Store, *, mandate_id: str, owner_uid: str, version: int, nonce: str) -> dict:
    """Atomically consume the nonce, record approval, start signing (step 2).

    Repeated confirmation with the (now-consumed) nonce returns the existing
    approved status rather than erroring, so a client retry after a lost
    response is safe.
    """

    def txn(t):
        mandate = t.get(MANDATES, mandate_id)
        if mandate is None:
            raise MandateError("not_found")
        if mandate["owner_uid"] != owner_uid:
            raise MandateError("not_owned")
        if mandate["version"] != version:
            raise MandateError("version_mismatch")
        if mandate["state"] == MandateState.APPROVED.value:
            return mandate  # idempotent recovery of an existing confirmation
        if mandate["state"] != MandateState.DRAFT.value:
            raise MandateError(f"cannot confirm mandate in state {mandate['state']}")
        if mandate["nonce_used"] or mandate["nonce"] != nonce:
            raise MandateError("invalid_or_used_nonce")

        is_initial = mandate["supersedes_mandate_id"] is None
        obligation = t.get(OBLIGATIONS, mandate["obligation_id"])

        old_mandate = None
        old_grant = None
        if is_initial:
            if obligation is not None and obligation.get("current_mandate_id"):
                raise MandateError("obligation_already_has_current_mandate")
        else:
            # Replacement path (SPEC.md 5 "Destination changes"): the
            # obligation must still be unpaid and its current mandate must
            # be the one this draft claims to replace.
            if obligation is None or obligation["state"] != ObligationState.UNPAID.value:
                raise MandateError("OBLIGATION_PAID")
            old_mandate_id = mandate["supersedes_mandate_id"]
            if obligation.get("current_mandate_id") != old_mandate_id:
                raise MandateError("not_current_mandate_for_obligation")
            old_mandate = t.get(MANDATES, old_mandate_id)
            if old_mandate is None:
                raise MandateError("superseded_mandate_not_found")
            if old_mandate["state"] != MandateState.APPROVED.value:
                raise MandateError("superseded_mandate_not_approved")
            old_grant_id = old_mandate.get("issued_grant_id")
            if old_grant_id:
                old_grant = t.get(GRANTS, old_grant_id)

        mandate = dict(mandate)
        mandate["nonce_used"] = True
        mandate["approved_at"] = int(time.time())
        mandate["state"] = MandateState.APPROVED.value
        mandate["signing_state"] = SigningState.PENDING.value
        t.set(MANDATES, mandate_id, mandate)

        if is_initial:
            if obligation is None:
                obligation = {
                    "obligation_id": mandate["obligation_id"],
                    "state": ObligationState.UNPAID.value,
                    "payment_id": None,
                    "current_mandate_id": None,
                    "current_grant_id": None,
                }
            obligation = dict(obligation)
            obligation["current_mandate_id"] = mandate_id
            t.set(OBLIGATIONS, mandate["obligation_id"], obligation)
        else:
            old_mandate = dict(old_mandate)
            old_mandate["state"] = MandateState.SUPERSEDED.value
            t.set(MANDATES, old_mandate["mandate_id"], old_mandate)

            if old_grant is not None and old_grant["state"] in (
                GrantState.PENDING_SIGNATURE.value,
                GrantState.ACTIVE.value,
            ):
                old_grant = dict(old_grant)
                old_grant["state"] = GrantState.REVOKED.value
                t.set(GRANTS, old_grant["grant_id"], old_grant)

            obligation = dict(obligation)
            obligation["current_mandate_id"] = mandate_id
            obligation["current_grant_id"] = None
            t.set(OBLIGATIONS, mandate["obligation_id"], obligation)

        return mandate

    return store.run_transaction(txn)


def build_mandate_payload(mandate: dict) -> MandatePayload:
    return MandatePayload(
        mandate_id=mandate["mandate_id"],
        mandate_version=mandate["version"],
        user_id=mandate["owner_uid"],
        instruction_id=mandate["instruction_id"],
        instruction_version=mandate["instruction_version"],
        obligation_id=mandate["obligation_id"],
        debit_account_id=mandate["debit_account_id"],
        payee_id=mandate["payee_id"],
        destination_account_id=mandate["destination_account_id"],
        destination_version=mandate["destination_version"],
        amount_mode=mandate["amount_mode"],
        amount_minor=mandate["amount_minor"],
        currency=mandate["currency"],
        payment_reference=mandate["payment_reference"],
        grant_expiry_seconds=mandate["grant_expiry_seconds"],
        selected_document_ids=tuple(mandate["selected_document_ids"]),
        policy_version=mandate["policy_version"],
        approved_at=mandate["approved_at"],
    )


def sign_mandate(store: Store, mandate_id: str, signer: Signer) -> dict:
    """Sign outside any transaction, then compare-and-set persist (steps 3-4).

    If revocation or supersession wins the race before this compare-and-set,
    the freshly produced signature is simply discarded (never persisted) —
    this function returns the mandate's current (revoked/superseded) state.
    """
    current = store.peek(MANDATES, mandate_id)
    if current is None:
        raise MandateError("not_found")
    if current["state"] != MandateState.APPROVED.value or current["signing_state"] != SigningState.PENDING.value:
        return current

    payload = build_mandate_payload(current)
    payload_dump = payload.model_dump(mode="json")
    payload_bytes = canonical_bytes(payload_dump)
    digest_hex = "sha256:" + mandate_payload_digest_bytes(payload).hex()
    token = encode_compact(
        typ="payguard-mandate+jwt", kid=signer.kid, payload_bytes=payload_bytes, signer=signer
    )

    def txn(t):
        mandate = t.get(MANDATES, mandate_id)
        if mandate is None:
            raise MandateError("not_found")
        if mandate["state"] != MandateState.APPROVED.value or mandate["signing_state"] != SigningState.PENDING.value:
            return mandate
        mandate = dict(mandate)
        mandate["compact_jws"] = token
        mandate["digest"] = digest_hex
        mandate["key_id"] = signer.kid
        mandate["signing_state"] = SigningState.SIGNED.value
        t.set(MANDATES, mandate_id, mandate)
        return mandate

    return store.run_transaction(txn)


def revoke_mandate(store: Store, mandate_id: str, owner_uid: str) -> dict:
    """Revoke current approval and its pending/active grant, even pre-signing."""

    def txn(t):
        # --- reads first ---
        mandate = t.get(MANDATES, mandate_id)
        if mandate is None:
            raise MandateError("not_found")
        if mandate["owner_uid"] != owner_uid:
            raise MandateError("not_owned")
        if mandate["state"] in (MandateState.REVOKED.value, MandateState.SUPERSEDED.value):
            return mandate

        grant = None
        grant_id = mandate.get("issued_grant_id")
        if grant_id:
            grant = t.get(GRANTS, grant_id)

        obligation = t.get(OBLIGATIONS, mandate["obligation_id"])

        # --- then writes ---
        mandate = dict(mandate)
        mandate["state"] = MandateState.REVOKED.value
        t.set(MANDATES, mandate_id, mandate)

        if grant is not None and grant["state"] in (
            GrantState.PENDING_SIGNATURE.value,
            GrantState.ACTIVE.value,
        ):
            grant = dict(grant)
            grant["state"] = GrantState.REVOKED.value
            t.set(GRANTS, grant_id, grant)

        if obligation is not None and obligation.get("current_mandate_id") == mandate_id:
            obligation = dict(obligation)
            obligation["current_mandate_id"] = None
            obligation["current_grant_id"] = None
            t.set(OBLIGATIONS, mandate["obligation_id"], obligation)

        return mandate

    return store.run_transaction(txn)
