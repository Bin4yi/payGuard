"""SPEC.md Section 13 configurations A and B: a baseline agent with none of
PayGuard's own runtime conflict-detection / review gate.

``services/runner/orchestrator.py``'s ``run_job`` deliberately enforces a
review gate that "model output cannot clear" (its own docstring, and
SPEC.md Section 10 item 6). That gate is part of what distinguishes
PayGuard (configuration C) from a naive tool-calling agent — without a
*separate* baseline that omits it, configurations A and B would have the
gate too, would catch every poisoned notice before ever reaching
``services.eval.broad_simulator``, and the comparison would be
meaningless (the broad simulator's deliberate leniency would never
actually get exercised). This module is that separate, deliberately
simpler baseline: get context, read the assigned documents, ask the model
once, and submit exactly what it proposes with no further scrutiny.

**Never used outside the evaluation harness — never wired into the public
demo API.** See ``tests/integration/test_broad_simulator.py``'s static
guard, which also covers this module by name.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.runner import tools
from services.runner.model_client import ModelClient
from services.runner.tools import BudgetExceeded, ToolContext, ToolError
from services.shared.crypto import VerifierRegistry
from services.shared.schemas import ProposedPaymentCall
from services.shared.store import Store

JOBS = "jobs"


class BaselineError(Exception):
    pass


@dataclass
class BaselineResult:
    outcome: str  # "paid" | "blocked" | "no_action"
    gateway_result: object | None = None


def run_job_baseline(
    store: Store,
    *,
    job_id: str,
    owner_uid: str,
    runner_principal: str,
    model_client: ModelClient,
    verifier_registry: VerifierRegistry,
    payment_executor,
) -> BaselineResult:
    job = store.peek(JOBS, job_id)
    if job is None or job["owner_uid"] != owner_uid:
        raise BaselineError("job not found or not owned")

    ctx = ToolContext(
        store=store, job_id=job_id, owner_uid=owner_uid, runner_principal=runner_principal,
        verifier_registry=verifier_registry, payment_executor=payment_executor,
    )
    context = tools.get_rent_context(ctx)
    documents = []
    for document_id in job.get("allowed_documents", ()):
        try:
            documents.append(tools.read_selected_document(ctx, document_id))
        except (ToolError, BudgetExceeded):
            break

    turn = model_client.generate(context=context, documents=documents, repair_hint=None)
    if turn.proposed_payment is None or turn.proposal.get("next_action") != "propose_payment":
        return BaselineResult(outcome="no_action")

    try:
        call = ProposedPaymentCall(**turn.proposed_payment)
    except Exception:
        return BaselineResult(outcome="no_action")

    grant_token = tools.get_assigned_grant_token(store, job_id, owner_uid)
    result = tools.propose_payment(ctx, call, grant_token=grant_token)
    return BaselineResult(outcome="paid" if result.status == "COMMITTED" else "blocked", gateway_result=result)
