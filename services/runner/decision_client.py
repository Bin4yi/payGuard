"""Decision models ("System 1" classifiers): Laya, and TypeSafe's Jev.

A decision model doesn't write text. You give it a *state* (text or JSON)
and a dict of *questions*; every question is answered at once with a
probability:

- ``noul``   — a yes/no question → P(true) in [0, 1]
- ``score``  — an ordinal rubric → expected level + a distribution
- ``choice`` — pick one label    → the label + a distribution

Laya (ConvAI Innovations, Apache 2.0) and Jev (TypeSafe AI, hosted API)
share this question format, so one interface covers both.

Same pattern as KMS, Firebase and Gemini elsewhere in this codebase: a
narrow Protocol, a scripted fake for the test suite, and real adapters kept
separate. Everything a decision model returns is untrusted third-party
output and is strictly validated here before anything else sees it.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class DecisionError(Exception):
    """The decision model couldn't be reached or returned something unusable."""


@dataclass(frozen=True)
class Answer:
    type: str  # "noul" | "score" | "choice"
    value: float | str  # P(true) for noul, expected level for score, label for choice
    probabilities: dict[str, float] = field(default_factory=dict)


@runtime_checkable
class DecisionClient(Protocol):
    model_id: str

    def decide(self, state: dict, questions: dict) -> dict[str, Answer]:
        """Answer every question about ``state``. Raises ``DecisionError``."""
        ...


