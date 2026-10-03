"""Local, non-KMS signing adapters for tests and Phase 1 development ONLY.

These exist so the deterministic gateway/authority transaction logic can be
tested without a live Cloud KMS dependency (SPEC.md Section 7: "Never call
... KMS ... inside a transaction callback" — signing always happens outside
transactions, through this same Signer interface, whether the concrete
implementation is this local adapter or the real Cloud KMS client).

THIS MODULE MUST NEVER BE WIRED UP IN A DEPLOYED SERVICE. Production config
selects the Cloud KMS-backed signer exclusively; there is no code path here
that reads request data to decide which signer to use. Phase 1 tests using
this adapter are not evidence of production cloud authentication (see
CLAUDE_CODE_PROMPT.md Phase 1, item 3).
"""

from __future__ import annotations

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_PSS_PADDING = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size)


def generate_rsa_keypair() -> tuple[rsa.RSAPrivateKey, rsa.RSAPublicKey]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


class LocalSigner:
    """In-process PS256 signer over a generated RSA keypair. Test/dev only."""

    def __init__(self, kid: str, private_key: rsa.RSAPrivateKey) -> None:
        self._kid = kid
        self._private_key = private_key

    @property
    def kid(self) -> str:
        return self._kid

    def sign(self, message: bytes) -> bytes:
        return self._private_key.sign(message, _PSS_PADDING, hashes.SHA256())


class LocalVerifierRegistry:
    """Fixed, independently-constructed allowlist of trusted public keys.

    Construct once from known-good keys (e.g. loaded at service start-up);
    never mutate it from data found in a token or export bundle under test.
    """

    def __init__(self, keys: dict[str, rsa.RSAPublicKey]) -> None:
        self._keys = dict(keys)

    def public_key_for(self, kid: str):
        return self._keys.get(kid)

    def allows_kid(self, kid: str) -> bool:
        return kid in self._keys

    def verify(self, kid: str, message: bytes, signature: bytes) -> bool:
        key = self._keys.get(kid)
        if key is None:
            return False
        try:
            key.verify(signature, message, _PSS_PADDING, hashes.SHA256())
            return True
        except Exception:
            return False


class FlakySigner:
    """Test double: raises for the first ``fail_times`` calls to ``sign()``,
    then delegates to ``inner``. Models a transient Cloud KMS outage so
    authority recovery logic (SPEC.md Section 5: "keep signing PENDING,
    retry the same frozen payload with bounded backoff") can be exercised
    without a real KMS dependency. Test/dev only, same restriction as the
    rest of this module.
    """

    def __init__(self, inner: LocalSigner, fail_times: int) -> None:
        self._inner = inner
        self._remaining_failures = fail_times

    @property
    def kid(self) -> str:
        return self._inner.kid

    def sign(self, message: bytes) -> bytes:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise RuntimeError("simulated KMS outage")
        return self._inner.sign(message)


def public_key_pem(public_key: rsa.RSAPublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
