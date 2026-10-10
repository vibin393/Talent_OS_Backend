# ruff: noqa: E501
"""Signed public marketing tokens (Epic 39 A6): ``<tenant_id_b32>.<message_id_b32>.<hmac16>``.

The HMAC-SHA256 is computed over ``(tenant_id, message_id, token_nonce)`` with a key HKDF-derived from the ``audit``
master key (``info="campaign-link"``). Verification is stateless for the tenant (the ids are in the token) and then
checks the nonce stored on the message. Every failure is the same 404 (no oracle). Tokens are never logged.
"""

import base64
import hashlib
import hmac
import secrets
import uuid
from functools import cache

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.security import key_material
from app.core.settings import get_settings

MAC_CHARS = 16


@cache
def _key(material: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"campaign-link").derive(
        material.encode()
    )


def new_nonce() -> str:
    return secrets.token_hex(16)


def _b32(value: uuid.UUID) -> str:
    return base64.b32encode(value.bytes).decode().rstrip("=").lower()


def _unb32(value: str) -> uuid.UUID:
    padded = value.upper() + "=" * (-len(value) % 8)
    raw = base64.b32decode(padded)
    if len(raw) != 16:
        raise ValueError("bad id")
    return uuid.UUID(bytes=raw)


def _mac(tenant_id: uuid.UUID, message_id: uuid.UUID, nonce: str) -> str:
    msg = f"{tenant_id}:{message_id}:{nonce}".encode()
    digest = hmac.new(_key(key_material("audit")), msg, hashlib.sha256).digest()
    return base64.b32encode(digest).decode().rstrip("=").lower()[:MAC_CHARS]


def make(tenant_id: uuid.UUID, message_id: uuid.UUID, nonce: str) -> str:
    return f"{_b32(tenant_id)}.{_b32(message_id)}.{_mac(tenant_id, message_id, nonce)}"


def parse(token: str) -> tuple[uuid.UUID, uuid.UUID, str] | None:
    """``(tenant_id, message_id, mac)`` or None for a malformed token (the MAC is checked by ``verify``)."""
    parts = (token or "").split(".")
    if len(parts) != 3 or len(parts[2]) != MAC_CHARS or len(token) > 120:
        return None
    try:
        return _unb32(parts[0]), _unb32(parts[1]), parts[2]
    except (ValueError, TypeError):
        return None


def verify(tenant_id: uuid.UUID, message_id: uuid.UUID, mac: str, nonce: str) -> bool:
    return hmac.compare_digest(_mac(tenant_id, message_id, nonce), mac)


def base_url() -> str:
    s = get_settings()
    return (s.marketing_link_base_url or s.frontend_url).rstrip("/")


def api_url(token: str, suffix: str) -> str:
    return f"{base_url()}/api/v1/public/m/{token}/{suffix}"


def page_url(token: str, suffix: str) -> str:
    return f"{base_url()}/m/{token}/{suffix}"
