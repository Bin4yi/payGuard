"""SPEC.md Section 13 "Controlled configurations" A and B: an authenticated,
isolated payment simulator that keeps every control from the real gateway
EXCEPT the exact payment-tuple-to-mandate-scope comparison.

**This module must never be reachable from the public demo API, never
wired to any real payment credential, and never exposed outside the
evaluation harness** (SPEC.md Section 13: "Never expose it through the
public demo or connect it to real payment credentials"). It exists solely
so configurations A and B have a meaningful, non-strawman baseline —
without it, "the gateway blocked the attack" would be trivially true of
any baseline that also happens to reject every out-of-scope request for
unrelated reasons.

Design choice: this is a **separate function**, not a flag inside
``services/gateway/enforcement.py``. The real gateway is the actual,
deployed payment boundary and must never grow a "skip the scope check"
parameter, not even a test-only one that defaults to safe — a
security-critical module is safer with no such switch to ever misconfigure
than with one more branch to audit forever. Everything that is *not* the
scope comparison is reused directly from ``enforcement.py`` (its exact
credential verification, idempotency, and persistence helpers), specifically
so that "credential validation, ownership/job binding, signing lifecycle,
expiry, revocation, idempotency and obligation duplicate controls remain
identical" (SPEC.md Section 13) is true by construction, not by promise.
"""

from __future__ import annotations

import time

from services.gateway import enforcement as gw
from services.gateway.enforcement import Denial, ExecutionResult, NeedsReverification
from services.shared.crypto import VerifierRegistry
from services.shared.digests import payment_tuple_digest, payment_tuple_from_request
from services.shared.reason_codes import ReasonCode
from services.shared.schemas import PaymentRequest
from services.shared.states import GrantState, JobStatus, MandateState, ObligationState, SigningState
from services.shared.store import Store, Transaction


def execute_payment_broad(
    store: Store,
    *,
    request: PaymentRequest,
    grant_token: str,
    idempotency_key: str,
    authenticated_principal: str,
    verifier_registry: VerifierRegistry,
    now_fn=time.time,
    attempt_id_factory=None,
) -> ExecutionResult:
    """Configuration A/B payment path: identical credential/replay/duplicate
    controls to ``enforcement.execute_payment``, but the actual debit
    account, credited destination, amount and reference come from the
    *request* rather than being required to equal the approved mandate's —
    i.e. the one check this function does NOT perform is "does this attempt
    match what the user approved."
    """
    import secrets

    attempt_id_factory = attempt_id_factory or (lambda: secrets.token_urlsafe(16))

    for _ in range(gw.MAX_REVERIFY_ATTEMPTS):
        now = now_fn()
        try:
            claims = gw._verify_grant_credential(
                grant_token=grant_token, authenticated_principal=authenticated_principal,
                verifier_registry=verifier_registry, now=now,
            )
            outside = gw._verify_outside_transaction(
                store, claims=claims, grant_token=grant_token, verifier_registry=verifier_registry
            )
            # Ownership/job binding only — deliberately NOT gw._check_tuple_binding,
            # which is the exact-scope comparison this configuration omits.
            if request.job_id != claims.job_id or request.mandate_id != claims.mandate_id:
                raise Denial(ReasonCode.INVALID_GRANT, owner_uid=outside.grant_record.get("owner_uid"))
            if request.obligation_id != claims.obligation_id:
                raise Denial(ReasonCode.INVALID_GRANT, owner_uid=outside.grant_record.get("owner_uid"))
        except Denial as denial:
            if denial.owner_uid is None:
                return ExecutionResult(status="BLOCKED", reason_codes=(denial.reason.value,), payment_id=None, attempt_id=None, receipt_status=None)
            owner_uid = denial.owner_uid
            request_tuple = payment_tuple_from_request(request, obligation_id=request.obligation_id, amount_mode="exact")
            request_digest = payment_tuple_digest(request_tuple)
            attempt_id = attempt_id_factory()

            def txn(t, denial=denial, owner_uid=owner_uid, request_digest=request_digest, attempt_id=attempt_id):
                sequence = gw._read_next_sequence(t, request.job_id)
                return gw._blocked(
                    t, attempt_id=attempt_id, owner_uid=owner_uid, request=request, idempotency_key=idempotency_key,
                    grant_id=claims.jti, request_digest=request_digest, reason=denial.reason,
                    workflow_id=request.job_id, sequence=sequence,
                )

            return store.run_transaction(txn)

        try:
            result = store.run_transaction(
                lambda t: _decide_and_commit_broad(
                    t, outside=outside, request=request, idempotency_key=idempotency_key, now=now_fn(),
                    attempt_id=attempt_id_factory(),
                )
            )
        except NeedsReverification:
            continue
        return result

    raise RuntimeError("exceeded reverification attempts without converging")


