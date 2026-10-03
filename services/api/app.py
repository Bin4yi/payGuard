"""The local demo backend: a single FastAPI app fronting the already-tested
services (authority, gateway, runner, receipts) for browser use.

A real deployment splits this into separate Cloud Run services per SPEC.md
Section 4, each with its own service account and no direct access to the
others' state. This single-process app exists for CLAUDE_CODE_PROMPT.md
Phase 4 (frontend + isolated demo access) and its tests; every handler here
still goes through the same ownership/ID-token checks a split deployment
would need, so the migration is "run these modules as separate services",
not "add security checks that were missing."

Two kinds of caller, never interchangeable:

- **Users** (``/v1/...``) present a session bearer token, verified by
  ``state.identity_verifier``. Every lookup is then checked against that
  user's ownership.
- **Services** (``/v1/internal/...`` and ``/v1/payments/execute``) present a
  service credential, verified by ``state.service_verifier`` — the local
  stand-in for Cloud Run's Google-issued ID tokens. A user session token is
  rejected on these routes and a service credential is rejected on user
  routes (the caller-side analogue of "never accept a receipt as a grant").

Every service-layer exception is mapped to a typed HTTP error in one place
(``_raise_for``), so no domain error can surface as a bare 500.

Endpoints not in SPEC.md Section 12's table are marked "Demo-only".
"""

from __future__ import annotations

import json
import secrets
from typing import Literal, NoReturn

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.concurrency import run_in_threadpool

from services.authority import demo, destinations as destinations_mod, grants, instructions, mandates, recovery
from services.gateway.enforcement import ExecutionResult, execute_payment
from services.lab import attack_lab
from services.receipts import export as receipt_export
from services.receipts import signing as receipt_signing
from services.receipts import verify as receipt_verify
from services.runner import documents as documents_mod
from services.runner import orchestrator, tools
from services.runner.model_client import ModelClient, NoticeParserModelClient
from services.shared.identity import IdentityError, VerifiedIdentity
from services.shared.normalize import NormalizationError, reject_duplicate_keys
from services.shared.reason_codes import ReasonCode
from services.shared.schemas import PaymentRequest
from services.shared.states import MandateState, ObligationState

from services.api.state import AUTHORITY_PRINCIPAL, SCHEDULER_PRINCIPAL, AppState


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(status_code=status_code, detail={"schema_version": "error-v1", "code": code, "message": message})


# Request bodies live at module level, not nested inside create_app(): with
# `from __future__ import annotations`, FastAPI cannot resolve a locally
# scoped class's string annotation and silently misreads the parameter as a
# query field named "body" (caught by tests/integration/test_api.py).


class RegisterDocumentBody(BaseModel):
    """No ``document_id`` field: ids are server-assigned (SPEC.md Section 5,
    rule 1). A client-chosen id lived in one global namespace, so one user's
    upload could collide with — and reveal the existence of — another's."""

    model_config = ConfigDict(extra="forbid")
    # Image types mean "text read from a photo in the browser (OCR)": the
    # server only ever stores extracted text, never the image itself.
    media_type: Literal["text/plain", "image/png", "image/jpeg", "image/webp"] = "text/plain"
    pages: list[str]


class RentInstructionBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    amount_minor: int
    currency: str = "LKR"
    destination_version: int | None = None


class CreateMandateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_ids: list[str] = []


class ConfirmMandateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int
    nonce: str


class ReviewBody(BaseModel):
    """Both fields → a destination change. Neither → expiry recovery with the
    same, unchanged payment details (SPEC.md Section 5)."""

    model_config = ConfigDict(extra="forbid")
    new_account_number: str | None = None
    new_label: str | None = None


class LabRunBody(BaseModel):
    """Either a library attack id, or free text (typed, or read from a photo
    in the browser) — not both."""

    model_config = ConfigDict(extra="forbid")
    attack_id: str | None = None
    text: str | None = None


LAB_RUNS_PER_SESSION = 200


class CreateJobBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mandate_id: str


# Gateway reason code → HTTP status, per SPEC.md Section 12 for
# POST /payments/execute: 200 committed, 403 denied authority, 409
# idempotency or obligation conflict, 422 invalid business input, 503 state
# unavailable.
_EXECUTE_STATUS = {
    ReasonCode.IDEMPOTENCY_CONFLICT.value: 409,
    ReasonCode.OBLIGATION_PAID.value: 409,
    ReasonCode.INSUFFICIENT_FUNDS.value: 422,
    ReasonCode.AUDIT_UNAVAILABLE.value: 503,
    ReasonCode.STATUS_PENDING.value: 503,
}

_MAX_IDEMPOTENCY_KEY_LENGTH = 128


