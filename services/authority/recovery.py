"""Scheduled authority signature recovery (SPEC.md Section 5, `POST
/internal/authority/drain`).

Signing a mandate or a grant happens outside any store transaction (Cloud
KMS is a network call). If that call — or the compare-and-set persistence
after it — fails or the process crashes before either completes, the
record is left in its ``PENDING``/``PENDING_SIGNATURE`` state, never in a
half-signed one: nothing is written until signing succeeds. A trusted
scheduled worker can safely re-invoke the same signing functions against
any record still pending; `mandates.sign_mandate` and
`grants.sign_and_activate_grant` are themselves idempotent no-ops against a
record that is no longer eligible (already signed, or revoked/superseded
out from under the retry), so calling them again here is always safe.

This "worker" is a plain scan-and-retry loop over the in-memory store's
`list_ids`; a Cloud-deployed version would instead run on Cloud Scheduler
against a Firestore query filtered by signing/grant state.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.authority import grants, mandates
from services.shared.crypto import Signer
from services.shared.states import GrantState, MandateState, SigningState
from services.shared.store import Store

MANDATES = "mandates"
GRANTS = "grants"


@dataclass
class RecoveryReport:
    mandates_signed: list[str] = field(default_factory=list)
    mandates_failed: list[str] = field(default_factory=list)
    grants_activated: list[str] = field(default_factory=list)
    grants_failed: list[str] = field(default_factory=list)


def recover_pending_mandate_signatures(store: Store, signer: Signer) -> RecoveryReport:
    report = RecoveryReport()
    for mandate_id in store.list_ids(MANDATES):
        mandate = store.peek(MANDATES, mandate_id)
        if (
            mandate is None
            or mandate["state"] != MandateState.APPROVED.value
            or mandate["signing_state"] != SigningState.PENDING.value
        ):
            continue
        try:
            result = mandates.sign_mandate(store, mandate_id, signer)
        except Exception:
            report.mandates_failed.append(mandate_id)
            continue
        if result["signing_state"] == SigningState.SIGNED.value:
            report.mandates_signed.append(mandate_id)
    return report


def recover_pending_grant_signatures(store: Store, signer: Signer) -> RecoveryReport:
    report = RecoveryReport()
    for grant_id in store.list_ids(GRANTS):
        grant = store.peek(GRANTS, grant_id)
        if grant is None or grant["state"] != GrantState.PENDING_SIGNATURE.value:
            continue
        try:
            result = grants.sign_and_activate_grant(store, grant_id, signer)
        except grants.GrantError:
            # e.g. expired before activation — not a transient failure to
            # retry; SPEC.md Section 6 requires fresh confirmation instead.
            report.grants_failed.append(grant_id)
            continue
        except Exception:
            report.grants_failed.append(grant_id)
            continue
        if result["state"] == GrantState.ACTIVE.value:
            report.grants_activated.append(grant_id)
    return report


def drain(store: Store, signer: Signer) -> RecoveryReport:
    """`POST /internal/authority/drain`: recover mandates first, then
    grants — a grant cannot activate until its mandate is SIGNED, so signing
    mandates first maximizes progress in one pass."""
    mandate_report = recover_pending_mandate_signatures(store, signer)
    grant_report = recover_pending_grant_signatures(store, signer)
    return RecoveryReport(
        mandates_signed=mandate_report.mandates_signed,
        mandates_failed=mandate_report.mandates_failed,
        grants_activated=grant_report.grants_activated,
        grants_failed=grant_report.grants_failed,
    )
