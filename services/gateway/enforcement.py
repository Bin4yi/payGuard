"""The payment gateway decision procedure. SPEC.md Section 7.

This is the only code path that can move money (PROJECT.md Section 3, step
3). Everything here is deterministic: no Gemini call, no KMS *signing* call
(only local public-key *verification*, which needs no network round trip and
is therefore safe inside a transaction callback), and no branch that widens
scope based on document or model content.

Decision procedure, mirroring SPEC.md Section 7 exactly:

1. Verify grant signature/type/issuer/audience/claims/lifetime and the
   authenticated caller principal, *before* any private record is read.
2. Load the grant record and the mandate it names; verify the stored mandate
   JWS; compute/compare digests.
3. Require agreement among the actual request, the signed grant claims, the
   stored grant record and the signed mandate snapshot.
4. Re-read everything inside one store transaction and recheck bindings,
   tuple equality, state and time; if the previously-verified digests turn
   out stale, abort and restart the whole procedure (not just the DB retry).
5. Idempotency-key replay and already-paid-obligation replay are read-only;
   they can never produce a second transfer.
6. New execution requires an ACTIVE grant, a SIGNED+APPROVED mandate, correct
   obligation pointers, a READY/RUNNING job, an UNPAID obligation, an
   unexpired grant, a resolvable destination and sufficient funds.
7. Commit debit/credit/transfer/consumption/idempotency/attempt/outbox atomically.
8. Every authenticated decision (allow or block) persists its evidence in the
   same transaction that decided it.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass

from services.shared.crypto import VerifierRegistry
from services.shared.digests import payment_tuple_digest, payment_tuple_from_request
from services.shared.jws import JwsError, verify_compact
from services.shared.reason_codes import ReasonCode
from services.shared.schemas import AttemptRecord, GrantClaims, MandatePayload, PaymentRequest
from services.shared.states import GrantState, JobStatus, MandateState, ObligationState, SigningState
from services.shared.store import Store, Transaction

MANDATES = "mandates"
GRANTS = "grants"
JOBS = "jobs"
OBLIGATIONS = "obligations"
DESTINATIONS = "destinations"
ACCOUNTS = "accounts"
IDEMPOTENCY = "idempotency"
PAYMENT_ATTEMPTS = "payment_attempts"
LEDGER_TRANSFERS = "ledger_transfers"
WORKFLOW_EVENTS = "workflow_events"
WORKFLOW_SEQ = "workflow_seq"

POLICY_VERSION = "rent-v1"
GRANT_AUD = "payguard-payment-gateway"
GRANT_ISS = "payguard-authority"
CLOCK_SKEW_SECONDS = 30
MAX_REVERIFY_ATTEMPTS = 8


class Denial(Exception):
    """An authorization decision to block, carrying its reason code.

    ``owner_uid``/``job_id``/``mandate_id``/``grant_id`` are populated once
    known, so the caller can decide whether owned evidence can be persisted
    (SPEC.md Section 7: unauthenticated malformed traffic has no owner to
    persist evidence under and may rely on infrastructure logs instead).
    """

    def __init__(self, reason: ReasonCode, *, owner_uid: str | None = None):
        super().__init__(reason.value)
        self.reason = reason
        self.owner_uid = owner_uid


class NeedsReverification(Exception):
    """A digest verified outside the transaction no longer matches stored state."""


@dataclass(frozen=True)
class ExecutionResult:
    status: str  # "COMMITTED" | "BLOCKED"
    reason_codes: tuple[str, ...]
    payment_id: str | None
    attempt_id: str | None
    receipt_status: str | None  # "PENDING" when COMMITTED


@dataclass
class _OutsideVerification:
    claims: GrantClaims
    grant_record: dict
    mandate_record: dict
    mandate_payload: MandatePayload
    mandate_digest: str
    token_hash: str


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _verify_grant_credential(
    *,
    grant_token: str,
    authenticated_principal: str,
    verifier_registry: VerifierRegistry,
    now: float,
) -> GrantClaims:
    """Step 1: reject invalid credentials before any private task read."""
    try:
        verified = verify_compact(grant_token, expected_typ="payguard-grant+jwt", registry=verifier_registry)
    except JwsError:
        raise Denial(ReasonCode.INVALID_GRANT)

    try:
        claims_dict = json.loads(verified.payload_bytes)
        claims = GrantClaims(**claims_dict)
    except Exception:
        raise Denial(ReasonCode.INVALID_GRANT)

    if claims.iss != GRANT_ISS or claims.aud != GRANT_AUD or claims.policy_version != POLICY_VERSION:
        raise Denial(ReasonCode.INVALID_GRANT)

    if claims.iat > now + CLOCK_SKEW_SECONDS or claims.nbf > now + CLOCK_SKEW_SECONDS:
        raise Denial(ReasonCode.INVALID_GRANT)

    if now > claims.exp:
        raise Denial(ReasonCode.GRANT_EXPIRED)

    # Never trust a caller-supplied principal string: compare against the
    # verified service identity the runtime attached to this request.
    if authenticated_principal != claims.agent_principal:
        raise Denial(ReasonCode.PRINCIPAL_MISMATCH)

    return claims


def _verify_outside_transaction(
    store: Store,
    *,
    claims: GrantClaims,
    grant_token: str,
    verifier_registry: VerifierRegistry,
) -> _OutsideVerification:
    """Steps 2-3: load grant/mandate, verify the stored mandate JWS, bind them."""
    grant_record = store.peek(GRANTS, claims.jti)
    if grant_record is None:
        raise Denial(ReasonCode.INVALID_GRANT)

    owner_uid = grant_record.get("owner_uid")

    token_hash = _sha256_hex(grant_token.encode("ascii"))
    if (
        grant_record["job_id"] != claims.job_id
        or grant_record["mandate_id"] != claims.mandate_id
        or grant_record["obligation_id"] != claims.obligation_id
        or grant_record["principal"] != claims.agent_principal
        or grant_record.get("token_hash") != token_hash
    ):
        raise Denial(ReasonCode.INVALID_GRANT, owner_uid=owner_uid)

    mandate_record = store.peek(MANDATES, claims.mandate_id)
    if mandate_record is None or not mandate_record.get("compact_jws"):
        raise Denial(ReasonCode.INVALID_GRANT, owner_uid=owner_uid)

    try:
        verified_mandate = verify_compact(
            mandate_record["compact_jws"], expected_typ="payguard-mandate+jwt", registry=verifier_registry
        )
        mandate_payload = MandatePayload(**json.loads(verified_mandate.payload_bytes))
    except Exception:
        raise Denial(ReasonCode.INVALID_GRANT, owner_uid=owner_uid)

    from services.shared.digests import mandate_payload_digest

    mandate_digest = mandate_payload_digest(mandate_payload)
    if (
        mandate_digest != claims.mandate_digest
        or mandate_digest != mandate_record.get("digest")
        or mandate_digest != grant_record.get("mandate_digest")
        or mandate_payload.mandate_version != claims.mandate_version
        or mandate_payload.mandate_id != claims.mandate_id
        or mandate_payload.user_id != owner_uid
    ):
        raise Denial(ReasonCode.INVALID_GRANT, owner_uid=owner_uid)

    return _OutsideVerification(
        claims=claims,
        grant_record=grant_record,
        mandate_record=mandate_record,
        mandate_payload=mandate_payload,
        mandate_digest=mandate_digest,
        token_hash=token_hash,
    )


def _check_tuple_binding(outside: _OutsideVerification, request: PaymentRequest) -> None:
    """Step 3: actual request vs. signed grant claims vs. signed mandate snapshot."""
    claims = outside.claims
    payload = outside.mandate_payload
    owner_uid = outside.grant_record.get("owner_uid")

    if request.job_id != claims.job_id or request.mandate_id != claims.mandate_id:
        raise Denial(ReasonCode.INVALID_GRANT, owner_uid=owner_uid)
    if request.obligation_id != claims.obligation_id or request.obligation_id != payload.obligation_id:
        raise Denial(ReasonCode.INVALID_GRANT, owner_uid=owner_uid)

    if request.destination_account_id != payload.destination_account_id or (
        request.destination_version != payload.destination_version
    ):
        raise Denial(ReasonCode.DESTINATION_CHANGED, owner_uid=owner_uid)

    if (
        request.debit_account_id != payload.debit_account_id
        or request.amount_minor != payload.amount_minor
        or request.currency != payload.currency
        or request.payment_reference != payload.payment_reference
    ):
        raise Denial(ReasonCode.SCOPE_MISMATCH, owner_uid=owner_uid)

    if claims.debit_account_id != payload.debit_account_id or claims.amount_minor != payload.amount_minor:
        raise Denial(ReasonCode.SCOPE_MISMATCH, owner_uid=owner_uid)

    if payload.max_executions != 1 or claims.max_executions != 1:
        raise Denial(ReasonCode.INVALID_GRANT, owner_uid=owner_uid)


def _read_next_sequence(t: Transaction, workflow_id: str) -> int:
    """Read-only: the sequence this transaction *would* append at, if it
    writes an event. Must be called during the read phase, before any
    ``t.set``/``t.delete`` in the same transaction attempt (the in-memory
    store enforces reads-before-writes, mirroring the Firestore rule)."""
    seq_doc = t.get(WORKFLOW_SEQ, workflow_id)
    return 1 if seq_doc is None else seq_doc["next_sequence"]


def _append_event(t: Transaction, *, workflow_id: str, sequence: int, event_type: str, payload: dict) -> None:
    """Write-phase only: advance the sequence counter and append the event."""
    t.set(WORKFLOW_SEQ, workflow_id, {"next_sequence": sequence + 1})
    event_id = f"{workflow_id}:{sequence}"
    t.set(
        WORKFLOW_EVENTS,
        event_id,
        {
            "workflow_id": workflow_id,
            "sequence": sequence,
            "event_type": event_type,
            "producer": "payment-gateway",
            "payload": payload,
            "signing_state": "PENDING",
        },
    )


def _persist_attempt(
    t: Transaction,
    *,
    attempt_id: str,
    owner_uid: str,
    request: PaymentRequest,
    idempotency_key: str,
    grant_id: str,
    request_digest: str,
    decision: str,
    reason_codes: tuple[str, ...],
) -> str:
    record = AttemptRecord(
        owner_uid=owner_uid,
        attempt_id=attempt_id,
        job_id=request.job_id,
        mandate_id=request.mandate_id,
        grant_id=grant_id,
        idempotency_key=idempotency_key,
        request=request,
        request_digest=request_digest,
    )
    from services.shared.digests import attempt_record_digest

    attempt_digest = attempt_record_digest(record)
    t.set(
        PAYMENT_ATTEMPTS,
        attempt_id,
        {
            "attempt_id": attempt_id,
            "owner_uid": owner_uid,
            "grant_id": grant_id,
            "idempotency_key": idempotency_key,
            "request": request.model_dump(mode="json"),
            "request_digest": request_digest,
            "attempt_digest": attempt_digest,
            "decision": decision,
            "reason_codes": list(reason_codes),
        },
    )
    return attempt_digest


def _receipt_payload(
    *,
    attempt_id: str,
    grant_id: str,
    mandate_digest: str | None,
    request_digest: str,
    attempt_digest: str,
    decision: str,
    reason_codes: tuple[str, ...],
    payment_id: str | None,
) -> dict:
    """Everything the receipt worker needs to sign a payguard-receipt+jwt
    event without re-deriving it from other collections (SPEC.md Section 9).
    """
    return {
        "attempt_id": attempt_id,
        "grant_id": grant_id,
        "mandate_digest": mandate_digest,
        "request_digest": request_digest,
        "attempt_digest": attempt_digest,
        "decision": decision,
        "reason_codes": list(reason_codes),
        "payment_id": payment_id,
    }


def _blocked(
    t: Transaction,
    *,
    attempt_id: str,
    owner_uid: str,
    request: PaymentRequest,
    idempotency_key: str,
    grant_id: str,
    request_digest: str,
    reason: ReasonCode,
    workflow_id: str,
    sequence: int,
    mandate_digest: str | None = None,
) -> ExecutionResult:
    attempt_digest = _persist_attempt(
        t,
        attempt_id=attempt_id,
        owner_uid=owner_uid,
        request=request,
        idempotency_key=idempotency_key,
        grant_id=grant_id,
        request_digest=request_digest,
        decision="BLOCKED",
        reason_codes=(reason.value,),
    )
    _append_event(
        t,
        workflow_id=workflow_id,
        sequence=sequence,
        event_type="payment.blocked",
        payload=_receipt_payload(
            attempt_id=attempt_id,
            grant_id=grant_id,
            mandate_digest=mandate_digest,
            request_digest=request_digest,
            attempt_digest=attempt_digest,
            decision="BLOCK",
            reason_codes=(reason.value,),
            payment_id=None,
        ),
    )
    return ExecutionResult(
        status="BLOCKED",
        reason_codes=(reason.value,),
        payment_id=None,
        attempt_id=attempt_id,
        receipt_status=None,
    )


def execute_payment(
    store: Store,
    *,
    request: PaymentRequest,
    grant_token: str,
    idempotency_key: str,
    authenticated_principal: str,
    verifier_registry: VerifierRegistry,
    now_fn=time.time,
    attempt_id_factory=lambda: secrets.token_urlsafe(16),
) -> ExecutionResult:
    for _ in range(MAX_REVERIFY_ATTEMPTS):
        now = now_fn()
        try:
            claims = _verify_grant_credential(
                grant_token=grant_token,
                authenticated_principal=authenticated_principal,
                verifier_registry=verifier_registry,
                now=now,
            )
            outside = _verify_outside_transaction(
                store, claims=claims, grant_token=grant_token, verifier_registry=verifier_registry
            )
            _check_tuple_binding(outside, request)
        except Denial as denial:
            if denial.owner_uid is None:
                # Unauthenticated / structurally invalid credential: no owned
                # context to bind evidence to (SPEC.md Section 7).
                return ExecutionResult(
                    status="BLOCKED",
                    reason_codes=(denial.reason.value,),
                    payment_id=None,
                    attempt_id=None,
                    receipt_status=None,
                )
            owner_uid = denial.owner_uid
            request_tuple = payment_tuple_from_request(
                request, obligation_id=request.obligation_id, amount_mode="exact"
            )
            request_digest = payment_tuple_digest(request_tuple)
            attempt_id = attempt_id_factory()

            def txn(t, denial=denial, owner_uid=owner_uid, request_digest=request_digest, attempt_id=attempt_id):
                sequence = _read_next_sequence(t, request.job_id)
                return _blocked(
                    t,
                    attempt_id=attempt_id,
                    owner_uid=owner_uid,
                    request=request,
                    idempotency_key=idempotency_key,
                    grant_id=claims.jti,
                    request_digest=request_digest,
                    reason=denial.reason,
                    workflow_id=request.job_id,
                    sequence=sequence,
                )

            return store.run_transaction(txn)

        try:
            result = store.run_transaction(
                lambda t: _decide_and_commit(
                    t,
                    outside=outside,
                    request=request,
                    idempotency_key=idempotency_key,
                    now=now_fn(),
                    attempt_id=attempt_id_factory(),
                )
            )
        except NeedsReverification:
            continue
        return result

    raise RuntimeError("exceeded reverification attempts without converging")


def _decide_and_commit(
    t: Transaction,
    *,
    outside: _OutsideVerification,
    request: PaymentRequest,
    idempotency_key: str,
    now: float,
    attempt_id: str,
) -> ExecutionResult:
    claims = outside.claims
    payload = outside.mandate_payload
    owner_uid = outside.grant_record.get("owner_uid")
    workflow_id = request.job_id

    # --- Finish all reads before any write. ---
    grant = t.get(GRANTS, claims.jti)
    mandate = t.get(MANDATES, claims.mandate_id)
    job = t.get(JOBS, request.job_id)
    obligation = t.get(OBLIGATIONS, request.obligation_id)
    destination = t.get(DESTINATIONS, f"{payload.destination_account_id}:{payload.destination_version}")
    debit_account = t.get(ACCOUNTS, payload.debit_account_id)
    credit_account_id = destination["demo_account_id"] if destination is not None else None
    credit_account = t.get(ACCOUNTS, credit_account_id) if credit_account_id is not None else None
    idem_key = f"{owner_uid}:{request.obligation_id}:{idempotency_key}"
    idem_record = t.get(IDEMPOTENCY, idem_key)
    existing_transfer = (
        t.get(LEDGER_TRANSFERS, obligation["payment_id"])
        if obligation is not None and obligation.get("payment_id")
        else None
    )
    sequence = _read_next_sequence(t, workflow_id)

    if grant is None or mandate is None or job is None or obligation is None:
        raise NeedsReverification()

    # The exact signed payload/JWS digests verified outside must be unchanged.
    if (
        grant.get("token_hash") != outside.token_hash
        or grant.get("mandate_digest") != outside.mandate_digest
        or mandate.get("digest") != outside.mandate_digest
        or mandate.get("compact_jws") != outside.mandate_record.get("compact_jws")
    ):
        raise NeedsReverification()

    request_tuple = payment_tuple_from_request(
        request, obligation_id=request.obligation_id, amount_mode=payload.amount_mode
    )
    request_digest = payment_tuple_digest(request_tuple)

    def block(reason: ReasonCode) -> ExecutionResult:
        return _blocked(
            t,
            attempt_id=attempt_id,
            owner_uid=owner_uid,
            request=request,
            idempotency_key=idempotency_key,
            grant_id=claims.jti,
            request_digest=request_digest,
            reason=reason,
            workflow_id=workflow_id,
            sequence=sequence,
            mandate_digest=outside.mandate_digest,
        )

    # --- Step 5: idempotency-key replay is read-only, never a second debit. ---
    if idem_record is not None:
        if idem_record["request_digest"] != request_digest:
            return block(ReasonCode.IDEMPOTENCY_CONFLICT)
        return ExecutionResult(
            status="COMMITTED",
            reason_codes=(),
            payment_id=idem_record["payment_id"],
            attempt_id=attempt_id,
            receipt_status="PENDING",
        )

    # --- Already-paid obligation: only a matching authenticated retry may
    # observe the existing result; anything else is a conflict, never a
    # second transfer.
    if obligation["state"] == ObligationState.PAID.value:
        transfer = existing_transfer
        if (
            transfer is not None
            and transfer["request_digest"] == request_digest
            and transfer["job_id"] == request.job_id
            and transfer["mandate_id"] == request.mandate_id
            and transfer["grant_id"] == claims.jti
        ):
            t.set(
                IDEMPOTENCY,
                idem_key,
                {
                    "key": idempotency_key,
                    "owner_uid": owner_uid,
                    "obligation_id": request.obligation_id,
                    "request_digest": request_digest,
                    "payment_id": obligation["payment_id"],
                },
            )
            return ExecutionResult(
                status="COMMITTED",
                reason_codes=(),
                payment_id=obligation["payment_id"],
                attempt_id=attempt_id,
                receipt_status="PENDING",
            )
        return block(ReasonCode.OBLIGATION_PAID)

    # --- Step 6: fresh-execution eligibility. ---
    if grant["state"] == GrantState.REVOKED.value:
        return block(ReasonCode.GRANT_REVOKED)
    if grant["state"] != GrantState.ACTIVE.value:
        return block(ReasonCode.INVALID_GRANT)
    if grant["expiry"] <= now:
        return block(ReasonCode.GRANT_EXPIRED)

    if mandate["state"] in (MandateState.REVOKED.value, MandateState.SUPERSEDED.value):
        return block(ReasonCode.GRANT_REVOKED)
    if mandate["state"] != MandateState.APPROVED.value or mandate["signing_state"] != SigningState.SIGNED.value:
        return block(ReasonCode.INVALID_GRANT)

    if (
        obligation.get("current_mandate_id") != claims.mandate_id
        or obligation.get("current_grant_id") != claims.jti
    ):
        return block(ReasonCode.INVALID_GRANT)

    if job["status"] not in (JobStatus.READY.value, JobStatus.RUNNING.value):
        return block(ReasonCode.INVALID_GRANT)

    if destination is None or credit_account_id is None:
        return block(ReasonCode.DESTINATION_CHANGED)

    if debit_account is None or debit_account["currency"] != request.currency:
        return block(ReasonCode.INSUFFICIENT_FUNDS)
    if debit_account["balance_minor"] < request.amount_minor:
        return block(ReasonCode.INSUFFICIENT_FUNDS)

    if credit_account is None or credit_account["currency"] != request.currency:
        return block(ReasonCode.DESTINATION_CHANGED)

    # --- Step 7: commit the transfer, consumption and evidence atomically. ---
    payment_id = f"pay-{attempt_id}"

    debit_account = dict(debit_account)
    debit_account["balance_minor"] -= request.amount_minor
    t.set(ACCOUNTS, payload.debit_account_id, debit_account)

    credit_account = dict(credit_account)
    credit_account["balance_minor"] += request.amount_minor
    t.set(ACCOUNTS, credit_account_id, credit_account)

    t.set(
        LEDGER_TRANSFERS,
        payment_id,
        {
            "payment_id": payment_id,
            "debit_account_id": payload.debit_account_id,
            "credit_account_id": credit_account_id,
            "amount_minor": request.amount_minor,
            "currency": request.currency,
            "obligation_id": request.obligation_id,
            "job_id": request.job_id,
            "mandate_id": request.mandate_id,
            "grant_id": claims.jti,
            "request_digest": request_digest,
        },
    )

    obligation = dict(obligation)
    obligation["state"] = ObligationState.PAID.value
    obligation["payment_id"] = payment_id
    t.set(OBLIGATIONS, request.obligation_id, obligation)

    grant = dict(grant)
    grant["state"] = GrantState.CONSUMED.value
    t.set(GRANTS, claims.jti, grant)

    mandate = dict(mandate)
    mandate["execution_marker"] = payment_id
    t.set(MANDATES, claims.mandate_id, mandate)

    job = dict(job)
    job["status"] = JobStatus.DONE.value
    t.set(JOBS, request.job_id, job)

    t.set(
        IDEMPOTENCY,
        idem_key,
        {
            "key": idempotency_key,
            "owner_uid": owner_uid,
            "obligation_id": request.obligation_id,
            "request_digest": request_digest,
            "payment_id": payment_id,
        },
    )

    attempt_digest = _persist_attempt(
        t,
        attempt_id=attempt_id,
        owner_uid=owner_uid,
        request=request,
        idempotency_key=idempotency_key,
        grant_id=claims.jti,
        request_digest=request_digest,
        decision="COMMITTED",
        reason_codes=(),
    )
    _append_event(
        t,
        workflow_id=workflow_id,
        sequence=sequence,
        event_type="payment.committed",
        payload=_receipt_payload(
            attempt_id=attempt_id,
            grant_id=claims.jti,
            mandate_digest=outside.mandate_digest,
            request_digest=request_digest,
            attempt_digest=attempt_digest,
            decision="ALLOW",
            reason_codes=(),
            payment_id=payment_id,
        ),
    )

    return ExecutionResult(
        status="COMMITTED",
        reason_codes=(),
        payment_id=payment_id,
        attempt_id=attempt_id,
        receipt_status="PENDING",
    )