def _raise_for(exc: Exception) -> NoReturn:
    """Map one service-layer exception to one typed HTTP error."""
    message = str(exc)
    if isinstance(exc, (mandates.MandateError, grants.GrantError)):
        if message in ("not_found", "mandate_not_found"):
            raise ApiError(404, "NOT_FOUND", "not found") from exc
        if message == "not_owned":
            # Same response as "not found": never confirm that another user's
            # record exists (SPEC.md Section 7).
            raise ApiError(404, "NOT_FOUND", "not found") from exc
        if message == "OBLIGATION_PAID":
            raise ApiError(409, "OBLIGATION_PAID", "this obligation is already paid") from exc
        raise ApiError(409, "CONFLICT", message) from exc
    if isinstance(exc, destinations_mod.DestinationError):
        raise ApiError(409, "CONFLICT", message) from exc
    if isinstance(exc, instructions.InstructionError):
        code = 404 if "not found" in message else 422
        raise ApiError(code, "INVALID_INSTRUCTION", message) from exc
    if isinstance(exc, documents_mod.DocumentError):
        raise ApiError(422, "INVALID_DOCUMENT", message) from exc
    if isinstance(exc, demo.DemoQuotaExceeded):
        raise ApiError(429, "RATE_LIMITED", message) from exc
    if isinstance(exc, demo.DemoError):
        raise ApiError(404, "NOT_FOUND", message) from exc
    if isinstance(exc, orchestrator.OrchestrationError):
        raise ApiError(409, "CONFLICT", message) from exc
    if isinstance(exc, tools.ToolError):
        raise ApiError(409, "CONFLICT", message) from exc
    if isinstance(exc, attack_lab.LabError):
        raise ApiError(422, "INVALID_REQUEST", message) from exc
    if isinstance(exc, receipt_export.ExportError):
        raise ApiError(404, "NOT_FOUND", message) from exc
    raise exc


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="PayGuard demo backend")
    app.state.payguard = state

    # ---- caller authentication ----

    def _bearer(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise ApiError(401, "UNAUTHENTICATED", "missing bearer token")
        return authorization[len("Bearer "):]

    def get_identity(authorization: str | None = Header(default=None)) -> VerifiedIdentity:
        token = _bearer(authorization)
        try:
            return state.identity_verifier.verify(token)
        except IdentityError as exc:
            raise ApiError(401, "UNAUTHENTICATED", str(exc)) from exc

    def get_service_principal(authorization: str | None = Header(default=None)) -> str:
        token = _bearer(authorization)
        try:
            return state.service_verifier.verify(token)
        except IdentityError as exc:
            raise ApiError(401, "UNAUTHENTICATED", "a service credential is required") from exc

    def require_principal(principal: str, *allowed: str) -> None:
        if principal not in allowed:
            raise ApiError(403, "PRINCIPAL_MISMATCH", "this service is not allowed to call this endpoint")

    # ---- ownership helpers ----

    def owned_mandate(mandate_id: str, owner_uid: str) -> dict:
        mandate = state.store.peek("mandates", mandate_id)
        if mandate is None or mandate["owner_uid"] != owner_uid:
            raise ApiError(404, "NOT_FOUND", "mandate not found")
        return mandate

    def owned_job(job_id: str, owner_uid: str) -> dict:
        job = state.store.peek("jobs", job_id)
        if job is None or job["owner_uid"] != owner_uid:
            raise ApiError(404, "NOT_FOUND", "job not found")
        return job

    def current_scenario(owner_uid: str) -> demo.ScenarioHandles:
        handles = demo.get_current_scenario(state.store, owner_uid)
        if handles is None:
            raise ApiError(404, "NOT_FOUND", "no scenario yet — call POST /v1/demo/scenarios first")
        return handles

    # ---- Demo-only: local session issuance (never present in a real deployment) ----

    @app.post("/v1/demo/anonymous-session")
    def demo_anonymous_session() -> dict:
        """Demo-only substitute for the browser calling Firebase Auth's
        anonymous sign-in directly. A real deployment never mints identity
        tokens from its own backend."""
        if state.local_identity_issuer is None:
            raise ApiError(503, "STATUS_PENDING", "local identity issuance is not enabled")
        uid, token = state.local_identity_issuer.issue_anonymous()
        return {"schema_version": "session-v1", "uid": uid, "id_token": token}

    # ---- User endpoints (SPEC.md Section 12) ----

    @app.post("/v1/demo/scenarios")
    def create_demo_scenario(identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        try:
            handles = demo.create_or_reset_scenario(state.store, owner_uid=identity.uid)
        except demo.DemoError as exc:
            _raise_for(exc)
        return _scenario_view(state, handles.scenario_id)

    @app.get("/v1/demo/scenarios/current")
    def get_current_demo_scenario(identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        return _scenario_view(state, current_scenario(identity.uid).scenario_id)

    @app.post("/v1/rent-instructions")
    def update_rent_instruction(body: RentInstructionBody, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        """Write a new version of this user's standing rent instruction.
        Only ever points at a destination version the user already owns."""
        handles = current_scenario(identity.uid)
        latest = instructions.get_latest(state.store, handles.instruction_id)
        if latest is None:
            raise ApiError(404, "NOT_FOUND", "instruction not found")
        try:
            record = instructions.update_instruction(
                state.store, instruction_id=handles.instruction_id, owner_uid=identity.uid,
                amount_minor=body.amount_minor, currency=body.currency,
                destination_account_id=latest["destination_account_id"],
                destination_version=body.destination_version or latest["destination_version"],
            )
        except instructions.InstructionError as exc:
            _raise_for(exc)
        return _instruction_view(record)

    @app.post("/v1/documents")
    def register_document(body: RegisterDocumentBody, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        size_bytes = sum(len(p.encode("utf-8")) for p in body.pages)
        try:
            doc = documents_mod.register_document(
                state.store,
                documents_mod.DocumentInput(
                    document_id=f"doc-{secrets.token_hex(8)}", owner_uid=identity.uid, media_type=body.media_type,
                    pages=tuple(body.pages), size_bytes=size_bytes,
                ),
            )
        except documents_mod.DocumentError as exc:
            _raise_for(exc)
        return {"schema_version": "document-v1", "document_id": doc["document_id"], "page_count": doc["page_count"]}

    @app.post("/v1/mandates")
    def create_mandate(body: CreateMandateBody, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        handles = current_scenario(identity.uid)
        obligation = state.store.peek("obligations", handles.obligation_id)
        if obligation is None:
            raise ApiError(404, "NOT_FOUND", "obligation not found")
        if obligation["state"] == ObligationState.PAID.value:
            raise ApiError(409, "OBLIGATION_PAID", "this obligation is already paid — start a fresh scenario to approve another payment")
        if obligation.get("current_mandate_id"):
            current = state.store.peek("mandates", obligation["current_mandate_id"])
            if current is not None and current["state"] == MandateState.APPROVED.value:
                raise ApiError(
                    409, "APPROVAL_EXISTS",
                    f"an approval already exists for this obligation ({current['mandate_id']}); "
                    "revoke it, or use review for an account change or an expired approval",
                )

        # Every selected document must be one this user owns, and within the
        # per-job budget (SPEC.md Section 10: 3 documents) — refused here,
        # with a clear reason, rather than failing mysteriously at run time.
        if len(body.document_ids) > orchestrator.MAX_DOCUMENTS:
            raise ApiError(422, "INVALID_REQUEST", f"at most {orchestrator.MAX_DOCUMENTS} documents per approval")
        for document_id in body.document_ids:
            document = state.store.peek("documents", document_id)
            if document is None or document["owner_uid"] != identity.uid:
                raise ApiError(404, "NOT_FOUND", f"document {document_id!r} not found")

        # The approved snapshot comes from the user's own current instruction
        # (SPEC.md Section 5, rule 5) — never from document text.
        instruction = instructions.get_latest(state.store, handles.instruction_id)
        if instruction is None or instruction["owner_uid"] != identity.uid:
            raise ApiError(404, "NOT_FOUND", "instruction not found")

        # Server-assigned, collision-proof id (SPEC.md Section 5, rule 1).
        mandate_id = f"mandate-{handles.scenario_id}-{secrets.token_hex(4)}"
        try:
            draft = mandates.create_draft(
                state.store,
                mandates.DraftInput(
                    mandate_id=mandate_id, owner_uid=identity.uid, instruction_id=instruction["instruction_id"],
                    instruction_version=instruction["version"], obligation_id=handles.obligation_id,
                    debit_account_id=handles.debit_account_id, payee_id=f"payee-{handles.scenario_id}",
                    destination_account_id=instruction["destination_account_id"],
                    destination_version=instruction["destination_version"],
                    amount_mode="exact", amount_minor=instruction["amount_minor"], currency=instruction["currency"],
                    payment_reference=handles.payment_reference, grant_expiry_seconds=300,
                    selected_document_ids=tuple(body.document_ids), policy_version="rent-v1",
                    supersedes_mandate_id=None,
                ),
            )
        except mandates.MandateError as exc:
            _raise_for(exc)
        return _mandate_view(state, draft, include_nonce=True)

    @app.post("/v1/mandates/{mandate_id}/confirm")
    def confirm_mandate(mandate_id: str, body: ConfirmMandateBody, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        owned_mandate(mandate_id, identity.uid)
        try:
            mandates.confirm_mandate(
                state.store, mandate_id=mandate_id, owner_uid=identity.uid, version=body.version, nonce=body.nonce,
            )
        except mandates.MandateError as exc:
            _raise_for(exc)
        # A real deployment relies on the scheduled authority worker; the demo
        # also signs inline so a fresh browser sees progress at once. If this
        # inline attempt fails (a KMS outage, say), the approval is still
        # recorded and simply stays signing_state=PENDING — the response says
        # so honestly, no grant can activate, and the drain endpoint recovers
        # it later (SPEC.md Section 5: "keep signing PENDING, retry").
        try:
            mandates.sign_mandate(state.store, mandate_id, state.authority_signer)
        except Exception:  # noqa: BLE001 - any signer failure means "still pending", by design
            pass
        confirmed = state.store.peek("mandates", mandate_id)

        # A confirmed account change is the user's own explicit decision, so
        # their standing instruction follows it — otherwise next month's
        # approval would quietly revert to the old account.
        if confirmed.get("supersedes_mandate_id"):
            latest = instructions.get_latest(state.store, confirmed["instruction_id"])
            if latest is not None and latest["destination_version"] != confirmed["destination_version"]:
                try:
                    instructions.update_instruction(
                        state.store, instruction_id=latest["instruction_id"], owner_uid=identity.uid,
                        amount_minor=latest["amount_minor"], currency=latest["currency"],
                        destination_account_id=confirmed["destination_account_id"],
                        destination_version=confirmed["destination_version"],
                    )
                except instructions.InstructionError:
                    pass  # the approval itself stands; only the default for next time is unchanged
        return _mandate_view(state, confirmed, include_nonce=False)

    @app.get("/v1/mandates/{mandate_id}")
    def get_mandate(mandate_id: str, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        return _mandate_view(state, owned_mandate(mandate_id, identity.uid), include_nonce=False)

    @app.post("/v1/mandates/{mandate_id}/revoke")
    def revoke_mandate(mandate_id: str, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        try:
            mandate = mandates.revoke_mandate(state.store, mandate_id, owner_uid=identity.uid)
        except mandates.MandateError as exc:
            _raise_for(exc)
        return _mandate_view(state, mandate, include_nonce=False)

    @app.post("/v1/mandates/{mandate_id}/review")
    def review_mandate(mandate_id: str, body: ReviewBody, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        """Replacement draft for an account change or an expired approval.
        Nothing is approved here: the returned draft still needs an explicit
        POST .../confirm, which is what atomically supersedes the old one."""
        old = owned_mandate(mandate_id, identity.uid)
        obligation = state.store.peek("obligations", old["obligation_id"])
        if obligation is None:
            raise ApiError(404, "NOT_FOUND", "obligation not found")
        if obligation["state"] == ObligationState.PAID.value:
            raise ApiError(409, "OBLIGATION_PAID", "this obligation is already paid; nothing to review")
        if old["state"] != MandateState.APPROVED.value or obligation.get("current_mandate_id") != mandate_id:
            raise ApiError(
                409, "NOT_CURRENT_APPROVAL",
                "only the current approval for an unpaid obligation can be reviewed; "
                "after a revoke, create a fresh approval instead",
            )

        changing_destination = body.new_account_number is not None or body.new_label is not None
        if changing_destination:
            if not (body.new_account_number and body.new_label):
                raise ApiError(422, "INVALID_REVIEW", "provide both new_account_number and new_label, or neither for expiry recovery")
            try:
                destination = destinations_mod.register_reviewed_destination(
                    state.store, destination_account_id=old["destination_account_id"], owner_uid=identity.uid,
                    account_number=body.new_account_number.strip(), label=body.new_label.strip(),
                    scope=old["obligation_id"],
                )
            except destinations_mod.DestinationError as exc:
                _raise_for(exc)
            destination_version = destination["version"]
        else:
            destination_version = old["destination_version"]

        replacement_id = f"{mandate_id.split('-r-')[0]}-r-{secrets.token_hex(4)}"
        try:
            draft = mandates.create_draft(
                state.store,
                mandates.DraftInput(
                    mandate_id=replacement_id, owner_uid=identity.uid, instruction_id=old["instruction_id"],
                    instruction_version=old["instruction_version"], obligation_id=old["obligation_id"],
                    debit_account_id=old["debit_account_id"], payee_id=old["payee_id"],
                    destination_account_id=old["destination_account_id"], destination_version=destination_version,
                    amount_mode=old["amount_mode"], amount_minor=old["amount_minor"],
                    currency=old["currency"], payment_reference=old["payment_reference"],
                    grant_expiry_seconds=old["grant_expiry_seconds"],
                    selected_document_ids=tuple(old["selected_document_ids"]),
                    policy_version=old["policy_version"], supersedes_mandate_id=mandate_id,
                ),
            )
        except mandates.MandateError as exc:
            _raise_for(exc)
        view = _mandate_view(state, draft, include_nonce=True)
        view["review_kind"] = "destination_change" if changing_destination else "expiry_recovery"
        return view

    @app.post("/v1/grants/{grant_id}/revoke")
    def revoke_grant(grant_id: str, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        try:
            grant = grants.revoke_grant(state.store, grant_id, owner_uid=identity.uid)
        except grants.GrantError as exc:
            if str(exc) == "already_consumed":
                raise ApiError(409, "OBLIGATION_PAID", "this grant was already used for a committed payment") from exc
            _raise_for(exc)
        return {"schema_version": "grant-v1", "grant_id": grant["grant_id"], "state": grant["state"]}

    @app.post("/v1/jobs")
    def create_job(body: CreateJobBody, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        owned_mandate(body.mandate_id, identity.uid)
        job_id = f"job-{body.mandate_id}"
        grant_id = f"grant-{body.mandate_id}"
        try:
            reservation = grants.reserve_job_and_grant(
                state.store, job_id=job_id, grant_id=grant_id, mandate_id=body.mandate_id, owner_uid=identity.uid,
                runner_principal=state.runner_principal,
            )
            # Demo simplification: activate inline instead of waiting for the
            # scheduled worker. A failure here leaves the grant honestly
            # PENDING_SIGNATURE; the runner can never receive it until active.
            if reservation["grant"] and reservation["grant"]["state"] == "PENDING_SIGNATURE":
                grants.sign_and_activate_grant(state.store, reservation["grant"]["grant_id"], state.authority_signer)
        except grants.GrantError as exc:
            _raise_for(exc)
        return _job_view(state, state.store.peek("jobs", job_id))

    @app.get("/v1/jobs/{job_id}")
    def get_job(job_id: str, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        return _job_view(state, owned_job(job_id, identity.uid))

    @app.post("/v1/jobs/{job_id}/run")
    def run_job(job_id: str, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        """Demo-only: a real deployment triggers the runner automatically
        once a grant activates (Cloud Run, not a browser-initiated call)."""
        owned_job(job_id, identity.uid)
        model_client: ModelClient = (
            state.model_client_factory() if state.model_client_factory else NoticeParserModelClient()
        )
        try:
            result = orchestrator.run_job(
                state.store, job_id=job_id, owner_uid=identity.uid, runner_principal=state.runner_principal,
                model_client=model_client, verifier_registry=state.authority_registry,
                screener=state.screener,
            )
        except (orchestrator.OrchestrationError, tools.ToolError) as exc:
            _raise_for(exc)
        # Stand-in for the scheduled receipt worker: sign whatever this
        # decision just appended, so the trail is ready when the UI asks.
        receipt_signing.drain_workflow(state.store, job_id, state.receipt_signer)
        return _orchestration_view(state, job_id, result)

    # ---- Attack Lab (isolated: every run uses its own throwaway store) ----

    lab_runs: dict[str, int] = {}

    @app.get("/v1/lab/attacks")
    def list_lab_attacks(identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        return {
            "schema_version": "lab-attacks-v1",
            "attacks": [
                {"attack_id": a.attack_id, "title": a.title, "technique": a.technique, "text": a.text,
                 "is_attack": a.is_attack}
                for a in attack_lab.LIBRARY
            ],
        }

    @app.post("/v1/lab/run")
    def run_lab_attack(body: LabRunBody, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        if (body.attack_id is None) == (body.text is None):
            raise ApiError(422, "INVALID_REQUEST", "send exactly one of attack_id or text")
        used = lab_runs.get(identity.uid, 0)
        if used >= LAB_RUNS_PER_SESSION:
            raise ApiError(429, "RATE_LIMITED", "Attack Lab limit reached for this session — start a new session")
        lab_runs[identity.uid] = used + 1
        try:
            if body.attack_id is not None:
                attack = attack_lab.get_attack(body.attack_id)
                return attack_lab.run_attack(attack.text, attack=attack, screener=state.screener)
            return attack_lab.run_attack(body.text or "", screener=state.screener)
        except attack_lab.LabError as exc:
            _raise_for(exc)

    @app.get("/v1/jobs/{job_id}/receipts")
    def get_job_receipts(job_id: str, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        """Read-only: verifies what is stored, signs nothing."""
        owned_job(job_id, identity.uid)
        result = receipt_verify.verify_chain(
            state.store, job_id, receipt_registry=state.receipt_registry, authority_registry=state.authority_registry,
        )
        return {
            "schema_version": "receipt-trail-v1",
            "workflow_id": job_id,
            "completeness_unknown": result.completeness_unknown,
            "gaps": result.gaps,
            "entries": [{"sequence": e.sequence, "status": e.status, "detail": e.detail} for e in result.entries],
        }

    @app.post("/v1/jobs/{job_id}/receipt-export")
    def export_job_receipts(job_id: str, identity: VerifiedIdentity = Depends(get_identity)) -> dict:
        """A private, portable evidence bundle, plus the result of verifying
        it against this server's independently configured keys."""
        owned_job(job_id, identity.uid)
        try:
            bundle = receipt_export.build_bundle(state.store, job_id, owner_uid=identity.uid)
        except receipt_export.ExportError as exc:
            _raise_for(exc)
        verification = receipt_export.verify_bundle(
            bundle, receipt_registry=state.receipt_registry, authority_registry=state.authority_registry,
        )
        return {
            "schema_version": "receipt-export-v1",
            "bundle": bundle,
            "verification": {
                "all_verified": verification.all_verified,
                "completeness_unknown": verification.completeness_unknown,
                "mandate_status": verification.mandate_status,
                "gaps": verification.gaps,
                "attempt_failures": verification.attempt_failures,
                "receipts": [{"sequence": r.sequence, "status": r.status, "detail": r.detail} for r in verification.receipts],
            },
        }

    # ---- Service endpoints (service credential required, never a user session) ----

    @app.get("/v1/internal/jobs/{job_id}/context")
    def internal_job_context(job_id: str, principal: str = Depends(get_service_principal)) -> dict:
        """Assigned context for the runner. Bound to the job's assigned
        runner principal; never includes a credential."""
        job = state.store.peek("jobs", job_id)
        if job is None or job["runner"] != principal:
            raise ApiError(404, "NOT_FOUND", "job not found")
        ctx = tools.ToolContext(
            store=state.store, job_id=job_id, owner_uid=job["owner_uid"], runner_principal=principal,
            verifier_registry=state.authority_registry,
        )
        try:
            context = tools.get_rent_context(ctx)
        except tools.ToolError as exc:
            _raise_for(exc)
        return {"schema_version": "job-context-v1", "job_id": job_id, "context": {**context, "allowed_document_ids": list(context["allowed_document_ids"])}}

    @app.get("/v1/internal/jobs/{job_id}/grant")
    def internal_job_grant(job_id: str, principal: str = Depends(get_service_principal)) -> dict:
        """The runner's only way to obtain its credential: returns the
        existing persisted token to the assigned runner, only while ACTIVE.
        Cannot create, renew or widen anything (SPEC.md Section 6)."""
        job = state.store.peek("jobs", job_id)
        if job is None or job["runner"] != principal:
            raise ApiError(404, "NOT_FOUND", "job not found")
        grant = state.store.peek("grants", job["grant_id"])
        if grant is None or grant["state"] != "ACTIVE" or not grant.get("compact_jws"):
            raise ApiError(409, "STATUS_PENDING", "this job has no active grant")
        return {"schema_version": "job-grant-v1", "job_id": job_id, "grant_token": grant["compact_jws"], "expires_at": grant["expiry"]}

    @app.post("/v1/payments/execute")
    async def payments_execute(
        request: Request,
        principal: str = Depends(get_service_principal),
        x_payguard_grant: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ):
        """The gateway's HTTP boundary (SPEC.md Section 7 "Input").

        The raw body is parsed with duplicate-key rejection *before* any
        normal JSON handling could silently keep the last of two
        ``amount_minor`` fields, then validated strictly (unknown fields,
        floats, strings-as-numbers and booleans all rejected). The
        idempotency key lives only in its header; a body field of that name
        is an unknown field and is rejected. The caller's identity is the
        verified service principal, never a string it supplies.
        """
        if not x_payguard_grant:
            raise ApiError(401, "INVALID_GRANT", "missing X-PayGuard-Grant header")
        if not idempotency_key or len(idempotency_key) > _MAX_IDEMPOTENCY_KEY_LENGTH or not idempotency_key.isascii():
            raise ApiError(422, "INVALID_REQUEST", "a non-empty ASCII Idempotency-Key header of at most 128 characters is required")

        raw = await request.body()
        try:
            data = reject_duplicate_keys(raw)
        except NormalizationError as exc:
            raise ApiError(422, "INVALID_REQUEST", str(exc)) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(422, "INVALID_REQUEST", "body is not valid JSON") from exc
        if not isinstance(data, dict):
            raise ApiError(422, "INVALID_REQUEST", "body must be a JSON object")
        try:
            payment_request = PaymentRequest(**data)
        except ValidationError as exc:
            raise ApiError(422, "INVALID_REQUEST", f"invalid payment request: {exc.errors()[0]['msg']}") from exc

        # The gateway is synchronous (store transactions + local signature
        # verification); run it off the event loop so one slow decision can't
        # stall every other request.
        result = await run_in_threadpool(
            execute_payment, state.store, request=payment_request, grant_token=x_payguard_grant,
            idempotency_key=idempotency_key, authenticated_principal=principal,
            verifier_registry=state.authority_registry,
        )
        body = {
            "schema_version": "execution-v1",
            "status": result.status,
            "reason_codes": list(result.reason_codes),
            "payment_id": result.payment_id,
            "receipt_status": result.receipt_status,
        }
        if result.status == "COMMITTED":
            return JSONResponse(status_code=200, content=body)
        status = next((_EXECUTE_STATUS[c] for c in result.reason_codes if c in _EXECUTE_STATUS), 403)
        return JSONResponse(status_code=status, content=body)

    @app.post("/v1/internal/authority/drain")
    def internal_authority_drain(principal: str = Depends(get_service_principal)) -> dict:
        """Recover pending mandate/grant signatures. Cannot create approval."""
        require_principal(principal, SCHEDULER_PRINCIPAL)
        report = recovery.drain(state.store, state.authority_signer)
        return {
            "mandates_signed": report.mandates_signed, "mandates_failed": report.mandates_failed,
            "grants_activated": report.grants_activated, "grants_failed": report.grants_failed,
        }

    @app.post("/v1/internal/receipts/drain")
    def internal_receipts_drain(principal: str = Depends(get_service_principal)) -> dict:
        """Sign every pending receipt event, in order, per workflow (bounded)."""
        require_principal(principal, SCHEDULER_PRINCIPAL)
        workflows = sorted({
            doc_id.rsplit(":", 1)[0]
            for doc_id in state.store.list_ids("workflow_events")
            if (state.store.peek("workflow_events", doc_id) or {}).get("signing_state") == "PENDING"
        })
        signed = 0
        for workflow_id in workflows[:100]:
            signed += len(receipt_signing.drain_workflow(state.store, workflow_id, state.receipt_signer, max_events=50))
        return {"workflows_processed": len(workflows[:100]), "receipts_signed": signed}

    @app.post("/v1/internal/demo/scenarios/{scenario_id}/seed")
    def internal_seed_scenario(scenario_id: str, principal: str = Depends(get_service_principal)) -> dict:
        """Authority-only, idempotent: repeated seeding returns the existing
        result and can never replenish balances."""
        require_principal(principal, AUTHORITY_PRINCIPAL)
        scenario = state.store.peek("demo_scenarios", scenario_id)
        if scenario is None:
            raise ApiError(404, "NOT_FOUND", "scenario not found")
        try:
            result = demo.seed_scenario_balances(state.store, scenario_id=scenario_id, owner_uid=scenario["owner_uid"])
        except demo.DemoError as exc:
            _raise_for(exc)
        return {"schema_version": "seed-v1", "scenario_id": scenario_id, "seeded": result["seeded"]}

    return app


# ---- views: explicit field lists, so a new stored field can never leak by accident ----


def _instruction_view(record: dict) -> dict:
    return {
        "schema_version": "instruction-v1",
        "instruction_id": record["instruction_id"],
        "version": record["version"],
        "amount_minor": record["amount_minor"],
        "currency": record["currency"],
        "destination_version": record["destination_version"],
    }


def _destination_view(state: AppState, destination_account_id: str, version: int) -> dict | None:
    destination = state.store.peek("destinations", f"{destination_account_id}:{version}")
    if destination is None:
        return None
    return {
        "version": destination["version"],
        "label": destination.get("label"),
        "account_number": destination.get("account_number"),
    }


def _scenario_view(state: AppState, scenario_id: str) -> dict:
    handles = demo.handles_for(scenario_id)
    debit = state.store.peek("accounts", handles.debit_account_id)
    obligation = state.store.peek("obligations", handles.obligation_id) or {}
    instruction = instructions.get_latest(state.store, handles.instruction_id) or {}
    destination = _destination_view(
        state, instruction.get("destination_account_id", handles.destination_account_id),
        instruction.get("destination_version", handles.destination_version),
    )

    current_mandate_id = obligation.get("current_mandate_id")
    current_mandate = state.store.peek("mandates", current_mandate_id) if current_mandate_id else None
    current_job_id = None
    if current_mandate and current_mandate.get("issued_grant_id"):
        grant = state.store.peek("grants", current_mandate["issued_grant_id"])
        current_job_id = grant["job_id"] if grant else None
    return {
        "schema_version": "scenario-v1",
        "scenario_id": handles.scenario_id,
        "obligation_id": handles.obligation_id,
        "obligation_state": obligation.get("state"),
        "payment_id": obligation.get("payment_id"),
        "amount_minor": instruction.get("amount_minor", handles.amount_minor),
        "currency": instruction.get("currency", handles.currency),
        "instruction_version": instruction.get("version"),
        "destination_label": destination["label"] if destination else None,
        "destination_account_number": destination["account_number"] if destination else None,
        "destination_version": destination["version"] if destination else None,
        "tenant_balance_minor": debit["balance_minor"] if debit else None,
        "current_mandate": (
            {"mandate_id": current_mandate["mandate_id"], "state": current_mandate["state"],
             "signing_state": current_mandate["signing_state"], "job_id": current_job_id}
            if current_mandate else None
        ),
    }


def _mandate_view(state: AppState, mandate: dict, *, include_nonce: bool) -> dict:
    view = {
        "schema_version": "mandate-v1",
        "mandate_id": mandate["mandate_id"],
        "version": mandate["version"],
        "state": mandate["state"],
        "signing_state": mandate["signing_state"],
        "obligation_id": mandate["obligation_id"],
        "debit_account_id": mandate["debit_account_id"],
        "destination_account_id": mandate["destination_account_id"],
        "destination_version": mandate["destination_version"],
        "destination": _destination_view(state, mandate["destination_account_id"], mandate["destination_version"]),
        "amount_minor": mandate["amount_minor"],
        "currency": mandate["currency"],
        "payment_reference": mandate["payment_reference"],
        "supersedes_mandate_id": mandate.get("supersedes_mandate_id"),
        "issued_grant_id": mandate.get("issued_grant_id"),
        # Signature metadata for the advanced view — never the grant token.
        "digest": mandate.get("digest"),
        "key_id": mandate.get("key_id"),
    }
    if include_nonce:
        view["nonce"] = mandate["nonce"]
    return view


def _job_view(state: AppState, job: dict) -> dict:
    obligation = state.store.peek("obligations", job["obligation_id"]) or {}
    grant = state.store.peek("grants", job["grant_id"]) or {}
    return {
        "schema_version": "job-v1",
        "job_id": job["job_id"],
        "status": job["status"],
        "mandate_id": job["mandate_id"],
        "obligation_id": job["obligation_id"],
        "grant_id": job["grant_id"],
        "grant_state": grant.get("state"),
        "grant_expires_at": grant.get("expiry"),
        "obligation_state": obligation.get("state"),
        "payment_id": obligation.get("payment_id"),
        "allowed_documents": job.get("allowed_documents", []),
    }


_PAYMENT_FIELDS = (
    "debit_account_id", "destination_account_id", "destination_version",
    "amount_minor", "currency", "payment_reference",
)


def _orchestration_view(state: AppState, job_id: str, result) -> dict:
    gateway: ExecutionResult | None = result.gateway_result
    view = {
        "schema_version": "orchestration-v1",
        "outcome": result.outcome,
        "conflicts": [
            {"field": c.field, "expected": c.expected, "claimed": c.claimed, "detail": c.detail, "source": c.source}
            for c in result.conflicts
        ],
        "gateway_status": gateway.status if gateway else None,
        "reason_codes": list(gateway.reason_codes) if gateway else [],
        "payment_id": gateway.payment_id if gateway else None,
        "approved": None,
        "attempted": None,
        # Decision-model pre-screen, if one ran — untrusted model output,
        # shown as such (probabilities, flags, or "unavailable").
        "screening": next(
            ({k: v for k, v in entry.items() if k != "type"} for entry in result.activity if entry.get("type") == "screening"),
            None,
        ),
    }
    # SPEC.md Section 14: "Show approved-versus-attempted details on refusal."
    # Both sides come from trusted stored records — the signed mandate and
    # the attempt exactly as the gateway received it — never from the model.
    job = state.store.peek("jobs", job_id)
    mandate = state.store.peek("mandates", job["mandate_id"]) if job else None
    if mandate is not None:
        view["approved"] = {f: mandate[f] for f in _PAYMENT_FIELDS}
    if gateway is not None and gateway.attempt_id:
        attempt = state.store.peek("payment_attempts", gateway.attempt_id)
        if attempt is not None:
            view["attempted"] = {f: attempt["request"][f] for f in _PAYMENT_FIELDS}
    return view
