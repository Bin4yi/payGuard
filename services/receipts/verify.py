"""In-app receipt verification. SPEC.md Section 9 "In-app verification".

Three distinct statements, never conflated:

- ``SIGNED``: a compact JWS is stored for this sequence (``workflow_events``
  says so). Says nothing about whether anyone has checked it.
- ``VERIFIED``: this verifier just checked the signature, type, sequence,
  chain linkage and (optionally) the supporting mandate/attempt records
  against pinned trusted keys and recomputed digests.
- Completeness is a *separate* question from either: a gap between two
  present sequences means something was removed; a run that simply stops
  at the highest sequence anyone has signed so far is an ordinary pending
  suffix, not evidence of tampering. Both are reported explicitly rather
  than one being silently read as the other.

The trusted public keys are supplied by the caller (``receipt_registry``,
``authority_registry``) — this module never trusts a key found inside an
exported bundle (SPEC.md Section 9: "A key included in the export is not a
trust root.").
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from services.gateway.enforcement import PAYMENT_ATTEMPTS
from services.shared.crypto import VerifierRegistry
from services.shared.digests import attempt_digest_from_stored, mandate_payload_digest
from services.shared.jws import JwsError, verify_compact
from services.shared.schemas import MandatePayload
from services.shared.store import Store
from services.receipts.signing import RECEIPT_TYP, receipt_chain_digest

WORKFLOW_EVENTS = "workflow_events"
RECEIPTS = "receipts"
GRANTS = "grants"
MANDATES = "mandates"

MANDATE_TYP = "payguard-mandate+jwt"


@dataclass
class ReceiptVerification:
    sequence: int
    status: str  # "VERIFIED" | "MISSING" | "SIGNATURE_INVALID" | "DIGEST_MISMATCH"
    # | "SEQUENCE_MISMATCH" | "CHAIN_BROKEN" | "TAMPERED_SUPPORTING_RECORD"
    detail: str | None = None


@dataclass
class ChainVerificationResult:
    workflow_id: str
    entries: list[ReceiptVerification] = field(default_factory=list)
    gaps: list[int] = field(default_factory=list)
    completeness_unknown: bool = True
    highest_verified_sequence: int = 0

    @property
    def all_verified(self) -> bool:
        return bool(self.entries) and all(e.status == "VERIFIED" for e in self.entries)


def _verify_supporting_records(
    store: Store, payload: dict, *, authority_registry: VerifierRegistry
) -> tuple[bool, str | None]:
    """Recompute mandate_digest and attempt_digest from the *current* stored
    records rather than trusting the claims inside the (already-verified)
    receipt payload — a receipt can only prove what was true when it was
    signed; this proves the referenced records still match that today."""
    grant_id = payload.get("grant_id")
    if grant_id is not None:
        grant = store.peek(GRANTS, grant_id)
        if grant is None:
            return False, f"referenced grant {grant_id!r} not found"
        mandate = store.peek(MANDATES, grant["mandate_id"])
        if mandate is None or not mandate.get("compact_jws"):
            return False, "referenced mandate not found or unsigned"
        try:
            verified_mandate = verify_compact(
                mandate["compact_jws"], expected_typ=MANDATE_TYP, registry=authority_registry
            )
            mandate_payload = MandatePayload(**json.loads(verified_mandate.payload_bytes))
        except JwsError as exc:
            return False, f"referenced mandate signature invalid: {exc}"
        recomputed_mandate_digest = mandate_payload_digest(mandate_payload)
        claimed = payload.get("mandate_digest")
        if claimed is not None and claimed != recomputed_mandate_digest:
            return False, "mandate_digest does not match recomputed digest of the current mandate record"

    attempt_id = payload.get("_attempt_id")  # see note in verify_chain about how this is populated
    if attempt_id is not None:
        attempt = store.peek(PAYMENT_ATTEMPTS, attempt_id)
        if attempt is None:
            return False, f"referenced attempt {attempt_id!r} not found"
        recomputed_attempt_digest = attempt_digest_from_stored(attempt)
        if recomputed_attempt_digest != attempt.get("attempt_digest"):
            return False, "stored attempt record no longer matches its own recorded attempt_digest"
        claimed_attempt_digest = payload.get("attempt_digest")
        if claimed_attempt_digest is not None and claimed_attempt_digest != recomputed_attempt_digest:
            return False, "attempt_digest does not match recomputed digest of the current attempt record"

    return True, None


def verify_chain(
    store: Store,
    workflow_id: str,
    *,
    receipt_registry: VerifierRegistry,
    authority_registry: VerifierRegistry | None = None,
    check_supporting_records: bool = True,
) -> ChainVerificationResult:
    prefix = f"{workflow_id}:"
    receipts_by_seq: dict[int, dict] = {}
    for doc_id in store.list_ids(RECEIPTS):
        if not doc_id.startswith(prefix):
            continue
        r = store.peek(RECEIPTS, doc_id)
        if r is not None:
            receipts_by_seq[r["sequence"]] = r

    # The highest sequence the gateway has *produced* at all (signed or
    # still PENDING) — needed to tell a merely-pending suffix (nothing wrong,
    # just not signed yet) apart from a genuinely complete chain.
    highest_produced_sequence = 0
    for doc_id in store.list_ids(WORKFLOW_EVENTS):
        if not doc_id.startswith(prefix):
            continue
        event = store.peek(WORKFLOW_EVENTS, doc_id)
        if event is not None:
            highest_produced_sequence = max(highest_produced_sequence, event["sequence"])

    if not receipts_by_seq:
        # Nothing signed yet. Anything produced is a pending *suffix* (the
        # signer hasn't run), not a hole in the middle — so no gaps are
        # reported, only that completeness can't be claimed.
        return ChainVerificationResult(workflow_id=workflow_id, completeness_unknown=True, gaps=[])

    max_seq = max(receipts_by_seq)
    gaps = [s for s in range(1, max_seq) if s not in receipts_by_seq]  # a *missing middle*, not a pending suffix
    pending_suffix = max_seq < highest_produced_sequence

    entries: list[ReceiptVerification] = []
    prev_stored_chain_digest: str | None = None
    highest_verified = 0

    for seq in range(1, max_seq + 1):
        r = receipts_by_seq.get(seq)
        if r is None:
            entries.append(ReceiptVerification(seq, "MISSING"))
            prev_stored_chain_digest = None
            continue

        recomputed_chain_digest = receipt_chain_digest(r["compact_jws"])
        if recomputed_chain_digest != r.get("digest"):
            entries.append(ReceiptVerification(seq, "DIGEST_MISMATCH"))
            prev_stored_chain_digest = recomputed_chain_digest
            continue

        try:
            verified = verify_compact(r["compact_jws"], expected_typ=RECEIPT_TYP, registry=receipt_registry)
        except JwsError as exc:
            entries.append(ReceiptVerification(seq, "SIGNATURE_INVALID", str(exc)))
            prev_stored_chain_digest = recomputed_chain_digest
            continue

        payload = json.loads(verified.payload_bytes)
        if payload.get("sequence") != seq or payload.get("workflow_id") != workflow_id:
            entries.append(ReceiptVerification(seq, "SEQUENCE_MISMATCH"))
            prev_stored_chain_digest = recomputed_chain_digest
            continue

        if payload.get("previous_receipt_digest") != prev_stored_chain_digest:
            entries.append(ReceiptVerification(seq, "CHAIN_BROKEN"))
            prev_stored_chain_digest = recomputed_chain_digest
            continue

        if check_supporting_records and authority_registry is not None:
            # Look up the originating attempt_id via the (unsigned, but
            # gateway-authored) outbox event that this receipt was signed
            # from, purely as an index — the actual cryptographic check is
            # the digest recomputation, not this lookup.
            event = store.peek(WORKFLOW_EVENTS, f"{workflow_id}:{seq}")
            enriched_payload = dict(payload)
            if event is not None:
                enriched_payload["_attempt_id"] = event["payload"].get("attempt_id")
            ok, detail = _verify_supporting_records(store, enriched_payload, authority_registry=authority_registry)
            if not ok:
                entries.append(ReceiptVerification(seq, "TAMPERED_SUPPORTING_RECORD", detail))
                prev_stored_chain_digest = recomputed_chain_digest
                continue

        entries.append(ReceiptVerification(seq, "VERIFIED"))
        highest_verified = seq
        prev_stored_chain_digest = recomputed_chain_digest

    # Completeness is only "known good" if every sequence from 1..max_seq is
    # present *and* verified, *and* the gateway has not produced any further
    # event still awaiting signature — an internal gap, a non-VERIFIED
    # entry, or an unsigned pending suffix all mean we cannot claim nothing
    # was withheld or altered (though only a gap or a bad entry indicates an
    # actual problem; a pending suffix is normal and expected).
    completeness_unknown = bool(gaps) or pending_suffix or any(e.status != "VERIFIED" for e in entries)

    return ChainVerificationResult(
        workflow_id=workflow_id,
        entries=entries,
        gaps=gaps,
        completeness_unknown=completeness_unknown,
        highest_verified_sequence=highest_verified,
    )
