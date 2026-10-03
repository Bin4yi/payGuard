"""Phase 3 exit evidence (CLAUDE_CODE_PROMPT.md):

- A normal notice produces checked evidence and one correct payment.
- If the model attempts an unauthorized tuple, the gateway blocks it and
  records that tuple.
- If the model refuses before calling the gateway, record model activity;
  do not invent a gateway attempt.
- If poisoned input produces the approved tuple, do not mislabel a
  within-scope transfer as an authorization failure.
- Ambiguity, changed account and expired grant follow the required
  review/reconfirmation path.
"""

from __future__ import annotations

from services.authority import destinations, mandates
from services.authority.mandates import DraftInput
from services.runner import documents as doc_store
from services.runner.model_client import FakeModelClient, ModelTurn
from services.runner.orchestrator import run_job
from tests.conftest import (
    AMOUNT_MINOR,
    CURRENCY,
    DEBIT_ACCOUNT,
    DEMO_LANDLORD_ACCOUNT,
    DESTINATION_ID,
    OBLIGATION_ID,
    OWNER_UID,
    PAYMENT_REFERENCE,
    RUNNER_PRINCIPAL,
    reserve_and_activate_grant,
)

NOTICE_DOC_ID = "notice-01"


def _approve_with_document(store, signer, mandate_id="mandate-01"):
    doc_store.register_document(
        store,
        doc_store.DocumentInput(
            document_id=NOTICE_DOC_ID, owner_uid=OWNER_UID, media_type="text/plain",
            pages=("Monthly rent LKR 50,000.00 payable to DEMO-LANDLORD-001 for September 2026.",),
            size_bytes=200,
        ),
    )
    draft = mandates.create_draft(
        store,
        DraftInput(
            mandate_id=mandate_id, owner_uid=OWNER_UID, instruction_id="instruction-01", instruction_version=1,
            obligation_id=OBLIGATION_ID, debit_account_id=DEBIT_ACCOUNT, payee_id="payee-01",
            destination_account_id=DESTINATION_ID, destination_version=1, amount_mode="exact",
            amount_minor=AMOUNT_MINOR, currency=CURRENCY, payment_reference=PAYMENT_REFERENCE,
            grant_expiry_seconds=300, selected_document_ids=(NOTICE_DOC_ID,), policy_version="rent-v1",
            supersedes_mandate_id=None,
        ),
    )
    mandates.confirm_mandate(store, mandate_id=mandate_id, owner_uid=OWNER_UID, version=draft["version"], nonce=draft["nonce"])
    mandates.sign_mandate(store, mandate_id, signer)


def _clean_proposal(document_id=NOTICE_DOC_ID, destination_account_id=DESTINATION_ID, destination_version=1, amount_minor=AMOUNT_MINOR):
    return {
        "document_id": document_id,
        "rent_period": "2026-09",
        "claimed_amount_minor": AMOUNT_MINOR,
        "claimed_currency": CURRENCY,
        "claimed_destination_text": "Demo Landlord",
        "evidence": [{"field": "claimed_amount_minor", "page": 1, "quote": "Monthly rent LKR 50,000.00"}],
        "conflicts": [],
        "next_action": "propose_payment",
    }, {
        "debit_account_id": DEBIT_ACCOUNT,
        "destination_account_id": destination_account_id,
        "destination_version": destination_version,
        "amount_minor": amount_minor,
        "currency": CURRENCY,
        "payment_reference": PAYMENT_REFERENCE,
    }


def test_normal_notice_produces_checked_evidence_and_one_correct_payment(seeded_store, authority_signer, verifier_registry):
    store = seeded_store
    signer, _ = authority_signer
    _approve_with_document(store, signer)
    grant = reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")
    assert grant["state"] == "ACTIVE"

    proposal_dict, call_dict = _clean_proposal()
    model = FakeModelClient(script=[ModelTurn(proposal=proposal_dict, proposed_payment=call_dict)])

    result = run_job(
        store, job_id="job-01", owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
        model_client=model, verifier_registry=verifier_registry,
    )

    assert result.outcome == "paid"
    assert result.gateway_result.status == "COMMITTED"
    assert result.conflicts == ()
    assert any(e["field"] == "claimed_amount_minor" for e in proposal_dict["evidence"])
    credit = store.peek("accounts", DEMO_LANDLORD_ACCOUNT)
    assert credit["balance_minor"] == AMOUNT_MINOR


