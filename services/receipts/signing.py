"""Receipt signing worker. SPEC.md Section 9 "Reliability".

Sequence numbers are assigned by the gateway in the same transaction that
creates each outbox event (`services/gateway/enforcement.py`). This worker's
job is only to sign them, in order, with the *receipt* key — a different
Cloud KMS key from the one the authority uses for mandates/grants
(SPEC.md Section 5: "Receipts use a second KMS key"):

1. Find the lowest-sequence still-``PENDING`` event for a workflow.
2. Sign outside any transaction (a KMS call).
3. Compare-and-set the expected chain head before storing — if another
   worker already advanced the chain (or already signed this exact
   sequence), this attempt's signature is discarded, never appended twice.

The signed record chains to the *previous* receipt's stored JWS bytes via
``previous_receipt_digest``, so the in-app verifier (``services/receipts/
verify.py``) can detect a removed or reordered event even though each
receipt only carries a digest, not the full previous payload.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from services.shared.crypto import Signer
from services.shared.jws import encode_compact
from services.shared.normalize import canonical_bytes
from services.shared.schemas import ReceiptEvent
from services.shared.store import Store

WORKFLOW_EVENTS = "workflow_events"
RECEIPTS = "receipts"

RECEIPT_TYP = "payguard-receipt+jwt"
RECEIPT_CHAIN_DOMAIN = "receipt-jws-v1"


class ReceiptError(Exception):
    pass


def _event_doc_id(workflow_id: str, sequence: int) -> str:
    return f"{workflow_id}:{sequence}"


def receipt_chain_digest(compact_jws: str) -> str:
    """Digest of a receipt's exact stored JWS bytes, used as the next
    receipt's ``previous_receipt_digest`` — chains to stored bytes, not to a
    re-derived payload, so any post-hoc alteration of a stored JWS breaks
    the chain at the next link."""
    h = hashlib.sha256()
    h.update(RECEIPT_CHAIN_DOMAIN.encode("ascii"))
    h.update(b"\x00")
    h.update(compact_jws.encode("ascii"))
    return "sha256:" + h.hexdigest()


@dataclass
class SignOutcome:
    signed: bool
    receipt: dict | None = None


def _lowest_pending_sequence(store: Store, workflow_id: str) -> int | None:
    prefix = f"{workflow_id}:"
    lowest = None
    for doc_id in store.list_ids(WORKFLOW_EVENTS):
        if not doc_id.startswith(prefix):
            continue
        event = store.peek(WORKFLOW_EVENTS, doc_id)
        if event is not None and event.get("signing_state") == "PENDING":
            if lowest is None or event["sequence"] < lowest:
                lowest = event["sequence"]
    return lowest


def sign_next_pending(store: Store, workflow_id: str, signer: Signer, *, now_fn=time.time) -> SignOutcome:
    """Sign the single lowest-sequence PENDING event for ``workflow_id``, if
    any. Returns ``SignOutcome(signed=False)`` when there is nothing pending
    or another worker won the race for this sequence."""
    sequence = _lowest_pending_sequence(store, workflow_id)
    if sequence is None:
        return SignOutcome(signed=False)

    event = store.peek(WORKFLOW_EVENTS, _event_doc_id(workflow_id, sequence))
    if event is None or event.get("signing_state") != "PENDING":
        return SignOutcome(signed=False)  # lost the race before we even started

    if sequence > 1:
        previous_receipt = store.peek(RECEIPTS, _event_doc_id(workflow_id, sequence - 1))
        if previous_receipt is None:
            raise ReceiptError(
                f"cannot sign sequence {sequence} for {workflow_id!r}: sequence {sequence - 1} is not signed yet"
            )
        previous_digest = previous_receipt["digest"]
    else:
        previous_digest = None

    payload = event["payload"]
    now = now_fn()
    occurred_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    receipt_payload = ReceiptEvent(
        receipt_id=f"receipt-{workflow_id}-{sequence}",
        workflow_id=workflow_id,
        sequence=sequence,
        event_type=event["event_type"],
        event_producer=event["producer"],
        occurred_at=occurred_at,
        signed_at=occurred_at,
        grant_id=payload.get("grant_id"),
        mandate_digest=payload.get("mandate_digest"),
        request_digest=payload.get("request_digest"),
        attempt_digest=payload.get("attempt_digest"),
        decision=payload.get("decision"),
        reason_codes=tuple(payload.get("reason_codes", ())),
        payment_id=payload.get("payment_id"),
        previous_receipt_digest=previous_digest,
    )
    payload_bytes = canonical_bytes(receipt_payload.model_dump(mode="json"))
    token = encode_compact(typ=RECEIPT_TYP, kid=signer.kid, payload_bytes=payload_bytes, signer=signer)
    digest = receipt_chain_digest(token)

    def txn(t):
        current_event = t.get(WORKFLOW_EVENTS, _event_doc_id(workflow_id, sequence))
        if current_event is None or current_event.get("signing_state") != "PENDING":
            return None  # another worker already signed this sequence
        if sequence > 1:
            head = t.get(RECEIPTS, _event_doc_id(workflow_id, sequence - 1))
            if head is None or head["digest"] != previous_digest:
                return None  # chain head moved; discard this signature

        current_event = dict(current_event)
        current_event["signing_state"] = "SIGNED"
        t.set(WORKFLOW_EVENTS, _event_doc_id(workflow_id, sequence), current_event)

        record = {
            "workflow_id": workflow_id,
            "sequence": sequence,
            "compact_jws": token,
            "digest": digest,
            "key_id": signer.kid,
        }
        t.set(RECEIPTS, _event_doc_id(workflow_id, sequence), record)
        return record

    result = store.run_transaction(txn)
    if result is None:
        return SignOutcome(signed=False)
    return SignOutcome(signed=True, receipt=result)


def drain_workflow(store: Store, workflow_id: str, signer: Signer, *, max_events: int = 1000) -> list[dict]:
    """Sign every currently-pending event for a workflow, in order."""
    signed = []
    for _ in range(max_events):
        outcome = sign_next_pending(store, workflow_id, signer)
        if not outcome.signed:
            break
        signed.append(outcome.receipt)
    return signed
