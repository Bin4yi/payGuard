"""Digest computation wiring schemas to the normalize.py canonical profile.

Each digest uses its own schema-version tag as the domain separator (SPEC.md
Section 9: "a digest never appears inside its own input"), so a mandate
payload digest, a payment tuple digest and an attempt digest can never
collide with each other even over overlapping field values.
"""

from __future__ import annotations

from services.shared.normalize import digest_for, digest_hex
from services.shared.schemas import AttemptRecord, MandatePayload, PaymentTuple


def mandate_payload_digest(payload: MandatePayload) -> str:
    return digest_hex("mandate-v1", payload.model_dump(mode="json"))


def mandate_payload_digest_bytes(payload: MandatePayload) -> bytes:
    return digest_for("mandate-v1", payload.model_dump(mode="json"))


def payment_tuple_digest(tuple_: PaymentTuple) -> str:
    """The ``request_digest``: semantic payment tuple only."""
    return digest_hex("payment-tuple-v1", tuple_.model_dump(mode="json"))


def attempt_record_digest(record: AttemptRecord) -> str:
    """The ``attempt_digest``: full attempt context, request_digest included
    as a *value* inside it (not as its own input) so exported context is
    also protected."""
    return digest_hex("attempt-v1", record.model_dump(mode="json"))


def attempt_digest_from_stored(attempt: dict) -> str:
    """Recompute an attempt record's digest from its *stored* fields.

    Used by both in-app verification and exported-bundle verification, so
    the two can never drift apart and disagree about whether a supporting
    record has been altered (SPEC.md Section 9: "Altering an unsigned
    supporting record must fail its referenced digest check").
    """
    from services.shared.schemas import PaymentRequest

    record = AttemptRecord(
        owner_uid=attempt["owner_uid"],
        attempt_id=attempt["attempt_id"],
        job_id=attempt["request"]["job_id"],
        mandate_id=attempt["request"]["mandate_id"],
        grant_id=attempt["grant_id"],
        idempotency_key=attempt["idempotency_key"],
        request=PaymentRequest(**attempt["request"]),
        request_digest=attempt["request_digest"],
    )
    return attempt_record_digest(record)


def payment_tuple_from_request(request, obligation_id: str, amount_mode: str) -> PaymentTuple:
    return PaymentTuple(
        action="rent.pay",
        debit_account_id=request.debit_account_id,
        destination_account_id=request.destination_account_id,
        destination_version=request.destination_version,
        amount_mode=amount_mode,
        amount_minor=request.amount_minor,
        currency=request.currency,
        payment_reference=request.payment_reference,
        obligation_id=obligation_id,
    )
