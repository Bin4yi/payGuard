"""SPEC.md Section 9: exported evidence bundles. A bundle is self-contained
but not self-certifying — the trust roots always come from the verifier."""

from __future__ import annotations

import copy

import pytest

from services.gateway.enforcement import execute_payment
from services.receipts import export, signing
from services.shared.local_adapters import LocalSigner, LocalVerifierRegistry, generate_rsa_keypair
from services.shared.schemas import PaymentRequest
from tests.conftest import (
    AMOUNT_MINOR, CURRENCY, DEBIT_ACCOUNT, DESTINATION_ID, OBLIGATION_ID, OWNER_UID, PAYMENT_REFERENCE,
    RUNNER_PRINCIPAL,
)


def _request(**overrides) -> PaymentRequest:
    base = dict(
        job_id="job-01", mandate_id="mandate-01", obligation_id=OBLIGATION_ID, debit_account_id=DEBIT_ACCOUNT,
        destination_account_id=DESTINATION_ID, destination_version=1, amount_minor=AMOUNT_MINOR,
        currency=CURRENCY, payment_reference=PAYMENT_REFERENCE,
    )
    base.update(overrides)
    return PaymentRequest(**base)


@pytest.fixture
def receipt_keys():
    private_key, public_key = generate_rsa_keypair()
    return LocalSigner("receipt-key-1", private_key), LocalVerifierRegistry({"receipt-key-1": public_key})


@pytest.fixture
def three_event_bundle(happy_path, verifier_registry, receipt_keys):
    """Two blocked attempts then a committed payment, all signed."""
    store, grant = happy_path["store"], happy_path["grant"]
    signer, _ = receipt_keys
    for i, amount in enumerate([AMOUNT_MINOR + 1, AMOUNT_MINOR + 2]):
        execute_payment(store, request=_request(amount_minor=amount), grant_token=grant["compact_jws"],
                        idempotency_key=f"blocked-{i}", authenticated_principal=RUNNER_PRINCIPAL,
                        verifier_registry=verifier_registry)
    execute_payment(store, request=_request(), grant_token=grant["compact_jws"], idempotency_key="ok",
                    authenticated_principal=RUNNER_PRINCIPAL, verifier_registry=verifier_registry)
    signing.drain_workflow(store, "job-01", signer)
    return store, export.build_bundle(store, "job-01", owner_uid=OWNER_UID)


def test_a_complete_bundle_verifies(three_event_bundle, receipt_keys, verifier_registry):
    _, bundle = three_event_bundle
    _, receipt_registry = receipt_keys
    result = export.verify_bundle(bundle, receipt_registry=receipt_registry, authority_registry=verifier_registry)
    assert result.all_verified
    assert result.completeness_unknown is False
    assert [r.status for r in result.receipts] == ["VERIFIED"] * 3
    assert len(bundle["attempts"]) == 3  # blocked attempts are evidence too, with their actual tuples


def test_removing_a_middle_receipt_is_a_gap(three_event_bundle, receipt_keys, verifier_registry):
    _, bundle = three_event_bundle
    _, receipt_registry = receipt_keys
    bundle["receipts"] = [r for r in bundle["receipts"] if r["sequence"] != 2]
    result = export.verify_bundle(bundle, receipt_registry=receipt_registry, authority_registry=verifier_registry)
    assert result.gaps == [2]
    assert result.completeness_unknown is True
    assert {r.sequence: r.status for r in result.receipts}[3] == "CHAIN_BROKEN"


def test_a_truncated_suffix_is_completeness_unknown_not_a_gap(three_event_bundle, receipt_keys, verifier_registry):
    """Every remaining signature is still valid — the honest answer is
    'we can't claim nothing was withheld', not 'tampered'."""
    _, bundle = three_event_bundle
    _, receipt_registry = receipt_keys
    bundle["receipts"] = [r for r in bundle["receipts"] if r["sequence"] != 3]
    result = export.verify_bundle(bundle, receipt_registry=receipt_registry, authority_registry=verifier_registry)
    assert result.gaps == []
    assert all(r.status == "VERIFIED" for r in result.receipts)
    assert result.completeness_unknown is True


def test_trust_comes_from_the_verifier_not_the_bundle(three_event_bundle, verifier_registry):
    """Verified against keys the verifier did not configure, nothing passes —
    there is no way for a bundle to vouch for itself."""
    _, bundle = three_event_bundle
    _, attacker_public = generate_rsa_keypair()
    attacker_registry = LocalVerifierRegistry({"receipt-key-1": attacker_public})
    result = export.verify_bundle(bundle, receipt_registry=attacker_registry, authority_registry=verifier_registry)
    assert all(r.status == "SIGNATURE_INVALID" for r in result.receipts)
    assert result.all_verified is False


def test_swapping_in_a_different_mandate_is_caught(three_event_bundle, receipt_keys, verifier_registry, authority_signer):
    """Replace the exported mandate with a validly signed *different* one:
    the receipts' signed mandate_digest no longer matches."""
    from services.authority import mandates as mandates_mod
    from tests.conftest import make_draft

    store, bundle = three_event_bundle
    _, receipt_registry = receipt_keys
    signer, _ = authority_signer

    other_obligation_draft = make_draft("mandate-other")
    other_obligation_draft.obligation_id = "rent-other"
    draft = mandates_mod.create_draft(store, other_obligation_draft)
    mandates_mod.confirm_mandate(store, mandate_id="mandate-other", owner_uid=OWNER_UID,
                                 version=draft["version"], nonce=draft["nonce"])
    other = mandates_mod.sign_mandate(store, "mandate-other", signer)

    forged = copy.deepcopy(bundle)
    forged["mandate"].update(compact_jws=other["compact_jws"], digest=other["digest"])
    result = export.verify_bundle(forged, receipt_registry=receipt_registry, authority_registry=verifier_registry)
    assert result.mandate_status == "VERIFIED"  # it IS a genuine mandate...
    assert any(r.status == "DIGEST_MISMATCH" for r in result.receipts)  # ...just not the one these receipts approved
    assert result.all_verified is False


def test_a_credential_shaped_field_fails_the_bundle(three_event_bundle, receipt_keys, verifier_registry):
    _, bundle = three_event_bundle
    _, receipt_registry = receipt_keys
    bundle["attempts"][0]["token"] = "leaked"
    result = export.verify_bundle(bundle, receipt_registry=receipt_registry, authority_registry=verifier_registry)
    assert result.contains_no_credentials is False
    assert result.all_verified is False


def test_build_bundle_never_includes_the_grant_token(three_event_bundle, happy_path):
    import json

    _, bundle = three_event_bundle
    assert happy_path["grant"]["compact_jws"] not in json.dumps(bundle)


def test_build_bundle_refuses_another_owner(three_event_bundle):
    store, _ = three_event_bundle
    with pytest.raises(export.ExportError):
        export.build_bundle(store, "job-01", owner_uid="someone-else")
