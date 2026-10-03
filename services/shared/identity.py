"""The Firebase Auth boundary, abstracted the same way Cloud KMS and Gemini
are: a narrow interface, a local test/dev adapter, and a real adapter stub
that is not exercised in this environment (no Firebase project/credentials
here).

SPEC.md Section 14: "Try demo" creates a Firebase anonymous-auth session and
sends its verified ID token to the authority; the server seeds owned
synthetic state. Backend authentication and ownership checks remain
mandatory regardless of which identity provider issued the token — nothing
downstream should ever special-case "anonymous" as less-verified than any
other session once a token has passed ``verify``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class IdentityError(Exception):
    """Raised when a bearer token fails verification."""


@dataclass(frozen=True)
class VerifiedIdentity:
    uid: str
    is_anonymous: bool
    issued_at: int


@runtime_checkable
class IdentityVerifier(Protocol):
    def verify(self, token: str) -> VerifiedIdentity:
        """Verify a bearer token and return the caller's identity.

        Must raise ``IdentityError`` for anything invalid, expired or
        unparseable — callers never get a partially-trusted identity back.
        """
        ...


class LocalIdentityVerifier:
    """Dev/demo identity verifier standing in for Firebase Auth. Tokens are
    ``"local:<uid>:<anonymous|persistent>:<issued_at>:<hmac>"`` strings
    minted by ``LocalIdentityIssuer`` below, HMAC-SHA256-signed with a
    server-held secret so a caller cannot mint a session for someone else's
    uid. There is no expiry and no revocation, so this is acceptable for a
    single-instance synthetic demo only — a real deployment verifies Firebase
    ID tokens (``FirebaseIdentityVerifier``) instead.
    """

    PREFIX = "local:"

    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("session secret must be at least 32 bytes")
        self._secret = secret

    @classmethod
    def generate(cls) -> "LocalIdentityVerifier":
        return cls(secrets.token_bytes(32))

    def _sign(self, body: str) -> str:
        return hmac.new(self._secret, body.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, token: str) -> VerifiedIdentity:
        if not token.startswith(self.PREFIX):
            raise IdentityError("not a recognized local session token")
        body, sep, signature = token.rpartition(":")
        if not sep or not hmac.compare_digest(signature.encode("utf-8"), self._sign(body).encode("utf-8")):
            raise IdentityError("invalid local session token")
        parts = body[len(self.PREFIX):].split(":")
        if len(parts) != 3:
            raise IdentityError("malformed local session token")
        uid, kind, issued_at_str = parts
        if kind not in ("anonymous", "persistent"):
            raise IdentityError("malformed local session token")
        try:
            issued_at = int(issued_at_str)
        except ValueError as exc:
            raise IdentityError("malformed local session token") from exc
        if not uid:
            raise IdentityError("empty uid")
        return VerifiedIdentity(uid=uid, is_anonymous=(kind == "anonymous"), issued_at=issued_at)


class LocalIdentityIssuer:
    """Dev/demo counterpart to ``LocalIdentityVerifier`` — mints signed
    tokens the way Firebase Auth issues ID tokens. Shares the verifier's
    secret; never used when a real identity provider is configured.
    """

    def __init__(self, verifier: LocalIdentityVerifier) -> None:
        self._verifier = verifier

    def _mint(self, uid: str, kind: str, now_fn) -> str:
        body = f"{LocalIdentityVerifier.PREFIX}{uid}:{kind}:{int(now_fn())}"
        return f"{body}:{self._verifier._sign(body)}"

    def issue_anonymous(self, *, now_fn=time.time) -> tuple[str, str]:
        uid = f"anon-{secrets.token_urlsafe(12)}"
        return uid, self._mint(uid, "anonymous", now_fn)

    def issue_persistent(self, uid: str, *, now_fn=time.time) -> str:
        if ":" in uid:
            raise ValueError("uid must not contain ':'")
        return self._mint(uid, "persistent", now_fn)


@runtime_checkable
class ServicePrincipalVerifier(Protocol):
    def verify(self, token: str) -> str:
        """Verify a *service-to-service* credential and return the calling
        service's principal name. Raises ``IdentityError`` otherwise.

        Distinct from ``IdentityVerifier`` on purpose: a user's session token
        must never be accepted where a service principal is required (and
        vice versa) — the same "type confusion" rule SPEC.md applies to
        mandates, grants and receipts, applied to callers.
        """
        ...


class LocalServicePrincipalVerifier:
    """Test/dev-only stand-in for Cloud Run service-to-service auth.

    A real deployment verifies a Google-issued OIDC ID token for the intended
    audience and the calling service account (SPEC.md Section 4: "Internal
    calls use Google-issued ID tokens validated for the intended audience and
    allowed principal"). Locally, each principal gets a random opaque token
    at startup; nothing about it is derivable from the principal's name.
    """

    def __init__(self, tokens_by_principal: dict[str, str]) -> None:
        self._tokens_by_principal = dict(tokens_by_principal)

    @classmethod
    def generate(cls, principals: list[str]) -> "LocalServicePrincipalVerifier":
        return cls({p: secrets.token_urlsafe(32) for p in principals})

    def token_for(self, principal: str) -> str:
        """Local wiring only (tests, the in-process demo) — the HTTP API never
        returns these."""
        return self._tokens_by_principal[principal]

    def verify(self, token: str) -> str:
        for principal, expected in self._tokens_by_principal.items():
            if hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
                return principal
        raise IdentityError("unknown service credential")


class FirebaseIdentityVerifier:
    """Production adapter over the Firebase Admin SDK. Not exercised by any
    test in this repository — there is no Firebase project or credentials
    configured in this environment. Kept intentionally thin: verify the ID
    token, map its claims to a ``VerifiedIdentity``, and let the API layer's
    ownership checks (unchanged regardless of adapter) do the rest.
    """

    def __init__(self, *, project_id: str):
        self._project_id = project_id
        self._app = None

    def _app_or_raise(self):
        if self._app is None:
            try:
                import firebase_admin  # type: ignore[import-not-found]
                from firebase_admin import auth as firebase_auth  # noqa: F401
            except ImportError as exc:
                raise RuntimeError(
                    "firebase-admin is not installed / no Firebase project is configured in this "
                    "environment — FirebaseIdentityVerifier is not exercised by this repository's tests"
                ) from exc
            self._app = firebase_admin.initialize_app()
        return self._app

    def verify(self, token: str) -> VerifiedIdentity:
        raise NotImplementedError(
            "FirebaseIdentityVerifier.verify is a deployment-time integration point, not exercised "
            "in this sandboxed session (no Firebase project/credentials available here). Wire it up "
            "with firebase_admin.auth.verify_id_token when deploying Phase 5."
        )
