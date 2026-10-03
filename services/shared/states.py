"""Entity states — SPEC.md Section 8."""

from __future__ import annotations

from enum import Enum


class MandateState(str, Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    REVOKED = "REVOKED"
    SUPERSEDED = "SUPERSEDED"


class SigningState(str, Enum):
    PENDING = "PENDING"
    SIGNED = "SIGNED"


class GrantState(str, Enum):
    PENDING_SIGNATURE = "PENDING_SIGNATURE"
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    REVOKED = "REVOKED"


class ObligationState(str, Enum):
    UNPAID = "UNPAID"
    PAID = "PAID"


class JobStatus(str, Enum):
    WAITING_SIGNATURE = "WAITING_SIGNATURE"
    READY = "READY"
    RUNNING = "RUNNING"
    DONE = "DONE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    FAILED = "FAILED"


class AttemptState(str, Enum):
    RECEIVED = "RECEIVED"
    BLOCKED = "BLOCKED"
    COMMITTED = "COMMITTED"


class ReceiptEventState(str, Enum):
    PENDING = "PENDING"
    SIGNED = "SIGNED"
