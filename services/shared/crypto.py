"""Signing/verification interfaces (SPEC.md Section 5/6/9).

Production signing goes through Cloud KMS (RSA-PSS-SHA256 / PS256). This
module defines the abstract Signer/Verifier boundary so Phase 1 can exercise
every transaction and binding rule against a local, non-KMS adapter without
depending on Google Cloud. The local adapter lives in ``local_adapters.py``
and is explicitly test/dev-only — see the warning there. Nothing in this
module or in ``jws.py`` decides *which* adapter is wired up; that choice is
made once, at service start-up, by environment-specific config, so it can
never be selected by request data.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Signer(Protocol):
    """Something that can produce a PS256 signature under one key id."""

    @property
    def kid(self) -> str: ...

    def sign(self, message: bytes) -> bytes:
        """Return the raw RSA-PSS-SHA256 signature over ``message``."""
        ...


@runtime_checkable
class VerifierRegistry(Protocol):
    """Independently configured trusted public keys, looked up by kid.

    A verifier registry is constructed from trusted app/service configuration
    (SPEC.md Section 9: "Install allowlisted public keys through trusted app
    configuration, independently of any export"). It must never be built from
    data found inside the token or bundle being verified.
    """

    def public_key_for(self, kid: str):
        """Return the public key object for an allowlisted kid, or None."""
        ...

    def allows_kid(self, kid: str) -> bool: ...


class UnknownKeyError(ValueError):
    """Raised when a kid is not present in the trusted verifier registry."""
