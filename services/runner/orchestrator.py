"""Runner orchestration (SPEC.md Section 10; CLAUDE_CODE_PROMPT.md Phase 3).

Drives one job: assemble owned context and documents, get one structured
turn from the model, validate it strictly (one schema-repair retry), resolve
its citations against the *actual* stored document text, detect conflicts
against the *actual* approved mandate and obligation history, and only then
decide whether ``propose_payment`` may even be attempted.

The review gate is enforced here in runtime code, not requested of the
model: if there is any conflict (model-reported or runtime-detected), any
ambiguous/missing claimed field, or the model's own ``next_action`` is not
``propose_payment``, the job is marked ``REVIEW_REQUIRED`` and the gateway is
never called. A poisoned document cannot talk its way past this gate — the
gate does not consult the model's opinion of whether it should apply.

When the gate does pass, the model's ``propose_payment`` tool-call fields are
submitted to the gateway exactly as given — never silently corrected to the
approved values first (SPEC.md Section 10: "Do not silently replace a wrong
destination with the approved one and then claim the attack was blocked.").
The gateway's own independent tuple check is what actually decides whether
the transfer happens; this orchestrator does not pre-authorize anything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pydantic import ValidationError

from services.gateway.enforcement import ExecutionResult
from services.runner import tools
from services.runner.decision_client import DecisionClient
from services.runner.model_client import ModelClient
from services.runner.screening import screen_documents
from services.runner.tools import BudgetExceeded, ToolContext, ToolError
from services.shared.crypto import VerifierRegistry
from services.shared.schemas import Conflict, ProposedPaymentCall, RentProposal
from services.shared.states import JobStatus
from services.shared.store import Store

MAX_DOCUMENTS = 3
TASK_BUDGET_SECONDS = 60

JOBS = "jobs"
MANDATES = "mandates"
OBLIGATIONS = "obligations"
DESTINATIONS = "destinations"
LEDGER_TRANSFERS = "ledger_transfers"
GRANTS = "grants"

OUTCOME_PAID = "paid"
OUTCOME_BLOCKED = "blocked"
OUTCOME_REVIEW_REQUIRED = "review_required"
OUTCOME_SCHEMA_INVALID = "schema_invalid"
OUTCOME_BUDGET_EXCEEDED = "budget_exceeded"
# The job's authority is gone (revoked, superseded or expired) — distinct
# from "the model's output was unreadable", and decided before any model call.
OUTCOME_NO_ACTIVE_AUTHORITY = "no_active_authority"
OUTCOME_TOOL_ERROR = "tool_error"


class OrchestrationError(Exception):
    pass


@dataclass
class OrchestrationResult:
    outcome: str
    proposal: RentProposal | None = None
    conflicts: tuple[Conflict, ...] = ()
    gateway_result: ExecutionResult | None = None
    activity: list[dict] = field(default_factory=list)


def _record_activity(store: Store, job_id: str, entries: list[dict]) -> None:
    """Document reads and model proposals are recorded as *activity*, not
    signed receipts — labelled as untrusted model assertions rather than
    trusted service observations (SPEC.md Section 9)."""

    def txn(t):
        existing = t.get("job_activity", job_id)
        seq_start = len(existing["entries"]) if existing else 0
        merged = (existing["entries"] if existing else []) + entries
        t.set("job_activity", job_id, {"job_id": job_id, "entries": merged, "trusted": False})
        return seq_start

    store.run_transaction(txn)


def _set_job_status(store: Store, job_id: str, status: JobStatus) -> None:
    def txn(t):
        job = t.get(JOBS, job_id)
        if job is None:
            return
        job = dict(job)
        job["status"] = status.value
        t.set(JOBS, job_id, job)

    store.run_transaction(txn)


def _validate_turn(raw_proposal: dict, raw_call: dict | None) -> tuple[RentProposal | None, ProposedPaymentCall | None, str | None]:
    try:
        proposal = RentProposal(**raw_proposal)
    except ValidationError as exc:
        return None, None, str(exc)
    call = None
    if raw_call is not None:
        try:
            call = ProposedPaymentCall(**raw_call)
        except ValidationError as exc:
            return None, None, str(exc)
    return proposal, call, None


def _resolve_citations(proposal: RentProposal, documents: dict[str, dict]) -> list[Conflict]:
    """A model citation is an assertion until checked (SPEC.md Section 10)."""
    issues: list[Conflict] = []
    for item in proposal.evidence:
        doc = documents.get(proposal.document_id) if proposal.document_id else None
        if doc is None:
            issues.append(Conflict(
                field=item.field, expected="a document assigned to this job", claimed=str(proposal.document_id),
                detail="cited document_id is not one of this job's assigned, readable documents",
                source="runtime",
            ))
            continue
        pages = doc["pages"]
        if item.page < 1 or item.page > len(pages):
            issues.append(Conflict(
                field=item.field, expected=f"a page between 1 and {len(pages)}", claimed=str(item.page),
                detail="cited page number is out of range for the document", source="runtime",
            ))
            continue
        page_text = pages[item.page - 1]
        if item.quote not in page_text:
            issues.append(Conflict(
                field=item.field, expected="quote must appear verbatim on the cited page", claimed=item.quote,
                detail=f"quote not found on page {item.page} of {proposal.document_id}", source="runtime",
            ))
    return issues


def _detect_conflicts(proposal: RentProposal, mandate: dict, obligation: dict, destination: dict | None) -> list[Conflict]:
    """Compare the model's claims against the *current* approved mandate and
    the obligation's payment history — never against the document's own
    unverified claims about itself (SPEC.md Section 10, item 1)."""
    detected: list[Conflict] = []

    if proposal.claimed_amount_minor is not None and proposal.claimed_amount_minor != mandate["amount_minor"]:
        detected.append(Conflict(
            field="claimed_amount_minor", expected=str(mandate["amount_minor"]),
            claimed=str(proposal.claimed_amount_minor), detail="claimed amount does not match the approved mandate",
            source="runtime",
        ))
    if proposal.claimed_currency is not None and proposal.claimed_currency != mandate["currency"]:
        detected.append(Conflict(
            field="claimed_currency", expected=mandate["currency"], claimed=proposal.claimed_currency,
            detail="claimed currency does not match the approved mandate", source="runtime",
        ))
    if proposal.claimed_destination_text is not None and destination is not None:
        label = destination.get("label", "")
        demo_account = destination.get("demo_account_id", "")
        account_number = destination.get("account_number", "")
        text = proposal.claimed_destination_text
        known = [v for v in (label, demo_account, account_number) if v]
        if not any(v == text or v in text for v in known):
            detected.append(Conflict(
                field="claimed_destination_text", expected=f"{label} ({demo_account})", claimed=text,
                detail="claimed destination text does not match the approved destination", source="runtime",
            ))
    if obligation["state"] == "PAID":
        detected.append(Conflict(
            field="obligation_id", expected="UNPAID", claimed="PAID",
            detail="this obligation's payment history shows it is already paid", source="runtime",
        ))
    return detected


def run_job(
    store: Store,
    *,
    job_id: str,
    owner_uid: str,
    runner_principal: str,
    model_client: ModelClient,
    verifier_registry: VerifierRegistry,
    now_fn=time.time,
    payment_executor=None,
    screener: DecisionClient | None = None,
) -> OrchestrationResult:
    start = now_fn()
    job = store.peek(JOBS, job_id)
    if job is None or job["owner_uid"] != owner_uid:
        raise OrchestrationError("job not found or not owned")
    # DONE is included so a client can safely replay a completed job: the
    # fixed per-job idempotency key inside tools.propose_payment guarantees
    # this can never produce a second transfer, only the same result again
    # (SPEC.md Section 8's idempotency-key replay path). REVIEW_REQUIRED,
    # FAILED and WAITING_SIGNATURE are excluded — those need the actual
    # remediation path (fresh confirmation, budget/schema fix), not a blind
    # re-run producing the identical inconclusive result.
    if job["status"] not in (JobStatus.READY.value, JobStatus.RUNNING.value, JobStatus.DONE.value):
        raise OrchestrationError(f"job is not runnable in status {job['status']}")

    mandate = store.peek(MANDATES, job["mandate_id"])
    obligation = store.peek(OBLIGATIONS, job["obligation_id"])
    if mandate is None or obligation is None:
        raise OrchestrationError("job references a missing mandate or obligation")

    if job["status"] == JobStatus.DONE.value:
        # Replaying an already-completed job is a request for its recorded
        # result, not a new reconciliation — re-running the model/conflict
        # pipeline against an obligation it will now find "already paid"
        # would misreport a safe replay as requiring review. Short-circuit
        # straight to the authoritative stored outcome (SPEC.md Section 8).
        transfer = store.peek(LEDGER_TRANSFERS, obligation["payment_id"]) if obligation.get("payment_id") else None
        gateway_result = None
        if transfer is not None:
            gateway_result = ExecutionResult(
                status="COMMITTED", reason_codes=(), payment_id=transfer["payment_id"],
                attempt_id=None, receipt_status="PENDING",
            )
        return OrchestrationResult(outcome=OUTCOME_PAID, gateway_result=gateway_result, activity=[{"type": "replay_of_done_job"}])

    # SPEC.md Section 6: "Start the runner only after activation." Check the
    # job's authority is actually live *before* spending a model call on it.
    # This is a cost/clarity check, not the security check — the gateway
    # re-verifies everything independently at execution time regardless.
    grant = store.peek(GRANTS, job["grant_id"])
    if grant is None or grant["state"] != "ACTIVE":
        state_label = grant["state"] if grant else "MISSING"
        return OrchestrationResult(
            outcome=OUTCOME_NO_ACTIVE_AUTHORITY,
            activity=[{"type": "no_active_authority", "grant_state": state_label}],
        )
    if grant["expiry"] <= now_fn():
        return OrchestrationResult(
            outcome=OUTCOME_NO_ACTIVE_AUTHORITY,
            activity=[{"type": "no_active_authority", "grant_state": "EXPIRED"}],
        )

    destination = store.peek(
        DESTINATIONS, f"{mandate['destination_account_id']}:{mandate['destination_version']}"
    )

    _set_job_status(store, job_id, JobStatus.RUNNING)
    ctx_kwargs = dict(
        store=store, job_id=job_id, owner_uid=owner_uid, runner_principal=runner_principal,
        verifier_registry=verifier_registry,
    )
    if payment_executor is not None:
        ctx_kwargs["payment_executor"] = payment_executor
    ctx = ToolContext(**ctx_kwargs)
    activity: list[dict] = []

    context = tools.get_rent_context(ctx)
    activity.append({"type": "tool_call", "tool": "get_rent_context"})

    documents: dict[str, dict] = {}
    doc_list = []
    allowed = list(job.get("allowed_documents", ()))[:MAX_DOCUMENTS]
    for document_id in allowed:
        if now_fn() - start > TASK_BUDGET_SECONDS:
            _set_job_status(store, job_id, JobStatus.FAILED)
            activity.append({"type": "budget_exceeded", "detail": "task time budget exceeded"})
            _record_activity(store, job_id, activity)
            return OrchestrationResult(outcome=OUTCOME_BUDGET_EXCEEDED, activity=activity)
        try:
            doc = tools.read_selected_document(ctx, document_id)
        except (ToolError, BudgetExceeded) as exc:
            over_budget = isinstance(exc, BudgetExceeded)
            _set_job_status(store, job_id, JobStatus.FAILED)
            activity.append({"type": "budget_exceeded" if over_budget else "tool_error", "detail": str(exc)})
            _record_activity(store, job_id, activity)
            return OrchestrationResult(outcome=OUTCOME_BUDGET_EXCEEDED if over_budget else OUTCOME_TOOL_ERROR, activity=activity)
        documents[document_id] = doc
        doc_list.append(doc)
        activity.append({"type": "tool_call", "tool": "read_selected_document", "document_id": document_id})

    # Optional pre-screen with a decision model (Laya / Jev). One-directional:
    # it can only send the job to review — see services/runner/screening.py.
    if screener is not None and doc_list:
        screening = screen_documents(screener, doc_list)
        activity.append({
            "type": "screening",
            "trusted": False,  # model output: an untrusted assertion (SPEC.md Section 9)
            "model_id": screening.model_id,
            "version": screening.version,
            "probabilities": screening.probabilities,
            "flags": [
                {"document_id": f.document_id, "question": f.question,
                 "probability": f.probability, "threshold": f.threshold, "reason": f.reason}
                for f in screening.flags
            ],
            "unavailable": screening.unavailable,
            "elapsed_ms": screening.elapsed_ms,
        })
        if screening.flagged:
            conflicts = tuple(
                Conflict(
                    field="notice",
                    expected=f"no change of payee (screening threshold {f.threshold:.2f})",
                    claimed=f"probability {f.probability:.2f}",
                    detail=f.reason,
                    source="screening",
                )
                for f in screening.flags
            )
            _set_job_status(store, job_id, JobStatus.REVIEW_REQUIRED)
            activity.append({"type": "review_required", "conflict_count": len(conflicts), "ambiguous": False})
            _record_activity(store, job_id, activity)
            return OrchestrationResult(outcome=OUTCOME_REVIEW_REQUIRED, conflicts=conflicts, activity=activity)

    turn = model_client.generate(context=context, documents=doc_list, repair_hint=None)
    proposal, proposed_call, error = _validate_turn(turn.proposal, turn.proposed_payment)
    activity.append({"type": "model_output", "raw": turn.proposal})

    if error is not None:
        turn2 = model_client.generate(context=context, documents=doc_list, repair_hint=error)
        proposal, proposed_call, error2 = _validate_turn(turn2.proposal, turn2.proposed_payment)
        activity.append({"type": "model_output_repair_attempt", "raw": turn2.proposal})
        if error2 is not None:
            _set_job_status(store, job_id, JobStatus.FAILED)
            activity.append({"type": "schema_repair_failed", "error": error2})
            _record_activity(store, job_id, activity)
            return OrchestrationResult(outcome=OUTCOME_SCHEMA_INVALID, activity=activity)

    citation_issues = _resolve_citations(proposal, documents)
    detected_conflicts = _detect_conflicts(proposal, mandate, obligation, destination)
    all_conflicts = tuple(proposal.conflicts) + tuple(detected_conflicts) + tuple(citation_issues)

    ambiguous = any(
        v is None
        for v in (proposal.claimed_amount_minor, proposal.claimed_currency, proposal.claimed_destination_text)
    )

    # --- Runtime-enforced review gate: model output cannot clear this. ---
    if all_conflicts or ambiguous or proposal.next_action != "propose_payment" or proposed_call is None:
        _set_job_status(store, job_id, JobStatus.REVIEW_REQUIRED)
        activity.append({"type": "review_required", "conflict_count": len(all_conflicts), "ambiguous": ambiguous})
        _record_activity(store, job_id, activity)
        return OrchestrationResult(outcome=OUTCOME_REVIEW_REQUIRED, proposal=proposal, conflicts=all_conflicts, activity=activity)

    try:
        grant_token = tools.get_assigned_grant_token(store, job_id, owner_uid)
    except ToolError as exc:
        # Authority was withdrawn while the model was working (revoked or
        # superseded mid-run). Not a model failure — say what actually happened.
        activity.append({"type": "no_active_authority", "detail": str(exc)})
        _record_activity(store, job_id, activity)
        return OrchestrationResult(outcome=OUTCOME_NO_ACTIVE_AUTHORITY, proposal=proposal, conflicts=all_conflicts, activity=activity)

    gateway_result = tools.propose_payment(ctx, proposed_call, grant_token=grant_token)
    activity.append({
        "type": "tool_call", "tool": "propose_payment", "status": gateway_result.status,
        "reason_codes": list(gateway_result.reason_codes),
    })

    if gateway_result.status != "COMMITTED":
        _set_job_status(store, job_id, JobStatus.REVIEW_REQUIRED)
        _record_activity(store, job_id, activity)
        return OrchestrationResult(
            outcome=OUTCOME_BLOCKED, proposal=proposal, conflicts=all_conflicts, gateway_result=gateway_result,
            activity=activity,
        )

    _record_activity(store, job_id, activity)
    return OrchestrationResult(
        outcome=OUTCOME_PAID, proposal=proposal, conflicts=all_conflicts, gateway_result=gateway_result,
        activity=activity,
    )
