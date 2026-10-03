"""Compact JWS profile shared by mandate, grant and receipt tokens.

Per SPEC.md Sections 5, 6 and 9: ``alg: PS256`` only, an explicit ``typ`` per
token kind, an allowlisted ``kid``, and the *exact* canonical payload bytes
signed and stored — never a re-serialization. Verification must reject any
other algorithm (defeats "alg confusion" attacks), any other typ (defeats
"never accept a receipt as a grant" / "never accept a mandate or receipt
where a grant is required"), and any kid not present in the caller-supplied
trusted registry (defeats "never fetch verification keys from a URL inside
the token").
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from services.shared.crypto import Signer, VerifierRegistry
from services.shared.normalize import canonical_bytes

ALG = "PS256"


class JwsError(ValueError):
    """Any structural, algorithm, type, key or signature verification failure."""


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padding = "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s + padding)
    except Exception as exc:
        raise JwsError("invalid base64url segment") from exc


def encode_compact(*, typ: str, kid: str, payload_bytes: bytes, signer: Signer) -> str:
    """Sign ``payload_bytes`` (already canonicalized) and return a compact JWS.

    ``signer.kid`` must equal ``kid`` — the caller states which key it wants
    to sign with, and the signer confirms it. This is a defensive check
    against wiring the wrong signer into the wrong token type.
    """
    if signer.kid != kid:
        raise JwsError(f"signer kid {signer.kid!r} does not match requested kid {kid!r}")
    header = {"alg": ALG, "typ": typ, "kid": kid}
    header_b64 = _b64url_encode(canonical_bytes(header))
    payload_b64 = _b64url_encode(payload_bytes)
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = signer.sign(signing_input)
    sig_b64 = _b64url_encode(signature)
    return f"{header_b64}.{payload_b64}.{sig_b64}"


@dataclass(frozen=True)
class VerifiedJws:
    header: dict
    payload_bytes: bytes
    kid: str


def verify_compact(
    token: str,
    *,
    expected_typ: str,
    registry: VerifierRegistry,
) -> VerifiedJws:
    """Verify a compact JWS and return its header/payload iff everything matches.

    Checks, all mandatory:
    - exactly three dot-separated segments
    - header decodes to a JSON object with alg == PS256 (reject any other alg,
      including "none")
    - header typ == expected_typ (caller states what kind of token this must be)
    - kid is present in the *caller-supplied* registry (never derived from the
      token itself)
    - signature verifies over the exact signing input bytes
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise JwsError("not a 3-part compact JWS")
    header_b64, payload_b64, sig_b64 = parts

    import json

    try:
        header = json.loads(_b64url_decode(header_b64))
    except Exception as exc:
        raise JwsError("invalid JWS header JSON") from exc
    if not isinstance(header, dict):
        raise JwsError("JWS header must be an object")

    if header.get("alg") != ALG:
        raise JwsError(f"unsupported alg: {header.get('alg')!r}")
    if header.get("typ") != expected_typ:
        raise JwsError(f"unexpected typ: {header.get('typ')!r} (wanted {expected_typ!r})")
    kid = header.get("kid")
    if not isinstance(kid, str) or not registry.allows_kid(kid):
        raise JwsError(f"kid not in trusted registry: {kid!r}")

    payload_bytes = _b64url_decode(payload_b64)
    signature = _b64url_decode(sig_b64)
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")

    public_key = registry.public_key_for(kid)
    if public_key is None:
        raise JwsError(f"no public key for kid: {kid!r}")

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    try:
        public_key.verify(
            signature,
            signing_input,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size),
            hashes.SHA256(),
        )
    except Exception as exc:
        raise JwsError("signature verification failed") from exc

    return VerifiedJws(header=header, payload_bytes=payload_bytes, kid=kid)
