"""Strict typed schemas for the mandate payload, grant claims, payment
request tuple and receipt event (SPEC.md Sections 5, 6, 7, 9).

All models are strict and reject unknown fields:

- ``strict=True`` makes pydantic-core refuse to coerce ``bool``, ``float`` or
  numeric strings into ``int`` fields (verified against pydantic-core
  behaviour, not assumed) — SPEC.md Section 3 amount handling: "Reject
  fractions, floats, strings, booleans, zero, negatives, and anything above
  the demo ceiling."
- ``extra="forbid"`` rejects unknown fields (SPEC.md Section 7: "An ignored
  field must never gain meaning later.").

These are ordinary DTOs for validating shapes; they intentionally hold no
business logic (that lives in the gateway/authority decision procedures).
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AMOUNT_CEILING_MINOR = 100_000_000

Money = Annotated[int, Field(strict=True, gt=0, le=AMOUNT_CEILING_MINOR)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
# Canonical JSON round-trips a tuple as a list; only sequence *shape* needs
# strictness (still no dict/scalar-as-sequence coercion), not the
# list-vs-tuple container type. Money/int fields keep full strictness above.
StrTuple = Annotated[tuple[str, ...], Field(strict=False)]


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class AsciiIdModel(StrictModel):
    """Base for models whose string fields must be ASCII (SPEC.md Section 9)."""

    @field_validator("*", mode="before")
    @classmethod
    def _reject_non_ascii_strings(cls, v):
        if isinstance(v, str):
            try:
                v.encode("ascii")
            except UnicodeEncodeError as exc:
                raise ValueError(f"non-ASCII string not allowed: {v!r}") from exc
        return v


class MandatePayload(AsciiIdModel):
    """The exact fixed payload signed as payguard-mandate+jwt (SPEC.md Section 5)."""

    schema_version: Literal["mandate-v1"] = "mandate-v1"
    iss: Literal["payguard-authority"] = "payguard-authority"
    mandate_id: str
    mandate_version: PositiveInt
    user_id: str
    instruction_id: str
    instruction_version: PositiveInt
    obligation_id: str
    action: Literal["rent.pay"] = "rent.pay"
    debit_account_id: str
    payee_id: str
    destination_account_id: str
    destination_version: PositiveInt
    amount_mode: Literal["exact", "maximum"]
    amount_minor: Money
    currency: Literal["LKR"] = "LKR"
    payment_reference: str
    max_executions: Literal[1] = 1
    grant_expiry_seconds: PositiveInt
    selected_document_ids: StrTuple = ()
    policy_version: str
    approved_at: NonNegativeInt


class GrantClaims(AsciiIdModel):
    """The exact claim set signed as payguard-grant+jwt (SPEC.md Section 6)."""

    iss: Literal["payguard-authority"] = "payguard-authority"
    aud: Literal["payguard-payment-gateway"] = "payguard-payment-gateway"
    sub: str
    jti: str
    iat: NonNegativeInt
    nbf: NonNegativeInt
    exp: NonNegativeInt
    agent_principal: str
    mandate_id: str
    mandate_version: PositiveInt
    mandate_digest: str
    job_id: str
    obligation_id: str
    action: Literal["rent.pay"] = "rent.pay"
    debit_account_id: str
    destination_account_id: str
    destination_version: PositiveInt
    amount_mode: Literal["exact", "maximum"]
    amount_minor: Money
    currency: Literal["LKR"] = "LKR"
    payment_reference: str
    max_executions: Literal[1] = 1
    policy_version: str

    @field_validator("exp")
    @classmethod
    def _max_lifetime(cls, exp: int, info):
        iat = info.data.get("iat")
        if iat is not None and exp - iat > 300:
            raise ValueError("grant lifetime exceeds 300 seconds")
        return exp


class PaymentRequest(AsciiIdModel):
    """The typed request body for POST /payments/execute (SPEC.md Section 7).

    ``action`` is fixed by the endpoint itself, not carried as a client field,
    and the idempotency key lives only in the ``Idempotency-Key`` header —
    both per SPEC.md Section 7 ("This endpoint fixes action=rent.pay" /
    "reject a duplicate body field").
    """

    job_id: str
    mandate_id: str
    obligation_id: str
    debit_account_id: str
    destination_account_id: str
    destination_version: PositiveInt
    amount_minor: Money
    currency: Literal["LKR"] = "LKR"
    payment_reference: str


class PaymentTuple(AsciiIdModel):
    """The semantic payment identity used for request_digest (SPEC.md Section 7).

    action, debit account, destination account/version, amount/mode,
    currency, reference and obligation — explicitly excludes token,
    idempotency key, job and mandate IDs.
    """

    schema_version: Literal["payment-tuple-v1"] = "payment-tuple-v1"
    action: Literal["rent.pay"] = "rent.pay"
    debit_account_id: str
    destination_account_id: str
    destination_version: PositiveInt
    amount_mode: Literal["exact", "maximum"]
    amount_minor: Money
    currency: Literal["LKR"] = "LKR"
    payment_reference: str
    obligation_id: str


class AttemptRecord(AsciiIdModel):
    """Full versioned attempt record hashed as attempt_digest (SPEC.md Section 7/9).

    Binds the context fields excluded from request_digest: owner, job,
    mandate, grant IDs and the idempotency key, alongside the actual typed
    payment fields as attempted (never silently repaired).
    """

    schema_version: Literal["attempt-v1"] = "attempt-v1"
    owner_uid: str
    attempt_id: str
    job_id: str
    mandate_id: str
    grant_id: str
    idempotency_key: str
    request: PaymentRequest
    request_digest: str


class ReceiptEvent(AsciiIdModel):
    """The signed receipt event payload (SPEC.md Section 9)."""

    schema_version: Literal["receipt-v1"] = "receipt-v1"
    iss: Literal["payguard-receipt-worker"] = "payguard-receipt-worker"
    receipt_id: str
    workflow_id: str
    sequence: PositiveInt
    event_type: str
    event_producer: str
    signing_service: Literal["receipt-worker"] = "receipt-worker"
    occurred_at: str
    signed_at: str
    grant_id: str | None = None
    mandate_digest: str | None = None
    request_digest: str | None = None
    attempt_digest: str | None = None
    decision: Literal["ALLOW", "BLOCK"] | None = None
    reason_codes: StrTuple = ()
    payment_id: str | None = None
    previous_receipt_digest: str | None = None


class EvidenceItem(AsciiIdModel):
    """One citation linking an extracted field to a page + supporting quote
    (SPEC.md Section 10). ``quote`` is an assertion until the runtime checks
    it against the actual stored document text — see
    ``services/runner/orchestrator.py``."""

    field: str
    page: PositiveInt
    quote: str


class Conflict(AsciiIdModel):
    """A structured disagreement surfaced instead of silently picking a side
    (SPEC.md Section 10, item 2). ``source`` distinguishes a conflict the
    model itself reported from one the deterministic runtime detected
    independently (e.g. by comparing against the approved mandate)."""

    field: str
    expected: str
    claimed: str
    detail: str
    source: Literal["model", "runtime", "screening"] = "model"


class RentProposal(AsciiIdModel):
    """The model's structured output contract (SPEC.md Section 10 "Output
    contract"). Untrusted: every claimed field is an assertion until the
    runtime resolves its citation and compares it against authoritative
    state. Missing/ambiguous values are ``None`` (schema-required
    clarification), never guessed."""

    schema_version: Literal["rent-proposal-v1"] = "rent-proposal-v1"
    document_id: str | None = None
    rent_period: str | None = None
    claimed_amount_minor: Money | None = None
    claimed_currency: Literal["LKR"] | None = None
    claimed_destination_text: str | None = None
    evidence: Annotated[tuple[EvidenceItem, ...], Field(strict=False)] = ()
    conflicts: Annotated[tuple[Conflict, ...], Field(strict=False)] = ()
    next_action: Literal["propose_payment", "request_clarification", "report_conflict"]


class ProposedPaymentCall(AsciiIdModel):
    """The typed arguments of a model-initiated ``propose_payment`` tool
    call (SPEC.md Section 10 "Tools"). Deliberately excludes job_id,
    mandate_id and obligation_id — those are runtime-injected from the
    assigned job context, never model-controlled, so a poisoned model has no
    field here through which it could redirect the call to a different job
    or mandate."""

    debit_account_id: str
    destination_account_id: str
    destination_version: PositiveInt
    amount_minor: Money
    currency: Literal["LKR"] = "LKR"
    payment_reference: str