def _decide_and_commit_broad(
    t: Transaction, *, outside, request: PaymentRequest, idempotency_key: str, now: float, attempt_id: str,
) -> ExecutionResult:
    claims = outside.claims
    payload = outside.mandate_payload
    owner_uid = outside.grant_record.get("owner_uid")
    workflow_id = request.job_id

    grant = t.get(gw.GRANTS, claims.jti)
    mandate = t.get(gw.MANDATES, claims.mandate_id)
    job = t.get(gw.JOBS, request.job_id)
    obligation = t.get(gw.OBLIGATIONS, request.obligation_id)
    # Broad mode resolves the destination from the REQUEST, not the mandate
    # — this is the entire point: an "otherwise valid alternate synthetic
    # destination" must be payable here for A/B to be a meaningful baseline.
    destination = t.get(gw.DESTINATIONS, f"{request.destination_account_id}:{request.destination_version}")
    debit_account = t.get(gw.ACCOUNTS, request.debit_account_id)
    credit_account_id = destination["demo_account_id"] if destination is not None else None
    credit_account = t.get(gw.ACCOUNTS, credit_account_id) if credit_account_id is not None else None
    idem_key = f"{owner_uid}:{request.obligation_id}:{idempotency_key}"
    idem_record = t.get(gw.IDEMPOTENCY, idem_key)
    existing_transfer = (
        t.get(gw.LEDGER_TRANSFERS, obligation["payment_id"]) if obligation is not None and obligation.get("payment_id") else None
    )
    sequence = gw._read_next_sequence(t, workflow_id)

    if grant is None or mandate is None or job is None or obligation is None:
        raise NeedsReverification()
    if (
        grant.get("token_hash") != outside.token_hash
        or grant.get("mandate_digest") != outside.mandate_digest
        or mandate.get("digest") != outside.mandate_digest
        or mandate.get("compact_jws") != outside.mandate_record.get("compact_jws")
    ):
        raise NeedsReverification()

    request_tuple = payment_tuple_from_request(request, obligation_id=request.obligation_id, amount_mode=payload.amount_mode)
    request_digest = payment_tuple_digest(request_tuple)

    def block(reason: ReasonCode) -> ExecutionResult:
        return gw._blocked(
            t, attempt_id=attempt_id, owner_uid=owner_uid, request=request, idempotency_key=idempotency_key,
            grant_id=claims.jti, request_digest=request_digest, reason=reason, workflow_id=workflow_id,
            sequence=sequence, mandate_digest=outside.mandate_digest,
        )

    # --- Identical replay/duplicate controls to the real gateway. ---
    if idem_record is not None:
        if idem_record["request_digest"] != request_digest:
            return block(ReasonCode.IDEMPOTENCY_CONFLICT)
        return ExecutionResult(status="COMMITTED", reason_codes=(), payment_id=idem_record["payment_id"], attempt_id=attempt_id, receipt_status="PENDING")

    if obligation["state"] == ObligationState.PAID.value:
        transfer = existing_transfer
        if (
            transfer is not None and transfer["request_digest"] == request_digest
            and transfer["job_id"] == request.job_id and transfer["mandate_id"] == request.mandate_id
            and transfer["grant_id"] == claims.jti
        ):
            t.set(gw.IDEMPOTENCY, idem_key, {"key": idempotency_key, "owner_uid": owner_uid, "obligation_id": request.obligation_id, "request_digest": request_digest, "payment_id": obligation["payment_id"]})
            return ExecutionResult(status="COMMITTED", reason_codes=(), payment_id=obligation["payment_id"], attempt_id=attempt_id, receipt_status="PENDING")
        return block(ReasonCode.OBLIGATION_PAID)

    # --- Identical credential-state controls. ---
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
    if obligation.get("current_mandate_id") != claims.mandate_id or obligation.get("current_grant_id") != claims.jti:
        return block(ReasonCode.INVALID_GRANT)
    if job["status"] not in (JobStatus.READY.value, JobStatus.RUNNING.value):
        return block(ReasonCode.INVALID_GRANT)

    # --- NOT checked here (this is the deliberate A/B omission): whether
    # request.destination_account_id/version/amount_minor/currency/
    # payment_reference/debit_account_id equal the approved mandate's. ---

    if destination is None or credit_account_id is None:
        return block(ReasonCode.DESTINATION_CHANGED)
    if debit_account is None or debit_account["currency"] != request.currency:
        return block(ReasonCode.INSUFFICIENT_FUNDS)
    if debit_account["balance_minor"] < request.amount_minor:
        return block(ReasonCode.INSUFFICIENT_FUNDS)
    if credit_account is None or credit_account["currency"] != request.currency:
        return block(ReasonCode.DESTINATION_CHANGED)

    payment_id = f"pay-broad-{attempt_id}"
    debit_account = dict(debit_account)
    debit_account["balance_minor"] -= request.amount_minor
    t.set(gw.ACCOUNTS, request.debit_account_id, debit_account)
    credit_account = dict(credit_account)
    credit_account["balance_minor"] += request.amount_minor
    t.set(gw.ACCOUNTS, credit_account_id, credit_account)
    t.set(gw.LEDGER_TRANSFERS, payment_id, {
        "payment_id": payment_id, "debit_account_id": request.debit_account_id, "credit_account_id": credit_account_id,
        "amount_minor": request.amount_minor, "currency": request.currency, "obligation_id": request.obligation_id,
        "job_id": request.job_id, "mandate_id": request.mandate_id, "grant_id": claims.jti, "request_digest": request_digest,
    })
    obligation = dict(obligation)
    obligation["state"] = ObligationState.PAID.value
    obligation["payment_id"] = payment_id
    t.set(gw.OBLIGATIONS, request.obligation_id, obligation)
    grant = dict(grant)
    grant["state"] = GrantState.CONSUMED.value
    t.set(gw.GRANTS, claims.jti, grant)
    job = dict(job)
    job["status"] = JobStatus.DONE.value
    t.set(gw.JOBS, request.job_id, job)
    t.set(gw.IDEMPOTENCY, idem_key, {"key": idempotency_key, "owner_uid": owner_uid, "obligation_id": request.obligation_id, "request_digest": request_digest, "payment_id": payment_id})

    attempt_digest = gw._persist_attempt(
        t, attempt_id=attempt_id, owner_uid=owner_uid, request=request, idempotency_key=idempotency_key,
        grant_id=claims.jti, request_digest=request_digest, decision="COMMITTED", reason_codes=(),
    )
    gw._append_event(
        t, workflow_id=workflow_id, sequence=sequence, event_type="payment.committed",
        payload=gw._receipt_payload(
            attempt_id=attempt_id, grant_id=claims.jti, mandate_digest=outside.mandate_digest,
            request_digest=request_digest, attempt_digest=attempt_digest, decision="ALLOW", reason_codes=(),
            payment_id=payment_id,
        ),
    )
    return ExecutionResult(status="COMMITTED", reason_codes=(), payment_id=payment_id, attempt_id=attempt_id, receipt_status="PENDING")
