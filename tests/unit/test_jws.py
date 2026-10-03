"""Compact JWS profile: golden round-trip plus type-confusion/tamper tests."""

from __future__ import annotations

import base64
import json

import pytest

from services.shared.jws import JwsError, encode_compact, verify_compact
from services.shared.local_adapters import LocalSigner, LocalVerifierRegistry, generate_rsa_keypair


@pytest.fixture
def keypair_and_registry():
    private_key, public_key = generate_rsa_keypair()
    signer = LocalSigner("key-1", private_key)
    registry = LocalVerifierRegistry({"key-1": public_key})
    return signer, registry


def test_round_trip(keypair_and_registry):
    signer, registry = keypair_and_registry
    token = encode_compact(typ="payguard-grant+jwt", kid="key-1", payload_bytes=b'{"a":1}', signer=signer)
    verified = verify_compact(token, expected_typ="payguard-grant+jwt", registry=registry)
    assert verified.payload_bytes == b'{"a":1}'
    assert verified.header["alg"] == "PS256"


def test_never_accept_a_receipt_as_a_grant(keypair_and_registry):
    signer, registry = keypair_and_registry
    token = encode_compact(typ="payguard-receipt+jwt", kid="key-1", payload_bytes=b'{"a":1}', signer=signer)
    with pytest.raises(JwsError):
        verify_compact(token, expected_typ="payguard-grant+jwt", registry=registry)


def test_never_accept_a_mandate_where_a_grant_is_required(keypair_and_registry):
    signer, registry = keypair_and_registry
    token = encode_compact(typ="payguard-mandate+jwt", kid="key-1", payload_bytes=b'{"a":1}', signer=signer)
    with pytest.raises(JwsError):
        verify_compact(token, expected_typ="payguard-grant+jwt", registry=registry)


def test_unknown_kid_rejected(keypair_and_registry):
    signer, registry = keypair_and_registry
    token = encode_compact(typ="payguard-grant+jwt", kid="key-1", payload_bytes=b'{"a":1}', signer=signer)
    empty_registry = LocalVerifierRegistry({})
    with pytest.raises(JwsError):
        verify_compact(token, expected_typ="payguard-grant+jwt", registry=empty_registry)


def test_wrong_key_signature_rejected(keypair_and_registry):
    signer, registry = keypair_and_registry
    # Sign with a *different* key than the one advertised in the header kid,
    # simulating a forged token that claims an allowlisted kid it didn't use.
    other_private, _ = generate_rsa_keypair()
    forged_signer = LocalSigner("key-1", other_private)  # same kid, wrong key
    token = encode_compact(typ="payguard-grant+jwt", kid="key-1", payload_bytes=b'{"a":1}', signer=forged_signer)
    with pytest.raises(JwsError):
        verify_compact(token, expected_typ="payguard-grant+jwt", registry=registry)


def test_alg_none_rejected(keypair_and_registry):
    _, registry = keypair_and_registry

    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = _b64url(json.dumps({"alg": "none", "typ": "payguard-grant+jwt", "kid": "key-1"}).encode())
    payload = _b64url(b'{"a":1}')
    token = f"{header}.{payload}."
    with pytest.raises(JwsError):
        verify_compact(token, expected_typ="payguard-grant+jwt", registry=registry)


def test_tampered_payload_rejected(keypair_and_registry):
    signer, registry = keypair_and_registry
    token = encode_compact(typ="payguard-grant+jwt", kid="key-1", payload_bytes=b'{"amount_minor":1}', signer=signer)
    header_b64, _, sig_b64 = token.split(".")
    tampered_payload = base64.urlsafe_b64encode(b'{"amount_minor":999999999}').rstrip(b"=").decode("ascii")
    tampered_token = f"{header_b64}.{tampered_payload}.{sig_b64}"
    with pytest.raises(JwsError):
        verify_compact(tampered_token, expected_typ="payguard-grant+jwt", registry=registry)


def test_malformed_token_shapes_rejected(keypair_and_registry):
    _, registry = keypair_and_registry
    for bad in ["not-a-jws", "a.b", "a.b.c.d", "", "..", "a..c"]:
        with pytest.raises(JwsError):
            verify_compact(bad, expected_typ="payguard-grant+jwt", registry=registry)
