"""The SPEC.md Section 12 endpoints that previously did not exist, plus the
service-credential boundary that now guards every internal route.

Two caller kinds must never be interchangeable: a user's session token is
refused on service routes, and a service credential is refused on user
routes. Every internal route also checks *which* service is calling.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from services.api.app import create_app
from services.api.state import AUTHORITY_PRINCIPAL, RUNNER_PRINCIPAL, SCHEDULER_PRINCIPAL, build_local_demo_state
from services.receipts import export as receipt_export


@pytest.fixture
def env():
    state = build_local_demo_state()
    return TestClient(create_app(state)), state


def _user(client):
    session = client.post("/v1/demo/anonymous-session").json()
    return {"Authorization": f"Bearer {session['id_token']}"}


def _svc(state, principal):
    return {"Authorization": f"Bearer {state.service_verifier.token_for(principal)}"}


def _approved_job(client, headers, *, notice="Rent notice: LKR 50,000.00 for September 2026."):
    scenario = client.post("/v1/demo/scenarios", headers=headers).json()
    doc_id = client.post("/v1/documents", headers=headers, json={"pages": [notice]}).json()["document_id"]
    mandate = client.post("/v1/mandates", headers=headers, json={"document_ids": [doc_id]}).json()
    client.post(f"/v1/mandates/{mandate['mandate_id']}/confirm", headers=headers,
                json={"version": mandate["version"], "nonce": mandate["nonce"]})
    job = client.post("/v1/jobs", headers=headers, json={"mandate_id": mandate["mandate_id"]}).json()
    return scenario, mandate, job


def _request_body(mandate, job, **overrides):
    body = {
        "job_id": job["job_id"],
        "mandate_id": mandate["mandate_id"],
        "obligation_id": mandate["obligation_id"],
        "debit_account_id": mandate["debit_account_id"],
        "destination_account_id": mandate["destination_account_id"],
        "destination_version": mandate["destination_version"],
        "amount_minor": mandate["amount_minor"],
        "currency": mandate["currency"],
        "payment_reference": mandate["payment_reference"],
    }
    body.update(overrides)
    return body


def _runner_grant(client, state, job):
    resp = client.get(f"/v1/internal/jobs/{job['job_id']}/grant", headers=_svc(state, RUNNER_PRINCIPAL))
    assert resp.status_code == 200, resp.text
    return resp.json()["grant_token"]


def _execute(client, state, grant_token, body, *, key="idem-1", principal=RUNNER_PRINCIPAL, raw=None):
    headers = {**_svc(state, principal), "X-PayGuard-Grant": grant_token, "Idempotency-Key": key,
               "Content-Type": "application/json"}
    content = raw if raw is not None else json.dumps(body)
    return client.post("/v1/payments/execute", headers=headers, content=content)


# ---- the caller-kind boundary ----


def test_internal_routes_refuse_user_sessions_and_anonymous_callers(env):
    client, state = env
    user = _user(client)
    for method, path in [
        ("post", "/v1/internal/authority/drain"),
        ("post", "/v1/internal/receipts/drain"),
        ("get", "/v1/internal/jobs/any/context"),
        ("get", "/v1/internal/jobs/any/grant"),
        ("post", "/v1/payments/execute"),
    ]:
        assert getattr(client, method)(path).status_code == 401, path
        assert getattr(client, method)(path, headers=user).status_code == 401, path


def test_user_routes_refuse_service_credentials(env):
    client, state = env
    resp = client.post("/v1/demo/scenarios", headers=_svc(state, RUNNER_PRINCIPAL))
    assert resp.status_code == 401


def test_internal_routes_check_which_service_is_calling(env):
    client, state = env
    # The runner may not trigger scheduler work or seed balances.
    assert client.post("/v1/internal/authority/drain", headers=_svc(state, RUNNER_PRINCIPAL)).status_code == 403
    assert client.post("/v1/internal/receipts/drain", headers=_svc(state, RUNNER_PRINCIPAL)).status_code == 403
    assert client.post("/v1/internal/demo/scenarios/x/seed", headers=_svc(state, SCHEDULER_PRINCIPAL)).status_code == 403
    assert client.post("/v1/internal/authority/drain", headers=_svc(state, SCHEDULER_PRINCIPAL)).status_code == 200


# ---- runner-facing: context and grant ----


def test_runner_gets_only_its_own_assigned_grant_and_context(env):
    client, state = env
    user = _user(client)
    _, mandate, job = _approved_job(client, user)

    ctx = client.get(f"/v1/internal/jobs/{job['job_id']}/context", headers=_svc(state, RUNNER_PRINCIPAL))
    assert ctx.status_code == 200
    grant = state.store.peek("grants", job["grant_id"])
    assert grant["compact_jws"] not in ctx.text  # context never carries a credential

    token = _runner_grant(client, state, job)
    assert token == grant["compact_jws"]

    # Another service is not the assigned runner: 404, not even an existence hint.
    other = client.get(f"/v1/internal/jobs/{job['job_id']}/grant", headers=_svc(state, SCHEDULER_PRINCIPAL))
    assert other.status_code == 404


def test_grant_endpoint_never_returns_an_inactive_grant(env):
    client, state = env
    user = _user(client)
    _, mandate, job = _approved_job(client, user)
    client.post(f"/v1/grants/{job['grant_id']}/revoke", headers=user)
    resp = client.get(f"/v1/internal/jobs/{job['job_id']}/grant", headers=_svc(state, RUNNER_PRINCIPAL))
    assert resp.status_code == 409


# ---- the gateway over HTTP ----


def test_execute_commits_an_authorized_payment_over_http(env):
    client, state = env
    user = _user(client)
    scenario, mandate, job = _approved_job(client, user)
    token = _runner_grant(client, state, job)

    resp = _execute(client, state, token, _request_body(mandate, job))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "COMMITTED"
    assert resp.json()["receipt_status"] == "PENDING"


def test_execute_rejects_duplicate_json_keys_before_parsing_can_lose_one(env):
    """The classic smuggling trick: send amount_minor twice and hope the
    validator sees one value while something else uses the other. The raw
    body is checked for duplicates before any normal JSON handling."""
    client, state = env
    user = _user(client)
    _, mandate, job = _approved_job(client, user)
    token = _runner_grant(client, state, job)

    body = json.dumps(_request_body(mandate, job))
    smuggled = body[:-1] + ', "amount_minor": 99999999}'
    resp = _execute(client, state, token, None, raw=smuggled)
    assert resp.status_code == 422
    assert "duplicate" in resp.text.lower()


@pytest.mark.parametrize("bad", [5000000.0, "5000000", True, 0, -1, 100_000_001])
def test_execute_rejects_ambiguous_amounts(env, bad):
    client, state = env
    user = _user(client)
    _, mandate, job = _approved_job(client, user)
    token = _runner_grant(client, state, job)
    resp = _execute(client, state, token, _request_body(mandate, job, amount_minor=bad))
    assert resp.status_code == 422


def test_execute_rejects_an_idempotency_key_smuggled_into_the_body(env):
    client, state = env
    user = _user(client)
    _, mandate, job = _approved_job(client, user)
    token = _runner_grant(client, state, job)
    resp = _execute(client, state, token, _request_body(mandate, job, idempotency_key="body-key"))
    assert resp.status_code == 422


def test_execute_requires_both_headers(env):
    client, state = env
    user = _user(client)
    _, mandate, job = _approved_job(client, user)
    token = _runner_grant(client, state, job)
    no_key = client.post("/v1/payments/execute", headers={**_svc(state, RUNNER_PRINCIPAL), "X-PayGuard-Grant": token},
                         json=_request_body(mandate, job))
    assert no_key.status_code == 422
    no_grant = client.post("/v1/payments/execute", headers={**_svc(state, RUNNER_PRINCIPAL), "Idempotency-Key": "k"},
                           json=_request_body(mandate, job))
    assert no_grant.status_code == 401


def test_execute_blocks_an_altered_destination_with_403(env):
    client, state = env
    user = _user(client)
    _, mandate, job = _approved_job(client, user)
    token = _runner_grant(client, state, job)
    resp = _execute(client, state, token, _request_body(mandate, job, destination_account_id="acct-attacker-99"))
    assert resp.status_code == 403
    assert "DESTINATION_CHANGED" in resp.json()["reason_codes"]


def test_execute_uses_the_verified_principal_never_a_claimed_one(env):
    """A different, fully authenticated service presenting the runner's
    grant is still refused: the grant names its agent principal."""
    client, state = env
    user = _user(client)
    _, mandate, job = _approved_job(client, user)
    token = _runner_grant(client, state, job)
    resp = _execute(client, state, token, _request_body(mandate, job), principal=SCHEDULER_PRINCIPAL)
    assert resp.status_code == 403
    assert "PRINCIPAL_MISMATCH" in resp.json()["reason_codes"]


def test_execute_replay_with_a_new_key_returns_the_same_payment(env):
    client, state = env
    user = _user(client)
    scenario, mandate, job = _approved_job(client, user)
    token = _runner_grant(client, state, job)
    first = _execute(client, state, token, _request_body(mandate, job), key="k1").json()
    second = _execute(client, state, token, _request_body(mandate, job), key="k2")
    assert second.status_code == 200
    assert second.json()["payment_id"] == first["payment_id"]
    balance = client.get("/v1/demo/scenarios/current", headers=user).json()["tenant_balance_minor"]
    assert balance == 50_000_000 - scenario["amount_minor"]  # debited exactly once


# ---- receipts: drain, read-only trail, export ----


def test_receipt_trail_is_read_only_and_the_drain_signs(env):
    client, state = env
    user = _user(client)
    _, mandate, job = _approved_job(client, user)
    token = _runner_grant(client, state, job)
    _execute(client, state, token, _request_body(mandate, job))

    before = client.get(f"/v1/jobs/{job['job_id']}/receipts", headers=user).json()
    assert before["entries"] == []           # nothing signed yet — and reading didn't sign it
    assert before["completeness_unknown"] is True

    drained = client.post("/v1/internal/receipts/drain", headers=_svc(state, SCHEDULER_PRINCIPAL)).json()
    assert drained["receipts_signed"] == 1

    after = client.get(f"/v1/jobs/{job['job_id']}/receipts", headers=user).json()
    assert [e["status"] for e in after["entries"]] == ["VERIFIED"]
    assert after["completeness_unknown"] is False


def test_receipt_export_verifies_and_carries_no_credentials(env):
    client, state = env
    user = _user(client)
    _, mandate, job = _approved_job(client, user)
    client.post(f"/v1/jobs/{job['job_id']}/run", headers=user)

    resp = client.post(f"/v1/jobs/{job['job_id']}/receipt-export", headers=user)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["verification"]["all_verified"] is True
    assert payload["verification"]["mandate_status"] == "VERIFIED"

    grant_token = state.store.peek("grants", job["grant_id"])["compact_jws"]
    assert grant_token not in resp.text                  # never export tokens
    assert "nonce" not in json.dumps(payload["bundle"])  # nor confirmation nonces


def test_exported_bundle_detects_an_edited_supporting_record(env):
    client, state = env
    user = _user(client)
    _, mandate, job = _approved_job(client, user)
    client.post(f"/v1/jobs/{job['job_id']}/run", headers=user)
    bundle = client.post(f"/v1/jobs/{job['job_id']}/receipt-export", headers=user).json()["bundle"]

    bundle["attempts"][0]["request"]["amount_minor"] = 1  # rewrite history in the exported file
    result = receipt_export.verify_bundle(
        bundle, receipt_registry=state.receipt_registry, authority_registry=state.authority_registry
    )
    assert result.attempt_failures
    assert result.all_verified is False


def test_another_user_cannot_export_my_receipts(env):
    client, state = env
    user_a, user_b = _user(client), _user(client)
    _, _, job = _approved_job(client, user_a)
    assert client.post(f"/v1/jobs/{job['job_id']}/receipt-export", headers=user_b).status_code == 404


# ---- authority-only seeding ----


def test_seed_is_idempotent_and_cannot_replenish(env):
    client, state = env
    user = _user(client)
    scenario, mandate, job = _approved_job(client, user)
    client.post(f"/v1/jobs/{job['job_id']}/run", headers=user)
    spent_balance = client.get("/v1/demo/scenarios/current", headers=user).json()["tenant_balance_minor"]

    resp = client.post(f"/v1/internal/demo/scenarios/{scenario['scenario_id']}/seed", headers=_svc(state, AUTHORITY_PRINCIPAL))
    assert resp.status_code == 200 and resp.json()["seeded"] is True
    assert client.get("/v1/demo/scenarios/current", headers=user).json()["tenant_balance_minor"] == spent_balance


# ---- grant revocation ----


def test_revoking_a_grant_stops_the_payment_and_consumed_grants_cannot_be_revoked(env):
    client, state = env
    user = _user(client)
    _, mandate, job = _approved_job(client, user)
    assert client.post(f"/v1/grants/{job['grant_id']}/revoke", headers=user).json()["state"] == "REVOKED"
    run = client.post(f"/v1/jobs/{job['job_id']}/run", headers=user)
    assert run.status_code == 200
    assert run.json()["outcome"] != "paid"

    user2 = _user(client)
    _, _, job2 = _approved_job(client, user2)
    client.post(f"/v1/jobs/{job2['job_id']}/run", headers=user2)
    late = client.post(f"/v1/grants/{job2['grant_id']}/revoke", headers=user2)
    assert late.status_code == 409  # a committed payment cannot be revoked away


# ---- rent instructions, and the approve-guards ----


def test_updated_instruction_drives_the_next_approval(env):
    client, state = env
    user = _user(client)
    client.post("/v1/demo/scenarios", headers=user)
    resp = client.post("/v1/rent-instructions", headers=user, json={"amount_minor": 4_250_000})
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2

    mandate = client.post("/v1/mandates", headers=user, json={"document_ids": []}).json()
    assert mandate["amount_minor"] == 4_250_000
    assert client.get("/v1/demo/scenarios/current", headers=user).json()["amount_minor"] == 4_250_000


@pytest.mark.parametrize("bad", [0, -5, 4250000.5, "4250000", True, 100_000_001])
def test_instruction_rejects_ambiguous_amounts(env, bad):
    client, state = env
    user = _user(client)
    client.post("/v1/demo/scenarios", headers=user)
    assert client.post("/v1/rent-instructions", headers=user, json={"amount_minor": bad}).status_code == 422


def test_instruction_cannot_point_at_an_unregistered_destination(env):
    client, state = env
    user = _user(client)
    client.post("/v1/demo/scenarios", headers=user)
    resp = client.post("/v1/rent-instructions", headers=user, json={"amount_minor": 100, "destination_version": 7})
    assert resp.status_code in (404, 422)


def test_approving_an_already_paid_obligation_explains_itself(env):
    client, state = env
    user = _user(client)
    _, mandate, job = _approved_job(client, user)
    client.post(f"/v1/jobs/{job['job_id']}/run", headers=user)

    resp = client.post("/v1/mandates", headers=user, json={"document_ids": []})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "OBLIGATION_PAID"
    assert "fresh scenario" in resp.json()["detail"]["message"]


def test_approving_twice_explains_the_existing_approval(env):
    client, state = env
    user = _user(client)
    _approved_job(client, user)
    resp = client.post("/v1/mandates", headers=user, json={"document_ids": []})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "APPROVAL_EXISTS"


def test_blocked_run_shows_approved_versus_attempted(env):
    """SPEC.md Section 14: 'Show approved-versus-attempted details on
    refusal' — both sides from trusted stored records."""
    from services.runner.model_client import FakeModelClient, ModelTurn

    state = build_local_demo_state()
    client = TestClient(create_app(state))
    user = _user(client)
    scenario, mandate, job = _approved_job(client, user)
    state.model_client_factory = lambda: FakeModelClient(script=[ModelTurn(
        proposal={
            "document_id": "n1", "rent_period": "2026-09", "claimed_amount_minor": mandate["amount_minor"],
            "claimed_currency": "LKR", "claimed_destination_text": "Demo Landlord", "evidence": [],
            "conflicts": [], "next_action": "propose_payment",
        },
        proposed_payment={
            "debit_account_id": mandate["debit_account_id"], "destination_account_id": "acct-attacker-99",
            "destination_version": 1, "amount_minor": mandate["amount_minor"], "currency": "LKR",
            "payment_reference": mandate["payment_reference"],
        },
    )])
    run = client.post(f"/v1/jobs/{job['job_id']}/run", headers=user).json()
    assert run["outcome"] == "blocked"
    assert run["approved"]["destination_account_id"] == mandate["destination_account_id"]
    assert run["attempted"]["destination_account_id"] == "acct-attacker-99"  # preserved, never corrected


