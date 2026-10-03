"""Wiring for one deployed instance of the API (SPEC.md Section 12).

A real deployment would split this across separate Cloud Run services per
SPEC.md Section 4 (authority / gateway / runner / receipts each with their
own service account); this single-process ``AppState`` exists for the local
demo and its tests, where every "service" is just a plain Python call into
the same already-tested modules. Swapping any one field here (the signer,
the identity verifier, the model client) for its real Cloud adapter is the
entire migration path to a real deployment — nothing above this layer
needs to change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.shared.crypto import Signer, VerifierRegistry
from services.shared.identity import IdentityVerifier, LocalIdentityIssuer, ServicePrincipalVerifier
from services.shared.local_adapters import LocalSigner, LocalVerifierRegistry, generate_rsa_keypair
from services.shared.store import Store

RUNNER_PRINCIPAL = "runner-service-demo"
SCHEDULER_PRINCIPAL = "scheduler-invoker"
AUTHORITY_PRINCIPAL = "authority-service"


@dataclass
class AppState:
    store: Store
    authority_signer: Signer
    authority_registry: VerifierRegistry
    receipt_signer: Signer
    receipt_registry: VerifierRegistry
    identity_verifier: IdentityVerifier
    service_verifier: ServicePrincipalVerifier
    runner_principal: str = RUNNER_PRINCIPAL
    # Demo-only: mints local test identity tokens so a browser without a
    # real Firebase project can still obtain a session. NEVER present this
    # capability in a real deployment — issuing your own bearer tokens
    # server-side for arbitrary callers defeats authentication entirely.
    # A real "Try demo" button calls Firebase Auth's anonymous sign-in
    # client-side and sends *that* verified ID token here instead.
    local_identity_issuer: LocalIdentityIssuer | None = field(default=None)
    model_client_factory: "object | None" = field(default=None)
    # Optional decision-model pre-screen (Laya / Jev). One-directional: it can
    # only add a review flag — see services/runner/screening.py. None = off.
    screener: "object | None" = field(default=None)


def build_screener(kind: str | None):
    """``PAYGUARD_SCREENER`` = ``none`` | ``laya-hf`` | ``laya-local``."""
    from services.runner.decision_client import LayaHuggingFaceSpaceClient, LayaLocalClient

    kind = (kind or "none").strip().lower()
    if kind in ("", "none", "off"):
        return None
    if kind == "laya-hf":
        return LayaHuggingFaceSpaceClient()
    if kind == "laya-local":
        return LayaLocalClient()
    raise ValueError(f"unknown PAYGUARD_SCREENER {kind!r} (expected none, laya-hf or laya-local)")


def build_local_demo_state(*, model_client_factory=None, session_secret: bytes | None = None) -> AppState:
    """Everything wired to the local, non-cloud test/dev adapters — this is
    what backs the local demo server and its test suite. Never call this
    from a real deployment entrypoint."""
    from services.shared.identity import LocalIdentityVerifier, LocalServicePrincipalVerifier

    identity_verifier = (
        LocalIdentityVerifier(session_secret) if session_secret is not None else LocalIdentityVerifier.generate()
    )
    authority_private, authority_public = generate_rsa_keypair()
    receipt_private, receipt_public = generate_rsa_keypair()
    return AppState(
        store=Store(),
        authority_signer=LocalSigner("authority-key-1", authority_private),
        authority_registry=LocalVerifierRegistry({"authority-key-1": authority_public}),
        receipt_signer=LocalSigner("receipt-key-1", receipt_private),
        receipt_registry=LocalVerifierRegistry({"receipt-key-1": receipt_public}),
        identity_verifier=identity_verifier,
        service_verifier=LocalServicePrincipalVerifier.generate(
            [RUNNER_PRINCIPAL, SCHEDULER_PRINCIPAL, AUTHORITY_PRINCIPAL]
        ),
        local_identity_issuer=LocalIdentityIssuer(identity_verifier),
        model_client_factory=model_client_factory,
    )
