"""Immutable destination versions (SPEC.md Sections 5, 7, 11).

A `destination_account_id` is a stable identifier for "the account this rent
gets paid to"; `destination_version` selects which physical `demo_account_id`
that currently resolves to. Amendments (a legitimate landlord account change)
create a new version — they never mutate an existing one — so the gateway
can "resolve the destination once from the approved immutable version and
use that same value at execution" (SPEC.md Section 7).
"""

from __future__ import annotations

from services.shared.store import Store

DESTINATIONS = "destinations"


class DestinationError(Exception):
    pass


def _key(destination_account_id: str, version: int) -> str:
    return f"{destination_account_id}:{version}"


def create_destination_version(
    store: Store,
    *,
    destination_account_id: str,
    version: int,
    owner_uid: str,
    demo_account_id: str,
    label: str,
) -> dict:
    def txn(t):
        key = _key(destination_account_id, version)
        existing = t.get(DESTINATIONS, key)
        if existing is not None:
            raise DestinationError(f"destination version already exists: {key}")
        record = {
            "destination_account_id": destination_account_id,
            "version": version,
            "owner_uid": owner_uid,
            "demo_account_id": demo_account_id,
            "label": label,
        }
        t.set(DESTINATIONS, key, record)
        return record

    return store.run_transaction(txn)


def resolve_destination(store: Store, destination_account_id: str, version: int) -> dict | None:
    return store.peek(DESTINATIONS, _key(destination_account_id, version))


def next_version(store: Store, destination_account_id: str) -> int:
    """The next unused version number for a destination. Two concurrent
    callers can compute the same number; ``create_destination_version``
    then refuses the second (versions are immutable, never overwritten), so
    this is a hint, not a lock."""
    prefix = f"{destination_account_id}:"
    versions = [
        int(doc_id[len(prefix):])
        for doc_id in store.list_ids(DESTINATIONS)
        if doc_id.startswith(prefix) and doc_id[len(prefix):].isdigit()
    ]
    return max(versions, default=0) + 1


def register_reviewed_destination(
    store: Store,
    *,
    destination_account_id: str,
    owner_uid: str,
    account_number: str,
    label: str,
    scope: str,
) -> dict:
    """Register a user-reviewed new destination version AND the synthetic
    ledger account it pays into, atomically.

    The ledger account id is server-generated and scoped, never the
    user-typed account number: a typed "account number" that happened to
    equal another tenant's ledger id must not be able to route a payment
    into that tenant's account. The typed number is kept only for display
    and for matching what a notice says (SPEC.md Section 7: "Server-defined
    account IDs"). The account starts at a zero balance — the gateway stays
    the only runtime balance writer.
    """
    import secrets

    version = next_version(store, destination_account_id)
    ledger_account_id = f"ext-{scope}-{secrets.token_hex(6)}"

    def txn(t):
        key = _key(destination_account_id, version)
        if t.get(DESTINATIONS, key) is not None:
            raise DestinationError(f"destination version already exists: {key}")
        if t.get("accounts", ledger_account_id) is not None:
            raise DestinationError("ledger account id collision; retry")
        t.set("accounts", ledger_account_id, {
            "account_id": ledger_account_id,
            "owner_uid": f"external:{account_number}",
            "currency": "LKR",
            "balance_minor": 0,
        })
        record = {
            "destination_account_id": destination_account_id,
            "version": version,
            "owner_uid": owner_uid,
            "demo_account_id": ledger_account_id,
            "account_number": account_number,
            "label": label,
        }
        t.set(DESTINATIONS, key, record)
        return record

    return store.run_transaction(txn)