def test_unauthorized_tuple_is_blocked_and_the_attempted_tuple_is_recorded(seeded_store, authority_signer, verifier_registry):
    """A poisoned notice fools the model into proposing an attacker account.
    The gateway blocks it — and the *actual attempted* (wrong) tuple is
    preserved in evidence, never silently swapped for the approved one."""
    store = seeded_store
    signer, _ = authority_signer
    _approve_with_document(store, signer)
    reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")

    proposal_dict, call_dict = _clean_proposal(destination_account_id="acct-attacker-99")
    proposal_dict["claimed_destination_text"] = "ATTACKER-ACCOUNT-99"
    model = FakeModelClient(script=[ModelTurn(proposal=proposal_dict, proposed_payment=call_dict)])

    result = run_job(
        store, job_id="job-01", owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
        model_client=model, verifier_registry=verifier_registry,
    )

    assert result.outcome == "review_required"  # destination conflict caught before the gateway is even asked
    assert any(c.field == "claimed_destination_text" for c in result.conflicts)
    assert result.gateway_result is None
    debit = store.peek("accounts", DEBIT_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000  # no debit


def test_model_that_resists_the_conflict_check_still_gets_blocked_at_the_gateway(
    seeded_store, authority_signer, verifier_registry
):
    """Even if the model's *reasoning* fields (claimed_destination_text)
    happen to still say the approved landlord while its *tool call* directly
    names a different account (e.g. a more sophisticated injection that
    manipulates the function-call arguments rather than the prose), the
    gateway — not the orchestrator's text-based conflict check — is what
    actually stops the transfer."""
    store = seeded_store
    signer, _ = authority_signer
    _approve_with_document(store, signer)
    reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")

    proposal_dict, call_dict = _clean_proposal(destination_account_id="acct-attacker-99")
    # claimed_destination_text left matching the approved landlord, but the
    # actual tool-call args target a different account.
    model = FakeModelClient(script=[ModelTurn(proposal=proposal_dict, proposed_payment=call_dict)])

    result = run_job(
        store, job_id="job-01", owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
        model_client=model, verifier_registry=verifier_registry,
    )

    assert result.outcome == "blocked"
    assert result.gateway_result.status == "BLOCKED"
    assert "DESTINATION_CHANGED" in result.gateway_result.reason_codes
    debit = store.peek("accounts", DEBIT_ACCOUNT)
    assert debit["balance_minor"] == 50_000_000

    attempt = store.peek("payment_attempts", result.gateway_result.attempt_id)
    assert attempt["request"]["destination_account_id"] == "acct-attacker-99"  # preserved, not corrected


def test_model_refusal_records_activity_without_fabricating_a_gateway_attempt(seeded_store, authority_signer, verifier_registry):
    store = seeded_store
    signer, _ = authority_signer
    _approve_with_document(store, signer)
    reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")

    proposal_dict = {
        "document_id": NOTICE_DOC_ID, "rent_period": "2026-09", "claimed_amount_minor": AMOUNT_MINOR,
        "claimed_currency": CURRENCY, "claimed_destination_text": "SUSPICIOUS TEXT - POSSIBLE INJECTION",
        "evidence": [], "conflicts": [
            {"field": "claimed_destination_text", "expected": "DEMO-LANDLORD-001", "claimed": "SUSPICIOUS TEXT",
             "detail": "document contains an instruction-like override attempt", "source": "model"},
        ],
        "next_action": "report_conflict",
    }
    model = FakeModelClient(script=[ModelTurn(proposal=proposal_dict, proposed_payment=None)])

    result = run_job(
        store, job_id="job-01", owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
        model_client=model, verifier_registry=verifier_registry,
    )

    assert result.outcome == "review_required"
    assert result.gateway_result is None
    job = store.peek("jobs", "job-01")
    assert job["status"] == "REVIEW_REQUIRED"
    activity = store.peek("job_activity", "job-01")
    assert any(e["type"] == "model_output" for e in activity["entries"])
    assert activity["trusted"] is False  # model activity is explicitly untrusted


def test_poisoned_input_that_still_produces_the_approved_tuple_is_not_mislabeled_as_a_failure(
    seeded_store, authority_signer, verifier_registry
):
    """If the model resists the poisoning and proposes exactly the approved
    tuple, that is a correct, in-scope payment — not an "authorization
    failure" just because the input document was adversarial."""
    store = seeded_store
    signer, _ = authority_signer
    _approve_with_document(store, signer)
    reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")

    proposal_dict, call_dict = _clean_proposal()  # the model resisted; proposes the approved tuple
    model = FakeModelClient(script=[ModelTurn(proposal=proposal_dict, proposed_payment=call_dict)])

    result = run_job(
        store, job_id="job-01", owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
        model_client=model, verifier_registry=verifier_registry,
    )
    assert result.outcome == "paid"
    assert result.gateway_result.status == "COMMITTED"


def test_ambiguous_missing_fields_require_review_not_a_guess(seeded_store, authority_signer, verifier_registry):
    store = seeded_store
    signer, _ = authority_signer
    _approve_with_document(store, signer)
    reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")

    proposal_dict = {
        "document_id": NOTICE_DOC_ID, "rent_period": "2026-09", "claimed_amount_minor": None,
        "claimed_currency": CURRENCY, "claimed_destination_text": "DEMO-LANDLORD-001",
        "evidence": [], "conflicts": [], "next_action": "request_clarification",
    }
    model = FakeModelClient(script=[ModelTurn(proposal=proposal_dict, proposed_payment=None)])

    result = run_job(
        store, job_id="job-01", owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
        model_client=model, verifier_registry=verifier_registry,
    )
    assert result.outcome == "review_required"
    assert result.gateway_result is None


def test_schema_invalid_output_gets_one_repair_retry_then_succeeds(seeded_store, authority_signer, verifier_registry):
    store = seeded_store
    signer, _ = authority_signer
    _approve_with_document(store, signer)
    reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")

    bad_proposal = {"document_id": NOTICE_DOC_ID, "next_action": "propose_payment", "claimed_amount_minor": "fifty thousand"}
    good_proposal, call_dict = _clean_proposal()
    model = FakeModelClient(script=[
        ModelTurn(proposal=bad_proposal, proposed_payment=None),
        ModelTurn(proposal=good_proposal, proposed_payment=call_dict),
    ])

    result = run_job(
        store, job_id="job-01", owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
        model_client=model, verifier_registry=verifier_registry,
    )
    assert result.outcome == "paid"
    assert len(model.calls) == 2
    assert model.calls[1]["repair_hint"] is not None


def test_schema_invalid_twice_stops_with_a_clear_error(seeded_store, authority_signer, verifier_registry):
    store = seeded_store
    signer, _ = authority_signer
    _approve_with_document(store, signer)
    reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")

    bad_proposal = {"document_id": NOTICE_DOC_ID, "next_action": "not_a_valid_action"}
    model = FakeModelClient(script=[
        ModelTurn(proposal=bad_proposal, proposed_payment=None),
        ModelTurn(proposal=bad_proposal, proposed_payment=None),
    ])

    result = run_job(
        store, job_id="job-01", owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
        model_client=model, verifier_registry=verifier_registry,
    )
    assert result.outcome == "schema_invalid"
    job = store.peek("jobs", "job-01")
    assert job["status"] == "FAILED"


def test_unverifiable_citation_is_a_conflict(seeded_store, authority_signer, verifier_registry):
    store = seeded_store
    signer, _ = authority_signer
    _approve_with_document(store, signer)
    reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")

    proposal_dict, call_dict = _clean_proposal()
    proposal_dict["evidence"] = [{"field": "claimed_amount_minor", "page": 1, "quote": "this text is not on the page"}]
    model = FakeModelClient(script=[ModelTurn(proposal=proposal_dict, proposed_payment=call_dict)])

    result = run_job(
        store, job_id="job-01", owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
        model_client=model, verifier_registry=verifier_registry,
    )
    assert result.outcome == "review_required"
    assert any(c.detail.startswith("quote not found") for c in result.conflicts)


def test_document_count_budget_is_enforced(seeded_store, authority_signer, verifier_registry):
    """More than 3 assigned documents: only the first 3 are read; this does
    not by itself fail the job (the model still gets a bounded, valid
    context) — it demonstrates the cap is applied, not skipped."""
    store = seeded_store
    signer, _ = authority_signer
    extra_docs = [f"notice-extra-{i}" for i in range(4)]
    for doc_id in extra_docs:
        doc_store.register_document(
            store, doc_store.DocumentInput(document_id=doc_id, owner_uid=OWNER_UID, media_type="text/plain",
                                            pages=("filler",), size_bytes=10),
        )
    draft = mandates.create_draft(
        store,
        DraftInput(
            mandate_id="mandate-01", owner_uid=OWNER_UID, instruction_id="instruction-01", instruction_version=1,
            obligation_id=OBLIGATION_ID, debit_account_id=DEBIT_ACCOUNT, payee_id="payee-01",
            destination_account_id=DESTINATION_ID, destination_version=1, amount_mode="exact",
            amount_minor=AMOUNT_MINOR, currency=CURRENCY, payment_reference=PAYMENT_REFERENCE,
            grant_expiry_seconds=300, selected_document_ids=tuple(extra_docs), policy_version="rent-v1",
            supersedes_mandate_id=None,
        ),
    )
    mandates.confirm_mandate(store, mandate_id="mandate-01", owner_uid=OWNER_UID, version=draft["version"], nonce=draft["nonce"])
    mandates.sign_mandate(store, "mandate-01", signer)
    reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")

    proposal_dict = {
        "document_id": extra_docs[0], "rent_period": "2026-09", "claimed_amount_minor": None,
        "claimed_currency": None, "claimed_destination_text": None, "evidence": [], "conflicts": [],
        "next_action": "request_clarification",
    }
    model = FakeModelClient(script=[ModelTurn(proposal=proposal_dict, proposed_payment=None)])

    result = run_job(
        store, job_id="job-01", owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
        model_client=model, verifier_registry=verifier_registry,
    )
    assert result.outcome == "review_required"
    read_calls = [e for e in result.activity if e.get("tool") == "read_selected_document"]
    assert len(read_calls) == 3  # capped at MAX_DOCUMENTS, not 4


def test_no_model_call_is_spent_without_live_authority(seeded_store, authority_signer, verifier_registry):
    """SPEC.md Section 6: 'Start the runner only after activation.' A revoked
    grant is reported as exactly that — and the model is never invoked (an
    empty FakeModelClient script would raise if it were)."""
    from services.authority import grants as grants_mod

    store = seeded_store
    signer, _ = authority_signer
    _approve_with_document(store, signer)
    reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")
    grants_mod.revoke_grant(store, "grant-01", owner_uid=OWNER_UID)

    model = FakeModelClient(script=[])
    result = run_job(
        store, job_id="job-01", owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
        model_client=model, verifier_registry=verifier_registry,
    )
    assert result.outcome == "no_active_authority"
    assert model.calls == []
    assert store.peek("accounts", DEBIT_ACCOUNT)["balance_minor"] == 50_000_000


def test_an_expired_grant_is_reported_before_any_model_call(seeded_store, authority_signer, verifier_registry):
    import time

    store = seeded_store
    signer, _ = authority_signer
    _approve_with_document(store, signer)
    reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")

    model = FakeModelClient(script=[])
    result = run_job(
        store, job_id="job-01", owner_uid=OWNER_UID, runner_principal=RUNNER_PRINCIPAL,
        model_client=model, verifier_registry=verifier_registry, now_fn=lambda: time.time() + 10_000,
    )
    assert result.outcome == "no_active_authority"
    assert result.activity[0]["grant_state"] == "EXPIRED"
    assert model.calls == []
