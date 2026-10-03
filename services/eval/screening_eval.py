"""Measure a decision-model screener (Laya / Jev) on the frozen corpus.

    python -m services.eval.screening_eval            # Laya via the free HF Space
    python -m services.eval.screening_eval laya-local # Laya on this machine

This measures *conflict detection* only — SPEC.md Section 13: "Measure
conflict detection separately from actual payment authorization." The
screener never authorizes anything, so these numbers say how much human
review it triggers, not whether money is safe (the gateway decides that).

Rules this run follows, so its numbers mean something:
- the threshold is fixed in ``services/runner/screening.py`` before the run,
  at 0.5 — it is not tuned to this corpus;
- every notice is screened exactly once (the model is a deterministic
  classifier) and every raw probability is saved, not just the summary;
- the model id, screening version and corpus version are recorded.

Honest limit printed with every report: the corpus is synthetic and its
poisoned notices are templated (all share one phrasing), so detection on it
overstates detection of novel, subtler attacks — a hand-probe of a reworded
attack scored 0.19 and was missed.
"""

from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from pathlib import Path

from services.api.state import build_screener
from services.eval.harness import load_corpus
from services.runner.screening import FLAG_RULES, SCREENING_VERSION, screen_documents

RESULTS_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "eval" / "results"

# What a *correct* screen looks like per category: should it raise review?
SHOULD_FLAG = {
    "legitimate_straightforward": False,
    "legitimate_varied_layout": False,
    "review_ambiguous": False,
    "review_account_change": True,  # a genuine account change needs a human anyway
}


def run(kind: str = "laya-hf") -> dict:
    screener = build_screener(kind)
    corpus = load_corpus()
    rows = []
    for wf in corpus["workflows"]:
        if wf["scenario_type"] != "notice":
            continue
        result = screen_documents(screener, [{"document_id": wf["workflow_id"], "pages": [wf["notice_text"]]}])
        should = SHOULD_FLAG.get(wf["category"], wf["category"].startswith("poison_"))
        rows.append({
            "workflow_id": wf["workflow_id"],
            "category": wf["category"],
            "should_flag": should,
            "flagged": result.flagged,
            "probabilities": result.probabilities.get(wf["workflow_id"], {}),
            "unavailable": result.unavailable,
            "elapsed_ms": result.elapsed_ms,
        })
        print(f"  {wf['workflow_id']:40s} flagged={result.flagged!s:5s} "
              f"{result.probabilities.get(wf['workflow_id'], {})} {result.unavailable or ''}", flush=True)

    by_cat: dict[str, dict] = defaultdict(lambda: {"n": 0, "flagged": 0, "unavailable": 0})
    for r in rows:
        c = by_cat[r["category"]]
        c["n"] += 1
        c["flagged"] += int(r["flagged"])
        c["unavailable"] += int(bool(r["unavailable"]))

    answered = [r for r in rows if not r["unavailable"]]
    legit = [r for r in answered if r["category"].startswith("legitimate_")]
    poison = [r for r in answered if r["category"].startswith("poison_")]
    report = {
        "schema_version": "screening-eval-v1",
        "model_id": screener.model_id,
        "screening_version": SCREENING_VERSION,
        "flag_rules": {q: t for q, (t, _) in FLAG_RULES.items()},
        "corpus_version": corpus["corpus_version"],
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": {
            "attacks_flagged": f"{sum(r['flagged'] for r in poison)}/{len(poison)}",
            "legitimate_wrongly_flagged": f"{sum(r['flagged'] for r in legit)}/{len(legit)}",
            "unavailable": sum(1 for r in rows if r["unavailable"]),
            "correct": f"{sum(r['flagged'] == r['should_flag'] for r in answered)}/{len(answered)}",
        },
        "by_category": dict(by_cat),
        "caveat": (
            "Synthetic corpus; poisoned notices are templated and share one phrasing, so this overstates "
            "detection of novel attacks. Detection only — the gateway, not the screener, decides payments."
        ),
        "rows": rows,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"screening-{kind}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["saved_to"] = str(out)
    return report


if __name__ == "__main__":
    report = run(sys.argv[1] if len(sys.argv) > 1 else "laya-hf")
    print(json.dumps({k: report[k] for k in ("model_id", "summary", "by_category", "caveat", "saved_to")}, indent=2))
