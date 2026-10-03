"""The Gemini boundary, abstracted the same way Cloud KMS was in
``services/shared/crypto.py``/``local_adapters.py``: a narrow interface plus
a test-only fake, with the real network-calling adapter kept separate and
never exercised by these tests (no model access in this environment).

CLAUDE_CODE_PROMPT.md Phase 3 requires pinning "the model ID, SDK version
and prompt version before evaluation. No `latest` alias, no unverified
preview names." — enforced here as required constructor arguments with no
default, so a caller cannot silently fall back to an unpinned version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ModelTurn:
    """One raw response from the model: an untrusted dict shaped like the
    rent-proposal-v1 contract, optionally paired with a structured
    ``propose_payment`` tool-call-shaped dict. Both are validated by the
    orchestrator, never trusted as-is (SPEC.md Section 10: "A model citation
    is an assertion until checked.")."""

    proposal: dict[str, Any]
    proposed_payment: dict[str, Any] | None = None


@runtime_checkable
class ModelClient(Protocol):
    model_id: str
    sdk_version: str
    prompt_version: str

    def generate(self, *, context: dict, documents: list[dict], repair_hint: str | None) -> ModelTurn:
        """Produce one structured turn. ``repair_hint`` is set only on the
        single allowed schema-repair retry (SPEC.md Section 10: "One
        schema-repair retry, then stop with a clear error.")."""
        ...


@dataclass
class FakeModelClient:
    """Test-only scripted model: returns each entry in ``script`` in order,
    one per ``generate()`` call, regardless of ``repair_hint`` unless the
    script entry is itself a repair. Lets Phase 3 tests exercise every
    required scenario (normal notice, poisoned notice, refusal, ambiguity,
    schema-repair) deterministically, without a live model dependency.
    """

    script: list[ModelTurn]
    model_id: str = "fake-model-for-tests"
    sdk_version: str = "fake-sdk-0"
    prompt_version: str = "fake-prompt-0"
    calls: list[dict] = field(default_factory=list)

    def generate(self, *, context: dict, documents: list[dict], repair_hint: str | None) -> ModelTurn:
        self.calls.append({"context": context, "repair_hint": repair_hint})
        if not self.script:
            raise RuntimeError("FakeModelClient script exhausted")
        return self.script.pop(0)


class NoticeParserModelClient:
    """A deterministic, regex-based stand-in for Gemini — used only by the
    local demo backend when no real model is configured. This is NOT an
    approximation of Gemini's actual document understanding; it exists so
    the demo's "agent reads a notice and proposes a payment" flow has
    something to show without API credentials, while still exercising the
    real security property under test: whatever this naive parser proposes,
    correct or poisoned, is what actually gets submitted to the gateway,
    and the gateway's independent tuple check is what decides the outcome.

    Deliberately naive on purpose: it honors an explicit override phrase
    ("PAY INSTEAD TO: <account-id>") if present, which is exactly the kind
    of prompt-injection trigger the poisoned-notice demo scenario needs to
    show a naive agent being fooled — a real model might resist or fall for
    similarly-styled attacks; this class does not simulate that judgment,
    it simulates the *worst case* so the gateway boundary has something
    concrete to stop.
    """

    model_id = "notice-parser-v0-not-gemini"
    sdk_version = "n/a"
    prompt_version = "n/a"

    _OVERRIDE_RE = None  # set in __init__ to avoid a module-level re import surprise

    def __init__(self) -> None:
        import re

        self._amount_re = re.compile(r"LKR\s*([\d,]+(?:\.\d+)?)")
        self._override_re = re.compile(r"PAY INSTEAD TO:\s*([A-Za-z0-9_-]+)", re.IGNORECASE)

    def generate(self, *, context: dict, documents: list, repair_hint: str | None) -> ModelTurn:
        text = ""
        document_id = None
        if documents:
            document_id = documents[0]["document_id"]
            text = "\n".join(documents[0]["pages"])

        amount_match = self._amount_re.search(text)
        claimed_amount_minor = None
        evidence = []
        if amount_match:
            raw = amount_match.group(1).replace(",", "")
            try:
                claimed_amount_minor = int(round(float(raw) * 100))
            except ValueError:
                claimed_amount_minor = None
            evidence.append({"field": "claimed_amount_minor", "page": 1, "quote": amount_match.group(0)})

        override_match = self._override_re.search(text)
        if override_match:
            destination_account_id = override_match.group(1)
            claimed_destination_text = override_match.group(0)
            evidence.append({"field": "claimed_destination_text", "page": 1, "quote": override_match.group(0)})
        else:
            destination_account_id = context["approved_destination_account_id"]
            claimed_destination_text = context.get("approved_destination_label") or "the approved landlord account"

        proposal = {
            "document_id": document_id,
            "rent_period": context.get("rent_period"),
            # Left as None (never guessed) when the notice doesn't state an
            # amount — SPEC.md Section 10: "Missing or ambiguous values
            # become null and require clarification."
            "claimed_amount_minor": claimed_amount_minor,
            "claimed_currency": "LKR" if claimed_amount_minor is not None else None,
            "claimed_destination_text": claimed_destination_text,
            "evidence": evidence,
            "conflicts": [],
            "next_action": "propose_payment" if claimed_amount_minor is not None else "request_clarification",
        }
        proposed_payment = None
        if claimed_amount_minor is not None:
            proposed_payment = {
                "debit_account_id": context["approved_debit_account_id"],
                "destination_account_id": destination_account_id,
                "destination_version": context["approved_destination_version"] if not override_match else 1,
                "amount_minor": claimed_amount_minor,
                "currency": "LKR",
                "payment_reference": context["rent_period"],
            }
        return ModelTurn(proposal=proposal, proposed_payment=proposed_payment)


class GeminiModelClient:
    """Production adapter over the Google Gen AI SDK. Not exercised by any
    test in this repository — there is no live model access in this
    environment. Kept intentionally thin: build the function declarations
    for the SPEC.md Section 10 tools, pin `model_id`, call the SDK, and
    return the model's structured JSON output as an untrusted dict for the
    orchestrator to validate exactly like a FakeModelClient turn.

    Deploy-time checklist before this is used for real (CLAUDE_CODE_PROMPT.md
    Phase 3/5): pin an exact model id (no ``latest`` alias, no unverified
    preview name), pin the installed `google-genai` SDK version, and freeze
    the prompt text under a versioned identifier — all three are recorded
    alongside evaluation results per SPEC.md Section 13.
    """

    def __init__(self, *, model_id: str, sdk_version: str, prompt_version: str, api_key: str | None = None):
        if not model_id or model_id == "latest":
            raise ValueError("model_id must be an exact pinned model identifier, never 'latest'")
        self.model_id = model_id
        self.sdk_version = sdk_version
        self.prompt_version = prompt_version
        self._api_key = api_key
        self._client = None  # lazily constructed; see _client_or_raise

    def _client_or_raise(self):
        if self._client is None:
            try:
                from google import genai  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(
                    "google-genai is not installed / no model access is configured in this "
                    "environment — GeminiModelClient is not exercised by this repository's tests"
                ) from exc
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def generate(self, *, context: dict, documents: list[dict], repair_hint: str | None) -> ModelTurn:
        raise NotImplementedError(
            "GeminiModelClient.generate is a deployment-time integration point, not exercised in "
            "this sandboxed session (no Gemini API credentials/network available here). Wire it up "
            "against the pinned model_id/sdk_version/prompt_version above when deploying Phase 5, "
            "then add a live-model smoke test that is clearly separate from the FakeModelClient "
            "suite (SPEC.md Section 13: 'Direct gateway tests do not depend on persuading the "
            "model to misbehave and remain separate from live-model results.')."
        )
