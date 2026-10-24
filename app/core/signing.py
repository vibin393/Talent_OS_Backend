"""Webhook signing and connector ingress verification (Story 66.7, AD-80, FR-351).

Pure (``hmac``/``hashlib`` only). Talent OS outbound webhooks carry
``TalentOS-Signature: t=<unix ts>,v1=<hex sig>[,v1=<hex sig>]`` where each signature is
HMAC-SHA256(secret, ``f"{t}."`` + body); two ``v1`` values are sent while a secret is being rotated.
:func:`verify_hmac` verifies inbound connector webhooks for the ``WebhookSpec`` schemes.
All comparisons are constant time.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import time
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

HMAC_SCHEMES = frozenset(
    {"hmac_sha256", "hmac_sha1_url", "plivo_v3", "basic", "path_token", "channel_token", "custom"}
)


def sign_v1(secret: bytes, timestamp: int, body: bytes) -> str:
    """Hex HMAC-SHA256 of ``f"{timestamp}."`` + body."""
    return hmac.new(secret, f"{int(timestamp)}.".encode() + body, hashlib.sha256).hexdigest()


def signature_header(secrets: Sequence[bytes], timestamp: int, body: bytes) -> str:
    """``t=<ts>,v1=<sig1>[,v1=<sig2>]`` — one ``v1`` per active secret (dual during rotation)."""
    if not secrets:
        raise ValueError("at least one secret is required")
    parts = [f"t={int(timestamp)}"]
    parts.extend(f"v1={sign_v1(s, timestamp, body)}" for s in secrets)
    return ",".join(parts)


def _eq(a: str | bytes, b: str | bytes) -> bool:
    ab = a.encode() if isinstance(a, str) else a
    bb = b.encode() if isinstance(b, str) else b
    return hmac.compare_digest(ab, bb)


def verify_v1(
    header: str, secrets: Sequence[bytes], body: bytes, *, tolerance: int = 300, now: int | None = None
) -> bool:
    """Return True when the timestamp is within ``tolerance`` seconds and any ``v1`` matches any secret."""
    if not header or not secrets:
        return False
    timestamp: int | None = None
    signatures: list[str] = []
    for item in header.split(","):
        key, sep, value = item.strip().partition("=")
        if not sep:
            return False
        key, value = key.strip(), value.strip()
        if key == "t":
            if timestamp is not None or not value.isascii() or not value.isdigit():
                return False
            timestamp = int(value)
        elif key == "v1":
            if value:
                signatures.append(value.lower())
    if timestamp is None or not signatures:
        return False
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance:
        return False
    matched = False
    for secret in secrets:
        expected = sign_v1(secret, timestamp, body)
        for sig in signatures:
            matched |= _eq(expected, sig)
    return matched


# ------------------------------------------------------------------------------------------ connector ingress


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lname = name.lower()
    for key, value in headers.items():
        if key.lower() == lname:
            return value
    return None


def _matches_digest(provided: str, digest: bytes) -> bool:
    """Accept hex (any case) or standard/url-safe base64 encodings of ``digest``."""
    provided = provided.strip()
    ok = _eq(provided.lower(), digest.hex())
    ok |= _eq(provided, base64.b64encode(digest).decode())
    ok |= _eq(provided.rstrip("="), base64.urlsafe_b64encode(digest).decode().rstrip("="))
    return ok


def _verify_hmac_sha256(
    secret: bytes,
    headers: Mapping[str, str],
    body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
    tolerance: int,
    now: int | None,
) -> bool:
    provided = _header(headers, signature_header or "X-Signature")
    if not provided:
        return False
    provided = provided.strip()
    if provided[:7].lower() == "sha256=":
        provided = provided[7:]
    message = body
    if timestamp_header:
        ts = _header(headers, timestamp_header)
        if ts is None or not ts.strip():
            return False
        ts = ts.strip()
        if ts.isascii() and ts.isdigit():
            current = int(time.time()) if now is None else now
            if abs(current - int(ts)) > tolerance:
                return False
        message = f"{ts}.".encode() + body
    return _matches_digest(provided, hmac.new(secret, message, hashlib.sha256).digest())


def _verify_twilio(
    secret: bytes, headers: Mapping[str, str], body: bytes, url: str | None, signature_header: str | None
) -> bool:
    provided = _header(headers, signature_header or "X-Twilio-Signature")
    if not provided or not url:
        return False
    data = url
    ctype = (_header(headers, "Content-Type") or "application/x-www-form-urlencoded").lower()
    if "application/x-www-form-urlencoded" in ctype and body:
        try:
            pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True, strict_parsing=False)
        except UnicodeDecodeError:
            return False
        data += "".join(k + v for k, v in sorted(pairs))
    expected = base64.b64encode(hmac.new(secret, data.encode(), hashlib.sha1).digest()).decode()  # NOSONAR
    return _eq(provided.strip(), expected)


def _verify_plivo_v3(
    secret: bytes, headers: Mapping[str, str], url: str | None, signature_header: str | None
) -> bool:
    provided = _header(headers, signature_header or "X-Plivo-Signature-V3")
    nonce = _header(headers, "X-Plivo-Signature-V3-Nonce")
    if not provided or nonce is None or not url:
        return False
    expected = base64.b64encode(hmac.new(secret, (url + nonce).encode(), hashlib.sha256).digest()).decode()
    ok = False
    for candidate in provided.split(","):
        ok |= _eq(candidate.strip(), expected)
    return ok


def _verify_basic(secret: bytes, headers: Mapping[str, str]) -> bool:
    auth = _header(headers, "Authorization")
    if not auth:
        return False
    scheme, _, token = auth.strip().partition(" ")
    if scheme.lower() != "basic" or not token:
        return False
    try:
        decoded = base64.b64decode(token.strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    return _eq(decoded, secret)


def _verify_token(secret: bytes, headers: Mapping[str, str], names: Sequence[str]) -> bool:
    for name in names:
        value = _header(headers, name)
        if value is not None:
            return _eq(value.strip().encode(), secret)
    return False


def verify_hmac(
    scheme: str,
    secret: bytes,
    headers: Mapping[str, str],
    body: bytes,
    url: str | None = None,
    *,
    signature_header: str | None = None,
    timestamp_header: str | None = None,
    tolerance: int = 300,
    now: int | None = None,
) -> bool:
    """Verify an inbound connector webhook for a ``WebhookSpec`` scheme. Header lookup is case-insensitive.

    * ``hmac_sha256`` — hex or base64 HMAC-SHA256 of the body (of ``f"{ts}."`` + body when
      ``timestamp_header`` is given); header ``signature_header`` (default ``X-Signature``),
      optional ``sha256=`` prefix.
    * ``hmac_sha1_url`` — Twilio: base64 HMAC-SHA1 of url + sorted form params (key+value);
      ``X-Twilio-Signature``.
    * ``plivo_v3`` — base64 HMAC-SHA256 of url + ``X-Plivo-Signature-V3-Nonce``; ``X-Plivo-Signature-V3``.
    * ``basic`` — ``Authorization: Basic`` credentials equal ``secret`` (``user:pass``).
    * ``path_token`` / ``channel_token`` — token header (``signature_header``, default ``X-Channel-Token``;
      ``channel_token`` also accepts ``X-Goog-Channel-Token``) equals ``secret``.
    * ``custom`` — adapter-specific; raises ``NotImplementedError``.
    """
    match scheme:
        case "hmac_sha256":
            return _verify_hmac_sha256(
                secret, headers, body, signature_header, timestamp_header, tolerance, now
            )
        case "hmac_sha1_url":
            return _verify_twilio(secret, headers, body, url, signature_header)
        case "plivo_v3":
            return _verify_plivo_v3(secret, headers, url, signature_header)
        case "basic":
            return _verify_basic(secret, headers)
        case "path_token":
            return _verify_token(secret, headers, [signature_header or "X-Channel-Token"])
        case "channel_token":
            names = [signature_header] if signature_header else ["X-Goog-Channel-Token", "X-Channel-Token"]
            return _verify_token(secret, headers, names)
        case "custom":
            raise NotImplementedError("custom webhook verification is implemented by the adapter")
        case _:
            raise ValueError(f"unknown webhook verification scheme: {scheme}")


__all__ = ["HMAC_SCHEMES", "sign_v1", "signature_header", "verify_hmac", "verify_v1"]