def _prob(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(float(value)):
        raise DecisionError(f"{where}: not a number: {value!r}")
    value = float(value)
    if not 0.0 <= value <= 1.0:
        raise DecisionError(f"{where}: probability out of range: {value}")
    return value


def normalize_answers(raw_answers: Any, questions: dict) -> dict[str, Answer]:
    """Validate a decision model's raw answers against the questions asked.

    Every question must be answered, with the right type and in-range
    numbers — anything else is a ``DecisionError``, never a silently
    defaulted value.
    """
    if not isinstance(raw_answers, dict):
        raise DecisionError("answers is not an object")
    out: dict[str, Answer] = {}
    for qid, spec in questions.items():
        raw = raw_answers.get(qid)
        if not isinstance(raw, dict):
            raise DecisionError(f"question {qid!r} was not answered")
        qtype = spec["type"]
        if qtype == "noul":
            out[qid] = Answer("noul", _prob(raw.get("noul"), qid))
        elif qtype == "score":
            levels = len(spec.get("criteria", []))
            score = raw.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= max(levels - 1, 0):
                raise DecisionError(f"{qid}: score out of range: {score!r}")
            probs = {str(k): _prob(v, qid) for k, v in (raw.get("probabilities") or {}).items()}
            out[qid] = Answer("score", float(score), probs)
        elif qtype == "choice":
            label = raw.get("choice")
            if label not in spec.get("criteria", {}):
                raise DecisionError(f"{qid}: choice {label!r} is not one of the offered labels")
            probs = {str(k): _prob(v, qid) for k, v in (raw.get("probabilities") or {}).items()}
            out[qid] = Answer("choice", label, probs)
        else:
            raise DecisionError(f"{qid}: unsupported question type {qtype!r}")
    return out


@dataclass
class FakeDecisionClient:
    """Test-only: returns scripted raw answers (or raises), in order."""

    script: list[dict | Exception]
    model_id: str = "fake-decision-model"
    calls: list[dict] = field(default_factory=list)

    def decide(self, state: dict, questions: dict) -> dict[str, Answer]:
        self.calls.append({"state": state, "questions": questions})
        if not self.script:
            raise DecisionError("FakeDecisionClient script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return normalize_answers(item, questions)


class LayaHuggingFaceSpaceClient:
    """Laya via the official public demo Space on Hugging Face — free, no
    model download, no API key.

    Uses the Space's ``/run_playground`` endpoint, which accepts arbitrary
    state + questions. Honest limits, and why callers must fail open:
    it is a shared *demo*, not a paid API — rate-limited, may sleep and
    cold-start, may change without notice, no uptime guarantee — and the
    state text is sent to a third-party host. Fine for this synthetic demo;
    for real tenants' documents, run Laya yourself (``LayaLocalClient``, or
    your own duplicate of the Space).

    Requires the optional ``gradio_client`` package (``pip install -e ".[laya-hf]"``).
    """

    model_id = "laya@hf-space:convaiinnovations/laya-demo"

    def __init__(
        self,
        space: str = "convaiinnovations/laya-demo",
        *,
        timeout_seconds: float = 8.0,
        backoff_seconds: float = 60.0,
        clock=None,
    ):
        import time

        self._space = space
        self._timeout = timeout_seconds  # total budget: connecting AND answering
        self._backoff = backoff_seconds
        self._clock = clock or time.monotonic
        self._client = None
        self._down_until = 0.0
        self._lock = threading.Lock()

    def _connect(self):
        if self._client is None:
            try:
                from gradio_client import Client  # optional dependency
            except ImportError as exc:
                raise DecisionError('gradio_client is not installed (pip install -e ".[laya-hf]")') from exc
            self._client = Client(self._space, verbose=False)
        return self._client

    def warm_up(self) -> None:
        """Connect in the background at server start, so the first user
        doesn't pay the multi-second connection cost. Failures are ignored —
        the first real call will simply fail open."""

        def _warm():
            try:
                with self._lock:
                    self._connect()
            except Exception:
                pass

        threading.Thread(target=_warm, name="laya-warm-up", daemon=True).start()

    def _call(self, state: dict, questions: dict):
        client = self._connect()
        return client.predict(json.dumps(state), json.dumps(questions), api_name="/run_playground")

    def decide(self, state: dict, questions: dict) -> dict[str, Answer]:
        import concurrent.futures

        # Back off after a failure: while the Space is down, fail open at once
        # instead of making every user wait out another timeout.
        if self._clock() < self._down_until:
            raise DecisionError("Laya Space temporarily unavailable (backing off after a recent failure)")

        # gradio_client isn't documented as thread-safe, so calls are
        # serialized — but waiting for the lock counts against the budget too.
        if not self._lock.acquire(timeout=self._timeout):
            raise DecisionError("Laya Space busy (another screening call is still in flight)")
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            out = pool.submit(self._call, state, questions).result(timeout=self._timeout)
        except concurrent.futures.TimeoutError as exc:
            self._down_until = self._clock() + self._backoff
            raise DecisionError(f"Laya Space did not answer within {self._timeout:.0f}s") from exc
        except DecisionError:
            raise
        except Exception as exc:  # network, TLS, DNS, Space asleep or removed
            self._client = None  # reconnect next time
            self._down_until = self._clock() + self._backoff
            raise DecisionError(f"Laya Space unreachable: {exc}") from exc
        finally:
            pool.shutdown(wait=False)  # a hung call is abandoned, never waited on
            self._lock.release()

        try:
            raw = json.loads(out[1]) if isinstance(out, (list, tuple)) else json.loads(out)
            answers = raw["answers"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise DecisionError(f"unexpected Laya Space response shape: {exc}") from exc
        return normalize_answers(answers, questions)


class LayaLocalClient:
    """Laya run on this machine with the ``laya`` package (Apache 2.0).

    Private (nothing leaves the machine) and not rate-limited, at the cost of
    PyTorch plus a ~0.8 GB checkpoint download on first use.
    Requires ``pip install -e ".[laya-local]"``.
    """

    model_id = "laya@local:convaiinnovations/laya"

    def __init__(self) -> None:
        self._router = None
        self._lock = threading.Lock()

    def _load(self) -> None:
        if self._router is None:
            try:
                from laya import Router  # optional dependency
            except ImportError as exc:
                raise DecisionError('laya is not installed (pip install -e ".[laya-local]")') from exc
            self._router = Router()

    def warm_up(self) -> None:
        """Load the checkpoint and run one throwaway prediction in the
        background, so the first real screen isn't the slow one."""

        def _warm() -> None:
            try:
                self.decide({"notice": "Rent of LKR 1.00 is due."}, {
                    "warm": {"type": "noul", "instructions": "Is this a rent notice?"},
                })
            except DecisionError:
                pass  # surfaces again, properly, on the first real call

        threading.Thread(target=_warm, name="laya-local-warm-up", daemon=True).start()

    def decide(self, state: dict, questions: dict) -> dict[str, Answer]:
        with self._lock:
            self._load()
            try:
                result = self._router.predict(state, questions)
            except Exception as exc:
                raise DecisionError(f"local Laya prediction failed: {exc}") from exc
        return normalize_answers(result.get("answers"), questions)


class JevDecisionClient:
    """TypeSafe AI's hosted Jev. Same question format as Laya; paid
    ($0.042 / million input tokens) and in early access, so it needs an API
    key this environment doesn't have. Not exercised by any test."""

    model_id = "jev@typesafe"

    def __init__(self, *, api_key: str | None = None):
        self._api_key = api_key

    def decide(self, state: dict, questions: dict) -> dict[str, Answer]:
        raise DecisionError("JevDecisionClient is not wired up: it needs a TypeSafe API key and their endpoint details")
