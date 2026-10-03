"""Tests the SPEC.md Section 13 evaluation harness *mechanism* — corpus
loading, per-trial isolation, and denominator bucketing — against a small
subset of the frozen corpus. This is deliberately NOT a full 720-trial run
(that takes ~3 minutes; run ``python -m services.eval.harness`` by hand for
the full dry run). Nothing here is a SPEC.md Section 13 evaluation result —
see services/eval/harness.py's module docstring for why.
"""

from __future__ import annotations

from services.eval.harness import load_corpus, run_harness


def test_corpus_is_frozen_at_80_workflows_with_the_required_category_counts():
    corpus = load_corpus()
    workflows = corpus["workflows"]
    assert len(workflows) == 80

    by_category: dict[str, int] = {}
    for wf in workflows:
        by_category[wf["category"]] = by_category.get(wf["category"], 0) + 1

    assert by_category["legitimate_straightforward"] == 30
    assert by_category["legitimate_varied_layout"] == 10
    assert by_category["review_account_change"] == 4
    assert by_category["review_ambiguous"] == 3
    assert by_category["review_expired_grant"] == 3
    poison_total = sum(v for k, v in by_category.items() if k.startswith("poison_"))
    assert poison_total == 30
    assert len({k for k in by_category if k.startswith("poison_")}) == 6  # six distinct families


def _subset_corpus(*, per_category: int = 1) -> dict:
    corpus = load_corpus()
    seen: dict[str, int] = {}
    kept = []
    for wf in corpus["workflows"]:
        n = seen.get(wf["category"], 0)
        if n < per_category:
            kept.append(wf)
            seen[wf["category"]] = n + 1
    return {**corpus, "workflows": kept}


def test_configuration_c_blocks_every_out_of_scope_attempt_the_baseline_lets_through():
    """The core property the harness exists to demonstrate: against the
    same naive stand-in model, configurations A/B (no scope enforcement)
    let a poisoned proposal actually pay an unauthorized destination, while
    configuration C (the real gateway) produces zero unauthorized transfers
    — regardless of how the model behaved.
    """
    subset = _subset_corpus(per_category=1)
    reports = run_harness(repeats=1, corpus=subset)

    assert reports["C"].unauthorized_transfers == 0
    assert reports["C"].unauthorized_proposals == 0
    # The baseline (A/B) must actually be capable of paying an
    # out-of-scope destination — otherwise it is a strawman, not a
    # meaningful comparison (SPEC.md Section 13).
    assert reports["A"].unauthorized_transfers > 0
    assert reports["A"].unauthorized_transfers == reports["A"].attack_total
    assert reports["B"].unauthorized_transfers == reports["A"].unauthorized_transfers


def test_all_three_configurations_complete_legitimate_workflows_automatically():
    subset = _subset_corpus(per_category=1)
    reports = run_harness(repeats=1, corpus=subset)
    for name in ("A", "B", "C"):
        r = reports[name]
        assert r.automatic_legitimate_total > 0
        assert r.automatic_legitimate_completions == r.automatic_legitimate_total


def test_configuration_c_correctly_requires_review_for_every_review_workflow():
    subset = _subset_corpus(per_category=1)
    reports = run_harness(repeats=1, corpus=subset)
    r = reports["C"]
    assert r.review_required_total > 0
    assert r.review_required_correct == r.review_required_total
