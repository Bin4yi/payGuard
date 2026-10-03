"""The five typed tools available to the model (SPEC.md Section 10 "Tools").

No tool accepts a URL, shell command, SQL expression, callback destination
or raw HTTP request — every parameter here is a plain identifier or typed
payment field. Approval, review creation, new-payee creation and token
issuance are never exposed as tools; ``propose_payment`` only ever forwards
to the deterministic gateway (`services/gateway/enforcement.py`), which is
the only code path that can move money.

Every tool is bound to one job via a ``ToolContext`` and enforces ownership
and job-assignment before returning anything — a poisoned document cannot
make the model's next tool call reach into another job or user's data,
because the binding happens here, not by trusting a model-supplied id.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.gateway.enforcement import ExecutionResult, execute_payment
from services.runner.documents import DocumentError, get_document_for_job
from services.shared.crypto import VerifierRegistry
from services.shared.schemas import PaymentRequest, ProposedPaymentCall
from services.shared.store import Store

JOBS = "jobs"
MANDATES = "mandates"
OBLIGATIONS = "obligations"
GRANTS = "grants"
LEDGER_TRANSFERS = "ledger_transfers"
DESTINATIONS = "destinations"

MAX_TOOL_CALLS = 8


class ToolError(Exception):
    pass


class BudgetExceeded(ToolError):
    pass


@dataclass
class ToolContext:
    store: Store
    job_id: str
    owner_uid: str
    runner_principal: str
    verifier_registry: VerifierRegistry
    calls_made: int = field(default=0)
    # Overridable only for the SPEC.md Section 13 evaluation harness, which
    # must run configurations A/B against services.eval.broad_simulator's
    # execute_payment_broad instead of the real gateway. Never overridden
    # anywhere in the demo API — see services/api/app.py and
    # tests/integration/test_broad_simulator.py's static guard.
    payment_executor: object = field(default=execute_payment)

    def _charge_call(self) -> None:
        if self.calls_made >= MAX_TOOL_CALLS:
            raise BudgetExceeded(f"exceeded the {MAX_TOOL_CALLS}-tool-call budget for this job")
        self.calls_made += 1


def _load_owned_job(store: Store, job_id: str, owner_uid: str) -> dict:
    job = store.peek(JOBS, job_id)
    if job is None or job["owner_uid"] != owner_uid:
        raise ToolError("job not found or not owned")
    return job


def get_rent_context(ctx: ToolContext) -> dict:
    """Owned mandate scope + obligation status — never a bearer token or
    signing material, even in an "advanced" form (SPEC.md Section 6:
    "Keep the token out of prompts... document content and logs.")."""
    ctx._charge_call()
    job = _load_owned_job(ctx.store, ctx.job_id, ctx.owner_uid)
    mandate = ctx.store.peek(MANDATES, job["mandate_id"])
    obligation = ctx.store.peek(OBLIGATIONS, job["obligation_id"])
    if mandate is None or obligation is None:
        raise ToolError("job references a missing mandate or obligation")
    destination = ctx.store.peek(
        DESTINATIONS, f"{mandate['destination_account_id']}:{mandate['destination_version']}"
    )

    return {
        "obligation_id": obligation["obligation_id"],
        "obligation_state": obligation["state"],
        "rent_period": mandate["payment_reference"],
        "approved_amount_minor": mandate["amount_minor"],
        "approved_currency": mandate["currency"],
        "approved_debit_account_id": mandate["debit_account_id"],
        "approved_destination_account_id": mandate["destination_account_id"],
        "approved_destination_version": mandate["destination_version"],
        "approved_destination_label": destination["label"] if destination else None,
        "allowed_document_ids": tuple(job.get("allowed_documents", ())),
    }


def read_selected_document(ctx: ToolContext, document_id: str) -> dict:
    """Permitted document or evidence view, bound to this job's assignment."""
    ctx._charge_call()
    try:
        document = get_document_for_job(ctx.store, job_id=ctx.job_id, owner_uid=ctx.owner_uid, document_id=document_id)
    except DocumentError as exc:
        raise ToolError(str(exc)) from exc
    return {
        "document_id": document["document_id"],
        "media_type": document["media_type"],
        "page_count": document["page_count"],
        "pages": list(document["pages"]),
    }


def get_payment_status(ctx: ToolContext) -> dict:
    """Authoritative transfer + receipt state — never a model-writable field."""
    ctx._charge_call()
    job = _load_owned_job(ctx.store, ctx.job_id, ctx.owner_uid)
    obligation = ctx.store.peek(OBLIGATIONS, job["obligation_id"])
    if obligation is None:
        raise ToolError("job references a missing obligation")
    transfer = None
    if obligation.get("payment_id"):
        transfer = ctx.store.peek(LEDGER_TRANSFERS, obligation["payment_id"])
    return {
        "obligation_state": obligation["state"],
        "payment_id": obligation.get("payment_id"),
        "job_status": job["status"],
        "transfer": transfer,
    }


def get_assigned_grant_token(store: Store, job_id: str, owner_uid: str) -> str:
    """`GET /internal/jobs/{id}/grant` equivalent: the runner's *only* way to
    obtain a payment credential — never a model tool, never exposed to the
    model's context, and never capable of creating or renewing one."""
    job = _load_owned_job(store, job_id, owner_uid)
    grant = store.peek(GRANTS, job["grant_id"])
    if grant is None or grant["state"] != "ACTIVE" or not grant.get("compact_jws"):
        raise ToolError("job has no active, signed grant")
    return grant["compact_jws"]


def propose_payment(ctx: ToolContext, call: ProposedPaymentCall, *, grant_token: str) -> ExecutionResult:
    """Pass the exact tuple to the gateway. ``job_id``/``mandate_id``/
    ``obligation_id`` are injected from the bound job context, never taken
    from the model's call — a poisoned model has no field here through
    which it could redirect payment to a different job, mandate or
    obligation than the one it was actually assigned (SPEC.md Section 10:
    "Never expose approval, new-payee creation or token issuance as model
    tools"; the destination/amount/reference *within this job's scope* are
    exactly what the gateway independently re-checks against the signed
    mandate — this tool does not pre-filter them)."""
    ctx._charge_call()
    job = _load_owned_job(ctx.store, ctx.job_id, ctx.owner_uid)
    request = PaymentRequest(
        job_id=ctx.job_id,
        mandate_id=job["mandate_id"],
        obligation_id=job["obligation_id"],
        debit_account_id=call.debit_account_id,
        destination_account_id=call.destination_account_id,
        destination_version=call.destination_version,
        amount_minor=call.amount_minor,
        currency=call.currency,
        payment_reference=call.payment_reference,
    )
    return ctx.payment_executor(
        ctx.store,
        request=request,
        grant_token=grant_token,
        idempotency_key=f"job-{ctx.job_id}-propose-1",
        authenticated_principal=ctx.runner_principal,
        verifier_registry=ctx.verifier_registry,
    )