# ---- documents: server-assigned ids, owner-scoped selection ----


def test_document_ids_are_server_assigned_and_never_collide_across_users(env):
    """Regression: ids used to be client-chosen in one global namespace, so
    user B's upload of 'n1' failed (and revealed) because user A had one."""
    client, state = env
    a, b = _user(client), _user(client)
    for headers in (a, b):
        client.post("/v1/demo/scenarios", headers=headers)
    doc_a = client.post("/v1/documents", headers=a, json={"pages": ["x"]})
    doc_b = client.post("/v1/documents", headers=b, json={"pages": ["y"]})
    assert doc_a.status_code == doc_b.status_code == 200
    assert doc_a.json()["document_id"] != doc_b.json()["document_id"]

    chosen = client.post("/v1/documents", headers=a, json={"document_id": "n1", "pages": ["z"]})
    assert chosen.status_code == 422  # a client-chosen id is an unknown field, refused


def test_an_approval_cannot_reference_another_users_document(env):
    client, state = env
    a, b = _user(client), _user(client)
    for headers in (a, b):
        client.post("/v1/demo/scenarios", headers=headers)
    a_doc = client.post("/v1/documents", headers=a, json={"pages": ["A's lease"]}).json()["document_id"]
    resp = client.post("/v1/mandates", headers=b, json={"document_ids": [a_doc]})
    assert resp.status_code == 404


def test_an_approval_is_limited_to_the_document_budget(env):
    client, state = env
    user = _user(client)
    client.post("/v1/demo/scenarios", headers=user)
    ids = [client.post("/v1/documents", headers=user, json={"pages": [str(i)]}).json()["document_id"] for i in range(4)]
    assert client.post("/v1/mandates", headers=user, json={"document_ids": ids}).status_code == 422
