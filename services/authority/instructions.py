"""Rent instructions: the user's own standing payment instruction.

SPEC.md Section 5, rule 5: "The initial approved destination comes from the
user's rent instruction, not automatic endorsement of document text." An
instruction is the authenticated user's own setting — no model tool can
create or edit one (SPEC.md Section 10: "Never expose approval, new-payee
creation or token issuance as model tools"), and it can only point at a
destination version the user already owns.

Instructions are versioned and every version is immutable: an update writes
version N+1 and moves the "latest" pointer, it never rewrites version N. A
mandate snapshots the exact instruction version it was drafted from, so
editing an instruction later can never retroactively change what an
already-approved mandate says.
"""

from __future__ import annotations

from pydantic import ValidationError

from services.shared.schemas import AMOUNT_CEILING_MINOR, Money, PositiveInt, StrictModel
from services.shared.store import Store

RENT_INSTRUCTIONS = "rent_instructions"
RENT_INSTRUCTION_VERSIONS = "rent_instruction_versions"
DESTINATIONS = "destinations"
OBLIGATIONS = "obligations"


class InstructionError(Exception):
    pass


class _InstructionFields(StrictModel):
    amount_minor: Money
    currency: str
    destination_account_id: str
    destination_version: PositiveInt


def _version_key(instruction_id: str, version: int) -> str:
    return f"{instruction_id}:{version}"


def get_latest(store: Store, instruction_id: str) -> dict | None:
    return store.peek(RENT_INSTRUCTIONS, instruction_id)


def update_instruction(
    store: Store,
    *,
    instruction_id: str,
    owner_uid: str,
    amount_minor: int,
    currency: str,
    destination_account_id: str,
    destination_version: int,
) -> dict:
    """Write a new immutable instruction version and advance the pointer."""
    try:
        fields = _InstructionFields(
            amount_minor=amount_minor,
            currency=currency,
            destination_account_id=destination_account_id,
            destination_version=destination_version,
        )
    except ValidationError as exc:
        raise InstructionError(
            f"invalid instruction (amount must be a whole number of minor units, 1..{AMOUNT_CEILING_MINOR}): {exc.errors()[0]['msg']}"
        ) from exc
    if fields.currency != "LKR":
        raise InstructionError("only LKR is supported in v1")

    def txn(t):
        current = t.get(RENT_INSTRUCTIONS, instruction_id)
        if current is None or current["owner_uid"] != owner_uid:
            raise InstructionError("instruction not found or not owned")
        destination = t.get(DESTINATIONS, f"{fields.destination_account_id}:{fields.destination_version}")
        if destination is None or destination["owner_uid"] != owner_uid:
            # Never let an instruction point at an account the user has not
            # registered through the authenticated review flow.
            raise InstructionError("destination version not found or not owned")

        new_version = current["version"] + 1
        record = {
            "instruction_id": instruction_id,
            "owner_uid": owner_uid,
            "version": new_version,
            "destination_account_id": fields.destination_account_id,
            "destination_version": fields.destination_version,
            "amount_minor": fields.amount_minor,
            "currency": fields.currency,
        }
        t.set(RENT_INSTRUCTION_VERSIONS, _version_key(instruction_id, new_version), record)
        t.set(RENT_INSTRUCTIONS, instruction_id, record)
        return record

    return store.run_transaction(txn)
