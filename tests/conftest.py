from __future__ import annotations

import time

import pytest

from services.authority import destinations, grants, mandates
from services.authority.mandates import DraftInput
from services.shared.local_adapters import LocalSigner, LocalVerifierRegistry, generate_rsa_keypair
from services.shared.store import Store

OWNER_UID = "user-01"
RUNNER_PRINCIPAL = "runner-service-01"
OBLIGATION_ID = "rent-2026-09-01"
DEBIT_ACCOUNT = "acct-tenant-01"
DESTINATION_ID = "acct-landlord-01"
DEMO_LANDLORD_ACCOUNT = "demo-landlord-account-01"
AMOUNT_MINOR = 5_000_000
CURRENCY = "LKR"
PAYMENT_REFERENCE = "RENT-2026-09-01"
POLICY_VERSION = "rent-v1"


@pytest.fixture
def store() -> Store:
    return Store()


@pytest.fixture
def authority_signer():
    private_key, public_key = generate_rsa_keypair()
    return LocalSigner("authority-key-1", private_key), public_key


@pytest.fixture
def verifier_registry(authority_signer):
    _, public_key = authority_signer
    return LocalVerifierRegistry({"authority-key-1": public_key})


@pytest.fixture
def seeded_store(store: Store):
    """Seed the tenant debit account and the landlord's demo ledger account."""
    store.run_transaction(lambda t: t.set("accounts", DEBIT_ACCOUNT, {
        "account_id": DEBIT_ACCOUNT, "owner_uid": OWNER_UID, "currency": CURRENCY, "balance_minor": 50_000_000,
    }))
    store.run_transaction(lambda t: t.set("accounts", DEMO_LANDLORD_ACCOUNT, {
        "account_id": DEMO_LANDLORD_ACCOUNT, "owner_uid": "landlord-01", "currency": CURRENCY, "balance_minor": 0,
    }))
    destinations.create_destination_version(
        store,
        destination_account_id=DESTINATION_ID,
        version=1,
        owner_uid=OWNER_UID,
        demo_account_id=DEMO_LANDLORD_ACCOUNT,
        label="Demo Landlord",
    )
    return store


def make_draft(mandate_id: str, *, supersedes: str | None = None, destination_version: int = 1) -> DraftInput:
    return DraftInput(
        mandate_id=mandate_id,
        owner_uid=OWNER_UID,
        instruction_id="instruction-01",
        instruction_version=1,
        obligation_id=OBLIGATION_ID,
        debit_account_id=DEBIT_ACCOUNT,
        payee_id="payee-01",
        destination_account_id=DESTINATION_ID,
        destination_version=destination_version,
        amount_mode="exact",
        amount_minor=AMOUNT_MINOR,
        currency=CURRENCY,
        payment_reference=PAYMENT_REFERENCE,
        grant_expiry_seconds=300,
        selected_document_ids=(),
        policy_version=POLICY_VERSION,
        supersedes_mandate_id=supersedes,
    )


def approve_and_sign_mandate(store: Store, signer, mandate_id: str, *, supersedes: str | None = None,
                              destination_version: int = 1) -> dict:
    draft = mandates.create_draft(store, make_draft(mandate_id, supersedes=supersedes,
                                                      destination_version=destination_version))
    mandates.confirm_mandate(store, mandate_id=mandate_id, owner_uid=OWNER_UID, version=draft["version"],
                              nonce=draft["nonce"])
    return mandates.sign_mandate(store, mandate_id, signer)


def reserve_and_activate_grant(store: Store, signer, *, job_id: str, grant_id: str, mandate_id: str) -> dict:
    grants.reserve_job_and_grant(
        store, job_id=job_id, grant_id=grant_id, mandate_id=mandate_id, owner_uid=OWNER_UID,
        runner_principal=RUNNER_PRINCIPAL,
    )
    return grants.sign_and_activate_grant(store, grant_id, signer)


@pytest.fixture
def happy_path(seeded_store, authority_signer):
    """A fully approved, signed, activated grant ready for one execution."""
    signer, _ = authority_signer
    mandate_id = "mandate-01"
    approve_and_sign_mandate(seeded_store, signer, mandate_id)
    grant = reserve_and_activate_grant(seeded_store, signer, job_id="job-01", grant_id="grant-01",
                                        mandate_id=mandate_id)
    return {"store": seeded_store, "mandate_id": mandate_id, "grant": grant}
