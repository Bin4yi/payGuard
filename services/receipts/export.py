"""Portable evidence bundles: export and verify. SPEC.md Section 9
"In-app verification (required)".

    "Export signed receipts, the referenced signed mandate and actual
    request records. Recompute supporting-record digests so
    approved-versus-attempted details are checked rather than merely
    displayed. Altering an unsigned supporting record must fail its
    referenced digest check."

A bundle is self-*contained* but deliberately not self-*certifying*: it
carries the signed receipts, the signed mandate they reference, and the
unsigned supporting records (the actual attempted payment tuples), but it
does NOT carry the public keys needed to check any of them. SPEC.md
Section 9 is explicit that "A key included in the export is not a trust
root" — ``verify_bundle`` therefore takes its trusted key registries as
arguments, from whatever configuration the verifier already had, and has
no code path that could adopt a key found inside the bundle it is
checking.

What a bundle must never contain: grant tokens, session tokens,
confirmation nonces, or any private key material (SPEC.md Section 9:
"Never export tokens"). ``build_bundle`` copies fields explicitly, one by
one, rather than dumping whole stored records, precisely so that adding a
sensitive field to a stored record later cannot silently start leaking it
into exports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from services.receipts.signing import RECEIPT_TYP, receipt_chain_digest
from services.receipts.verify import MANDATE_TYP
from services.shared.crypto import VerifierRegistry
from services.shared.digests import attempt_digest_from_stored, mandate_payload_digest
from services.shared.jws import JwsError, verify_compact
from services.shared.schemas import MandatePayload
from services.shared.store import Store

BUNDLE_SCHEMA_VERSION = "receipt-bundle-v1"

RECEIPTS = "receipts"
WORKFLOW_EVENTS = "workflow_events"
PAYMENT_ATTEMPTS = "payment_attempts"
MANDATES = "mandates"
JOBS = "jobs"


class ExportError(Exception):
    pass


def build_bundle(store: Store, workflow_id: str, *, owner_uid: str) -> dict:
    """Assemble an owned, portable evidence bundle for one workflow (job).

    Ownership is checked against the job itself, not taken on trust from
    the caller — an export is a read of private evidence like any other.
    """
    job = store.peek(JOBS, workflow_id)
    if job is None or job["owner_uid"] != owner_uid:
        raise ExportError("workflow not found or not owned")

    prefix = f"{workflow_id}:"

    receipts: list[dict] = []
    for doc_id in store.list_ids(RECEIPTS):
        if not doc_id.startswith(prefix):
            continue
        stored = store.peek(RECEIPTS, doc_id)
        if stored is None:
            continue
        receipts.append({
            "sequence": stored["sequence"],
            "compact_jws": stored["compact_jws"],
            "digest": stored["digest"],
            "key_id": stored["key_id"],
        })
    receipts.sort(key=lambda r: r["sequence"])

    # The highest sequence the gateway ever produced, signed or not — lets a
    # verifier tell "nothing was withheld" apart from "the signer is behind".
    highest_produced_sequence = 0
    attempt_ids: list[str] = []
    for doc_id in store.list_ids(WORKFLOW_EVENTS):
        if not doc_id.startswith(prefix):
            continue
        event = store.peek(WORKFLOW_EVENTS, doc_id)
        if event is None:
            continue
        highest_produced_sequence = max(highest_produced_sequence, event["sequence"])
        attempt_id = event.get("payload", {}).get("attempt_id")
        if attempt_id and attempt_id not in attempt_ids:
            attempt_ids.append(attempt_id)

    attempts: list[dict] = []
    for attempt_id in attempt_ids:
        stored = store.peek(PAYMENT_ATTEMPTS, attempt_id)
        if stored is None or stored.get("owner_uid") != owner_uid:
            continue
        # Explicit field copy: never a blanket dump of the stored record.
        attempts.append({
            "attempt_id": stored["attempt_id"],
            "owner_uid": stored["owner_uid"],
            "grant_id": stored["grant_id"],
            "idempotency_key": stored["idempotency_key"],
            "request": dict(stored["request"]),
            "request_digest": stored["request_digest"],
            "attempt_digest": stored["attempt_digest"],
            "decision": stored["decision"],
            "reason_codes": list(stored["reason_codes"]),
        })

    mandate_export = None
    mandate = store.peek(MANDATES, job["mandate_id"])
    if mandate is not None and mandate.get("compact_jws"):
        mandate_export = {
            "mandate_id": mandate["mandate_id"],
            "version": mandate["version"],
            "compact_jws": mandate["compact_jws"],
            "digest": mandate["digest"],
            "key_id": mandate["key_id"],
            "state": mandate["state"],
            "signing_state": mandate["signing_state"],
        }

    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "owner_uid": owner_uid,
        "highest_produced_sequence": highest_produced_sequence,
        "receipts": receipts,
        "mandate": mandate_export,
        "attempts": attempts,
        "trust_note": (
            "This bundle carries no public keys. Verify it against keys you already "
            "trust and configured independently; a key shipped inside an export is "
            "not a trust root."
        ),
    }


@dataclass
class BundleItemResult:
    sequence: int
    status: str  # VERIFIED | SIGNATURE_INVALID | DIGEST_MISMATCH | SEQUENCE_MISMATCH | CHAIN_BROKEN | MISSING
    detail: str | None = None


@dataclass
class BundleVerification:
    workflow_id: str
    receipts: list[BundleItemResult] = field(default_factory=list)
    mandate_status: str = "ABSENT"  # VERIFIED | SIGNATURE_INVALID | DIGEST_MISMATCH | ABSENT
    attempt_failures: list[str] = field(default_factory=list)
    gaps: list[int] = field(default_factory=list)
    completeness_unknown: bool = True
    contains_no_credentials: bool = True

    @property
    def all_verified(self) -> bool:
        return (
            bool(self.receipts)
            and all(r.status == "VERIFIED" for r in self.receipts)
            and self.mandate_status == "VERIFIED"
            and not self.attempt_failures
            and self.contains_no_credentials
        )


# Field names that must never appear anywhere in an exported bundle.
_FORBIDDEN_BUNDLE_KEYS = frozenset({"nonce", "token", "compact_grant", "private_key", "id_token", "token_hash"})


def _scan_for_credentials(node, path: str = "$") -> list[str]:
    """Structural check that an export carries no credential-shaped field.

    ``compact_jws`` is expected and allowed (receipts and the mandate are
    *meant* to be exported); a grant's token, a session id token, a
    confirmation nonce or a token hash are not.
    """
    findings: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in _FORBIDDEN_BUNDLE_KEYS:
                findings.append(f"{path}.{key}")
            findings.extend(_scan_for_credentials(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for i, value in enumerate(node):
            findings.extend(_scan_for_credentials(value, f"{path}[{i}]"))
    return findings


def verify_bundle(
    bundle: dict,
    *,
    receipt_registry: VerifierRegistry,
    authority_registry: VerifierRegistry,
) -> BundleVerification:
    """Check an exported bundle using only the bundle's own bytes plus the
    caller's independently-configured trusted keys."""
    if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise ExportError(f"unsupported bundle schema: {bundle.get('schema_version')!r}")

    workflow_id = bundle["workflow_id"]
    result = BundleVerification(workflow_id=workflow_id)

    leaked = _scan_for_credentials(bundle)
    if leaked:
        result.contains_no_credentials = False
        result.attempt_failures.append(f"bundle contains credential-shaped fields: {leaked}")

    # --- The signed mandate, and its recomputed digest. ---
    mandate = bundle.get("mandate")
    mandate_digest_from_payload: str | None = None
    if mandate is not None:
        try:
            verified = verify_compact(
                mandate["compact_jws"], expected_typ=MANDATE_TYP, registry=authority_registry
            )
            payload = MandatePayload(**json.loads(verified.payload_bytes))
            mandate_digest_from_payload = mandate_payload_digest(payload)
            if mandate_digest_from_payload != mandate.get("digest"):
                result.mandate_status = "DIGEST_MISMATCH"
            else:
                result.mandate_status = "VERIFIED"
        except Exception as exc:
            result.mandate_status = "SIGNATURE_INVALID"
            result.attempt_failures.append(f"mandate: {exc}")

    # --- Every unsigned supporting record, recomputed from its own content. ---
    for attempt in bundle.get("attempts", []):
        try:
            recomputed = attempt_digest_from_stored(attempt)
        except Exception as exc:
            result.attempt_failures.append(f"attempt {attempt.get('attempt_id')!r}: unreadable ({exc})")
            continue
        if recomputed != attempt.get("attempt_digest"):
            result.attempt_failures.append(
                f"attempt {attempt.get('attempt_id')!r}: content no longer matches its recorded attempt_digest"
            )

    # --- The receipt chain, over the bundle's own stored bytes. ---
    by_seq = {r["sequence"]: r for r in bundle.get("receipts", [])}
    if by_seq:
        max_seq = max(by_seq)
        result.gaps = [s for s in range(1, max_seq) if s not in by_seq]
        prev_chain_digest: str | None = None
        claimed_attempt_digests: set[str] = set()

        for seq in range(1, max_seq + 1):
            entry = by_seq.get(seq)
            if entry is None:
                result.receipts.append(BundleItemResult(seq, "MISSING"))
                prev_chain_digest = None
                continue

            recomputed_chain_digest = receipt_chain_digest(entry["compact_jws"])
            if recomputed_chain_digest != entry.get("digest"):
                result.receipts.append(BundleItemResult(seq, "DIGEST_MISMATCH"))
                prev_chain_digest = recomputed_chain_digest
                continue

            try:
                verified = verify_compact(
                    entry["compact_jws"], expected_typ=RECEIPT_TYP, registry=receipt_registry
                )
            except JwsError as exc:
                result.receipts.append(BundleItemResult(seq, "SIGNATURE_INVALID", str(exc)))
                prev_chain_digest = recomputed_chain_digest
                continue

            payload = json.loads(verified.payload_bytes)
            if payload.get("sequence") != seq or payload.get("workflow_id") != workflow_id:
                result.receipts.append(BundleItemResult(seq, "SEQUENCE_MISMATCH"))
                prev_chain_digest = recomputed_chain_digest
                continue

            if payload.get("previous_receipt_digest") != prev_chain_digest:
                result.receipts.append(BundleItemResult(seq, "CHAIN_BROKEN"))
                prev_chain_digest = recomputed_chain_digest
                continue

            # Cross-check the signed claims against the exported supporting records.
            claimed_mandate_digest = payload.get("mandate_digest")
            if (
                claimed_mandate_digest is not None
                and mandate_digest_from_payload is not None
                and claimed_mandate_digest != mandate_digest_from_payload
            ):
                result.receipts.append(
                    BundleItemResult(seq, "DIGEST_MISMATCH", "receipt references a different mandate than the one exported")
                )
                prev_chain_digest = recomputed_chain_digest
                continue

            if payload.get("attempt_digest") is not None:
                claimed_attempt_digests.add(payload["attempt_digest"])

            result.receipts.append(BundleItemResult(seq, "VERIFIED"))
            prev_chain_digest = recomputed_chain_digest

        exported_attempt_digests = {a.get("attempt_digest") for a in bundle.get("attempts", [])}
        for claimed in claimed_attempt_digests - exported_attempt_digests:
            result.attempt_failures.append(
                f"a receipt references attempt_digest {claimed} but no matching attempt record was exported"
            )

        pending_suffix = max_seq < bundle.get("highest_produced_sequence", max_seq)
    else:
        pending_suffix = bundle.get("highest_produced_sequence", 0) > 0

    result.completeness_unknown = (
        bool(result.gaps)
        or pending_suffix
        or not result.receipts
        or any(r.status != "VERIFIED" for r in result.receipts)
        or bool(result.attempt_failures)
    )
    return result
