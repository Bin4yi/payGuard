"""Demo session tokens are HMAC-signed: knowing (or guessing) someone's uid
must not be enough to act as them."""

from __future__ import annotations

import pytest

from services.shared.identity import IdentityError, LocalIdentityIssuer, LocalIdentityVerifier


def _pair() -> tuple[LocalIdentityVerifier, LocalIdentityIssuer]:
    verifier = LocalIdentityVerifier.generate()
    return verifier, LocalIdentityIssuer(verifier)


def test_issued_token_verifies():
    verifier, issuer = _pair()
    uid, token = issuer.issue_anonymous()
    identity = verifier.verify(token)
    assert identity.uid == uid and identity.is_anonymous


def test_hand_built_token_for_known_uid_is_rejected():
    verifier, issuer = _pair()
    uid, _ = issuer.issue_anonymous()
    with pytest.raises(IdentityError):
        verifier.verify(f"local:{uid}:anonymous:0")
    with pytest.raises(IdentityError):
        verifier.verify(f"local:{uid}:anonymous:0:{'0' * 64}")


def test_tampered_uid_is_rejected():
    verifier, issuer = _pair()
    _, token = issuer.issue_anonymous()
    _, victim_token = issuer.issue_anonymous()
    victim_uid = verifier.verify(victim_token).uid
    parts = token.split(":")
    parts[1] = victim_uid
    with pytest.raises(IdentityError):
        verifier.verify(":".join(parts))


def test_token_from_another_secret_is_rejected():
    _, issuer_a = _pair()
    verifier_b, _ = _pair()
    _, token = issuer_a.issue_anonymous()
    with pytest.raises(IdentityError):
        verifier_b.verify(token)


def test_short_secret_refused():
    with pytest.raises(ValueError):
        LocalIdentityVerifier(b"too-short")
