"""Golden fixtures for canonical serialization and digests (SPEC.md Section 9)."""

from __future__ import annotations

import pytest

from services.shared.normalize import (
    NormalizationError,
    canonical_bytes,
    digest_for,
    digest_hex,
    reject_duplicate_keys,
)


def test_canonical_bytes_sorts_keys_and_strips_whitespace():
    payload = {"b": 1, "a": "x", "c": {"z": 1, "y": 2}}
    out = canonical_bytes(payload)
    assert out == b'{"a":"x","b":1,"c":{"y":2,"z":1}}'


def test_canonical_bytes_is_deterministic_regardless_of_input_key_order():
    p1 = {"a": 1, "b": 2}
    p2 = {"b": 2, "a": 1}
    assert canonical_bytes(p1) == canonical_bytes(p2)


@pytest.mark.parametrize("bad", [True, False, 1.5, float("nan")])
def test_rejects_bool_and_float(bad):
    with pytest.raises(NormalizationError):
        canonical_bytes({"amount_minor": bad})


def test_rejects_non_ascii_string():
    with pytest.raises(NormalizationError):
        canonical_bytes({"label": "café"})


def test_rejects_non_string_dict_key():
    with pytest.raises(NormalizationError):
        canonical_bytes({1: "a"})  # type: ignore[dict-item]


def test_digest_golden_fixture():
    """Freeze this exact digest — a change here means the wire format moved."""
    payload = {"amount_minor": 5_000_000, "currency": "LKR", "obligation_id": "rent-2026-09-01"}
    assert (
        digest_hex("payment-tuple-v1", payload)
        == "sha256:" + digest_for("payment-tuple-v1", payload).hex()
    )
    assert digest_hex("payment-tuple-v1", payload) == (
        "sha256:0fa311c74444df1ff78a7fba5efdc61687c73ed7652d59440610de2d7843a2db"
    )


def test_digest_domain_separation_by_schema_version():
    payload = {"x": 1}
    assert digest_for("schema-a", payload) != digest_for("schema-b", payload)


def test_digest_never_needs_to_embed_itself():
    """A digest is computed purely from schema_version + payload bytes; the
    payload passed in must not already contain a 'digest' field referring to
    itself — this test documents that digest_for takes no such shortcut and
    two payloads differing only by an extra (unrelated) digest-shaped field
    hash differently, i.e. nothing is silently excluded."""
    base = {"a": 1}
    with_extra = {"a": 1, "digest": "sha256:deadbeef"}
    assert digest_for("schema-v1", base) != digest_for("schema-v1", with_extra)


def test_reject_duplicate_keys_in_raw_json():
    with pytest.raises(NormalizationError):
        reject_duplicate_keys('{"a": 1, "a": 2}')


def test_reject_duplicate_keys_allows_normal_json():
    assert reject_duplicate_keys('{"a": 1, "b": 2}') == {"a": 1, "b": 2}
