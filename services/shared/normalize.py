"""Canonical serialization and digests per SPEC.md Section 9.

Rules (do not deviate — signature verification and digest equality depend on
byte-for-byte determinism):

- Sorted-key UTF-8 JSON, no whitespace, ASCII-only identifiers/strings.
- Numeric monetary/count fields must already be Python ``int`` — never float,
  bool, or numeric string. ``bool`` is rejected explicitly because ``bool`` is
  a subclass of ``int`` in Python and would otherwise silently pass isinstance
  checks.
- A digest's input is: the ASCII schema-version string, one zero byte, then
  the exact canonical payload bytes. A digest must never be embedded inside
  the bytes that are hashed to produce it (SPEC.md Section 9: "a digest never
  appears inside its own input").
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


class NormalizationError(ValueError):
    """Raised when a value cannot be canonicalized under the v1 profile."""


def _check_ascii(s: str, path: str) -> None:
    try:
        s.encode("ascii")
    except UnicodeEncodeError as exc:
        raise NormalizationError(f"non-ASCII string at {path}: {s!r}") from exc


def _check_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, str):
        if isinstance(value, str):
            _check_ascii(value, path)
        return
    if isinstance(value, bool):
        raise NormalizationError(f"boolean not allowed in canonical payload at {path}")
    if isinstance(value, int):
        return
    if isinstance(value, float):
        raise NormalizationError(f"float not allowed in canonical payload at {path}")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise NormalizationError(f"non-string key at {path}")
            _check_ascii(k, f"{path}.{k}")
            _check_value(v, f"{path}.{k}")
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _check_value(v, f"{path}[{i}]")
        return
    raise NormalizationError(f"unsupported type {type(value).__name__} at {path}")


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Serialize ``payload`` to the deterministic byte form used for signing/digests.

    Raises NormalizationError if the payload contains a type this profile does
    not allow (float, bool, non-ASCII string, non-string dict key).
    """
    if not isinstance(payload, dict):
        raise NormalizationError("top-level payload must be an object")
    _check_value(payload, "$")
    # json.dumps with sort_keys + separators gives sorted-key, whitespace-free
    # UTF-8-safe text; ensure_ascii keeps it strictly ASCII on the wire.
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("ascii")


def digest_for(schema_version: str, payload: dict[str, Any]) -> bytes:
    """SHA-256 of ASCII schema_version + 0x00 + canonical payload bytes."""
    _check_ascii(schema_version, "$schema_version")
    body = canonical_bytes(payload)
    h = hashlib.sha256()
    h.update(schema_version.encode("ascii"))
    h.update(b"\x00")
    h.update(body)
    return h.digest()


def digest_hex(schema_version: str, payload: dict[str, Any]) -> str:
    return "sha256:" + digest_for(schema_version, payload).hex()


def reject_duplicate_keys(raw: str | bytes) -> dict[str, Any]:
    """Parse JSON, raising if any object in it has a duplicate key.

    Use this for any externally supplied JSON (payment requests, receipts on
    import) instead of the default json.loads, which silently keeps the last
    of a set of duplicate keys.
    """

    def _hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        for k, v in pairs:
            if k in seen:
                raise NormalizationError(f"duplicate JSON key: {k!r}")
            seen[k] = v
        return seen

    return json.loads(raw, object_pairs_hook=_hook)
