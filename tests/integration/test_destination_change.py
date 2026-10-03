"""SPEC.md Section 5 'Destination changes' — the required MVP recovery flow:

blocked -> review changed details -> explicit confirmation -> old authority
invalidated -> replacement signed -> new job/grant for the same unpaid
obligation.
"""

from __future__ import annotations

from services.authority import destinations, grants, mandates
from services.gateway.enforcement import execute_payment
from services.shared.schemas import PaymentRequest
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
    approve_and_sign_mandate,
    reserve_and_activate_grant,
)

NEW_LANDLORD_ACCOUNT = "demo-new-landlord-account-01"


def _request(**overrides) -> PaymentRequest:
    base = dict(
        job_id="job-01",
        mandate_id="mandate-01",
        obligation_id=OBLIGATION_ID,
        debit_account_id=DEBIT_ACCOUNT,
        destination_account_id=DESTINATION_ID,
        destination_version=1,
        amount_minor=AMOUNT_MINOR,
        currency=CURRENCY,
        payment_reference=PAYMENT_REFERENCE,
    )
    base.update(overrides)
    return PaymentRequest(**base)


def test_reviewed_account_change_supersedes_old_grant_and_pays_new_destination(
    seeded_store, authority_signer, verifier_registry
):
    store = seeded_store
    signer, _ = authority_signer

    approve_and_sign_mandate(store, signer, "mandate-01")
    grant1 = reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")
    assert store.peek("grants", "grant-01")["state"] == "ACTIVE"

    # A poisoned/legitimate notice claims a new landlord account. v1 always
    # routes this through explicit user review — never auto-accepted.
    store.run_transaction(lambda t: t.set("accounts", NEW_LANDLORD_ACCOUNT, {
        "account_id": NEW_LANDLORD_ACCOUNT, "owner_uid": "landlord-02", "currency": CURRENCY, "balance_minor": 0,
    }))
    destinations.create_destination_version(
        store, destination_account_id=DESTINATION_ID, version=2, owner_uid=OWNER_UID,
        demo_account_id=NEW_LANDLORD_ACCOUNT, label="New Landlord (reviewed)",
    )

    draft = mandates.create_draft(
        store,
        mandates.DraftInput(
            mandate_id="mandate-01-repl", owner_uid=OWNER_UID, instruction_id="instruction-01", instruction_version=1,
            obligation_id=OBLIGATION_ID, debit_account_id=DEBIT_ACCOUNT, payee_id="payee-01",
            destination_account_id=DESTINATION_ID, destination_version=2, amount_mode="exact",
            amount_minor=AMOUNT_MINOR, currency=CURRENCY, payment_reference=PAYMENT_REFERENCE,
            grant_expiry_seconds=300, selected_document_ids=(), policy_version="rent-v1",
            supersedes_mandate_id="mandate-01",
        ),
    )

    # Explicit user confirmation of the replacement.
    mandates.confirm_mandate(
        store, mandate_id="mandate-01-repl", owner_uid=OWNER_UID, version=draft["version"], nonce=draft["nonce"]
    )
    mandates.sign_mandate(store, "mandate-01-repl", signer)

    old_mandate = store.peek("mandates", "mandate-01")
    assert old_mandate["state"] == "SUPERSEDED"
    old_grant = store.peek("grants", "grant-01")
    assert old_grant["state"] == "REVOKED"

    obligation = store.peek("obligations", OBLIGATION_ID)
    assert obligation["current_mandate_id"] == "mandate-01-repl"
    assert obligation["current_grant_id"] is None  # bounded to at most one pending/active grant

    # The old grant cannot execute anymore.
    old_grant_result = execute_payment(
        store, request=_request(), grant_token=grant1["compact_jws"], idempotency_key="idem-old-1",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert old_grant_result.status == "BLOCKED"
    assert "GRANT_REVOKED" in old_grant_result.reason_codes

    # A fresh job/grant against the replacement mandate pays the *new* destination.
    grant2 = reserve_and_activate_grant(
        store, signer, job_id="job-02", grant_id="grant-02", mandate_id="mandate-01-repl"
    )
    result = execute_payment(
        store,
        request=_request(job_id="job-02", mandate_id="mandate-01-repl", destination_version=2),
        grant_token=grant2["compact_jws"],
        idempotency_key="idem-new-1",
        authenticated_principal=RUNNER_PRINCIPAL,
        verifier_registry=verifier_registry,
    )
    assert result.status == "COMMITTED"

    old_landlord = store.peek("accounts", DEMO_LANDLORD_ACCOUNT)
    new_landlord = store.peek("accounts", NEW_LANDLORD_ACCOUNT)
    assert old_landlord["balance_minor"] == 0
    assert new_landlord["balance_minor"] == AMOUNT_MINOR


def test_replacement_confirmation_fails_once_obligation_already_paid(
    seeded_store, authority_signer, verifier_registry
):
    """If payment commits first, replacement confirmation fails with
    OBLIGATION_PAID (SPEC.md Section 5)."""
    store = seeded_store
    signer, _ = authority_signer

    approve_and_sign_mandate(store, signer, "mandate-01")
    grant1 = reserve_and_activate_grant(store, signer, job_id="job-01", grant_id="grant-01", mandate_id="mandate-01")

    paid = execute_payment(
        store, request=_request(), grant_token=grant1["compact_jws"], idempotency_key="idem-paid-1",
        authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry,
    )
    assert paid.status == "COMMITTED"

    destinations.create_destination_version(
        store, destination_account_id=DESTINATION_ID, version=2, owner_uid=OWNER_UID,
        demo_account_id=NEW_LANDLORD_ACCOUNT, label="New Landlord (too late)",
    )
    draft = mandates.create_draft(
        store,
        mandates.DraftInput(
            mandate_id="mandate-01-late", owner_uid=OWNER_UID, instruction_id="instruction-01", instruction_version=1,
            obligation_id=OBLIGATION_ID, debit_account_id=DEBIT_ACCOUNT, payee_id="payee-01",
            destination_account_id=DESTINATION_ID, destination_version=2, amount_mode="exact",
            amount_minor=AMOUNT_MINOR, currency=CURRENCY, payment_reference=PAYMENT_REFERENCE,
            grant_expiry_seconds=300, selected_document_ids=(), policy_version="rent-v1",
            supersedes_mandate_id="mandate-01",
        ),
    )
    try:
        mandates.confirm_mandate(
            store, mandate_id="mandate-01-late", owner_uid=OWNER_UID, version=draft["version"], nonce=draft["nonce"]
        )
        raised = False
    except mandates.MandateError as exc:
        raised = True
        assert "OBLIGATION_PAID" in str(exc)
    assert raised
