"""In-memory transactional document store standing in for Firestore in Phase 1.

Mirrors the two Firestore transaction properties the gateway/authority logic
depends on:

1. A transaction function may be invoked more than once; it must finish all
   its reads before it does any writes (SPEC.md Section 7: "Finish reads
   before writes"). ``Transaction.set`` raises if called after a read that
   happened after an earlier write in the same attempt, and the whole
   transaction is retried from scratch on a version conflict, so a
   transaction function must be a pure function of its reads — it must not
   accumulate side effects across retries itself (that's the caller's job:
   compute everything from ``txn.get`` results).
2. Commit is atomic and isolated: it succeeds only if every document the
   transaction read is still at the version it was read at, otherwise every
   write in that attempt is discarded and the transaction function reruns
   against a fresh snapshot. This is what makes the obligation lock and
   idempotency checks race-safe under concurrent execution attempts.

This is a deliberately small stand-in — no indexes, no queries beyond
get-by-id — sufficient to test the state machine and binding rules without a
live Firestore emulator dependency in Phase 1.
"""

from __future__ import annotations

import copy
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

DocKey = tuple[str, str]  # (collection, doc_id)

T = TypeVar("T")


class TransactionConflict(RuntimeError):
    """Raised when a transaction cannot commit after the retry budget."""


class ReadAfterWriteError(RuntimeError):
    """Raised if a transaction attempts a read after it has already written.

    Enforces SPEC.md Section 7 step 4: "Finish reads before writes."
    """


class Store:
    def __init__(self) -> None:
        self._data: dict[DocKey, dict[str, Any]] = {}
        self._versions: dict[DocKey, int] = {}
        self._lock = threading.Lock()

    # Non-transactional convenience reads (for owner-facing status APIs and
    # scheduled worker scans that do not need to participate in a write
    # transaction — a real Firestore-backed worker would use a field-filtered
    # query instead of an id scan; this stand-in only supports iterating all
    # ids in a collection as a snapshot, which is enough for Phase 1/2 tests).
    def list_ids(self, collection: str) -> list[str]:
        with self._lock:
            return [doc_id for (coll, doc_id) in self._data if coll == collection]

    def peek(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            key = (collection, doc_id)
            doc = self._data.get(key)
            return copy.deepcopy(doc) if doc is not None else None

    def _raw_get(self, collection: str, doc_id: str) -> tuple[dict[str, Any] | None, int]:
        with self._lock:
            key = (collection, doc_id)
            doc = self._data.get(key)
            version = self._versions.get(key, 0)
            return (copy.deepcopy(doc) if doc is not None else None, version)

    def _commit(self, reads: dict[DocKey, int], writes: dict[DocKey, dict[str, Any] | None]) -> bool:
        with self._lock:
            for key, expected_version in reads.items():
                if self._versions.get(key, 0) != expected_version:
                    return False
            for key, data in writes.items():
                if data is None:
                    self._data.pop(key, None)
                else:
                    self._data[key] = copy.deepcopy(data)
                self._versions[key] = self._versions.get(key, 0) + 1
            return True

    def run_transaction(self, fn: Callable[["Transaction"], T], max_attempts: int = 200) -> T:
        last_conflict: Exception | None = None
        for attempt in range(max_attempts):
            txn = Transaction(self)
            result = fn(txn)
            if self._commit(txn.reads, txn.writes):
                return result
            last_conflict = TransactionConflict(f"commit conflict on attempt {attempt}")
            time.sleep(random.uniform(0, 0.002))
        raise last_conflict or TransactionConflict("exhausted transaction retry budget")


@dataclass
class Transaction:
    store: Store
    reads: dict[DocKey, int] = field(default_factory=dict)
    writes: dict[DocKey, dict[str, Any] | None] = field(default_factory=dict)
    _cache: dict[DocKey, dict[str, Any] | None] = field(default_factory=dict)
    _write_started: bool = False

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        key = (collection, doc_id)
        if key in self._cache:
            return copy.deepcopy(self._cache[key])
        if self._write_started:
            raise ReadAfterWriteError(
                f"attempted to read {key!r} after a write started in this transaction"
            )
        doc, version = self.store._raw_get(collection, doc_id)
        self.reads[key] = version
        self._cache[key] = doc
        return copy.deepcopy(doc) if doc is not None else None

    def set(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self._write_started = True
        key = (collection, doc_id)
        self.writes[key] = data
        self._cache[key] = data

    def delete(self, collection: str, doc_id: str) -> None:
        self._write_started = True
        key = (collection, doc_id)
        self.writes[key] = None
        self._cache[key] = None
