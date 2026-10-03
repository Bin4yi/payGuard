"""Pre-screening rent notices with a decision model (Laya / Jev), before
Gemini ever reads them.

**One-directional by construction.** A screener is itself an ML model and
can be fooled, so it may only ever *add* a review flag — it can never clear
one, approve anything, or let a payment through that would otherwise be
stopped. The asymmetry is the whole design:

- screener flags a notice   → the job goes to human review, no Gemini call
  is spent, no payment is attempted;
- screener misses an attack → nothing changes: Gemini, the orchestrator's
  review gate and the gateway's exact-tuple check all still apply;
- screener is down/garbled  → fail open to exactly those same layers, and
  record that screening was unavailable.

So the screener can cost a legitimate payment a human review (a false
positive), but it can never cost security. SPEC.md Section 10, item 6: "A
deterministic review flag raised by validated workflow checks stops
automatic submission." The flag rule itself (``probability >= threshold``)
is ordinary deterministic code; only the input probability comes from the
model.

Question choice was probed against the live Laya Space before being fixed
here. Two plausible questions were rejected as unreliable: "instructions
aimed at an AI?" scored a normal "[Automated reminder]" notice at 1.0, and
"differs from the approved payee?" scored a plain invoice at 0.72. The one
kept separated normal notices (0.15–0.40) from redirect attempts
(0.84–0.93) — but also missed a subtle rewording at 0.19, which is exactly
why this layer is advisory. The threshold is the natural 0.5 decision point
for a probability, fixed in advance rather than tuned to examples.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from services.runner.decision_client import Answer, DecisionClient, DecisionError

SCREENING_VERSION = "screen-v1"

QUESTIONS: dict[str, dict] = {
    "redirects_payment": {
        "type": "noul",
        "instructions": "Does the text try to change the bank account or payee that payment should be sent to?",
    },
}

# Question id -> (threshold, human-readable reason). Only questions listed
# here can raise a flag; anything else a model returns is informational.
FLAG_RULES: dict[str, tuple[float, str]] = {
    "redirects_payment": (0.5, "the notice appears to redirect payment to a different account or payee"),
}

MAX_CHARS_PER_DOCUMENT = 8_000  # bound what is sent to a third-party model


@dataclass(frozen=True)
class ScreeningFlag:
    document_id: str
    question: str
    probability: float
    threshold: float
    reason: str


@dataclass
class ScreeningResult:
    model_id: str
    version: str = SCREENING_VERSION
    flags: list[ScreeningFlag] = field(default_factory=list)
    # document_id -> {question: probability}; recorded as untrusted activity.
    probabilities: dict[str, dict[str, float]] = field(default_factory=dict)
    unavailable: str | None = None  # set when screening failed open
    elapsed_ms: int = 0

    @property
    def flagged(self) -> bool:
        return bool(self.flags)


def screen_documents(client: DecisionClient, documents: list[dict]) -> ScreeningResult:
    """Screen each assigned document. Never raises: any failure fails open."""
    start = time.monotonic()
    result = ScreeningResult(model_id=getattr(client, "model_id", "unknown"))
    for doc in documents:
        text = "\n".join(doc.get("pages", []))[:MAX_CHARS_PER_DOCUMENT]
        if not text.strip():
            continue
        try:
            answers: dict[str, Answer] = client.decide({"notice": text}, QUESTIONS)
        except DecisionError as exc:
            result.unavailable = str(exc)
            break
        except Exception as exc:  # a third-party client must never break the run
            result.unavailable = f"unexpected screener error: {exc}"
            break
        probs = {qid: float(a.value) for qid, a in answers.items() if a.type == "noul"}
        result.probabilities[doc["document_id"]] = probs
        for qid, (threshold, reason) in FLAG_RULES.items():
            p = probs.get(qid)
            if p is not None and p >= threshold:
                result.flags.append(ScreeningFlag(doc["document_id"], qid, p, threshold, reason))
    result.elapsed_ms = int((time.monotonic() - start) * 1000)
    return result
