"""Central tenant-claim token (Phase 1, Epic 3 / AD-6) — the hybrid model.

The opaque ``tos_session`` cookie stays the source of truth (long-lived, revocable). From a live
session, central identity mints a **short-lived RS256 JWT** carrying the tenant claim, which other
apps (Croar, Portal, …) validate **offline** against the JWKS served here — no per-request call back
to Talent OS. A revoked session stops working within the token's short TTL.

Local/dev: the signing key is generated once per process. Production loads a managed key from the
secret store (TODO) and rotates it; consumers always re-read JWKS, so rotation is transparent.
"""

import hashlib
import os
import time
import uuid
from pathlib import Path

from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

ISSUER = "talent-os"
AUDIENCE = "unified-platform"
TTL_SECONDS = 300  # 5 minutes — short enough that session revocation takes effect quickly

_signing_key: RSAKey | None = None
_kid: str | None = None


def _key_path() -> Path:
    return Path(os.getenv("CENTRAL_JWT_KEY_PATH", "/app/.keys/central_jwt.pem"))


def _compute_kid(key: RSAKey) -> str:
    """A content-derived key id: a short hash of the PUBLIC key. A rotated signing key therefore advertises
    a NEW ``kid`` automatically, so consumers that cache JWKS can tell the keys apart during rotation."""
    public_pem = key.as_pem(private=False)
    return "tos-" + hashlib.sha256(public_pem).hexdigest()[:16]


def _import(pem: bytes, kid: str | None = None) -> RSAKey:
    params = {"use": "sig", "alg": "RS256"}
    if kid is not None:
        params["kid"] = kid
    return RSAKey.import_key(pem, params)


def _key() -> RSAKey:
    """The RS256 signing key for central tokens.

    Loaded from a managed PEM (default ``/app/.keys/central_jwt.pem``, ideally a mounted volume / secret
    store) so a restart or rebuild does NOT rotate it — rotating it silently invalidates every consumer's
    cached JWKS until they also re-read it.

    Production (``app_env == "production"``) FAILS CLOSED: if the managed key is missing or unreadable it
    raises rather than silently self-generating a key (which would mint tokens no consumer's pinned JWKS
    trusts, or — worse — a fresh key every process). Outside production the key is self-generated and
    persisted (best-effort) so local/dev keeps working with no setup. The ``kid`` is derived from the key
    content (``_compute_kid``), so it tracks rotation.
    """
    global _signing_key, _kid
    if _signing_key is not None:
        return _signing_key
    from app.core.settings import get_settings

    production = get_settings().app_env == "production"
    key_path = _key_path()
    pem: bytes | None = None
    try:
        if key_path.exists():
            pem = key_path.read_bytes()
    except Exception:
        pem = None  # unreadable/corrupt
    if pem is not None:
        key = _import(pem)
    elif production:
        msg = (
            f"central JWT signing key missing or unreadable at {key_path} — refusing to self-generate in "
            "production (set CENTRAL_JWT_KEY_PATH to a managed RS256 PEM)"
        )
        raise RuntimeError(msg)
    else:
        key = RSAKey.generate_key(2048, {"use": "sig", "alg": "RS256"}, private=True)
        try:
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_bytes(key.as_pem(private=True))
        except Exception:  # nosec B110 - best-effort; a failure here is non-fatal by design
            pass  # best-effort persistence; a per-process key still works within this process
    kid = _compute_kid(key)
    # Re-import carrying the derived kid so JWKS and the token header advertise it.
    _signing_key = _import(key.as_pem(private=True), kid)
    _kid = kid
    return _signing_key


def _key_id() -> str:
    _key()
    assert _kid is not None
    return _kid


def mint_central_token(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    tenant_slug: str | None = None,
    email: str | None = None,
    name: str | None = None,
    module_caps: dict[str, list[str]] | None = None,
) -> str:
    """Mint a short-lived tenant-claim JWT for a signed-in user. ``email``/``name`` let other apps
    identify (and JIT-provision) the matching local user for seamless SSO. ``module_caps`` (unified RBAC)
    carries the user's effective per-module capabilities so other apps honor them offline; only granted
    caps are included, keeping the token small (an empty map is omitted)."""
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "iat": now,
        "exp": now + TTL_SECONDS,
    }
    if tenant_slug:
        claims["tenant_slug"] = tenant_slug
    if email:
        claims["email"] = email
    if name:
        claims["name"] = name
    if module_caps:
        claims["module_caps"] = module_caps
    header = {"alg": "RS256", "kid": _key_id(), "typ": "JWT"}
    return jwt.encode(header, claims, _key())


def jwks() -> dict[str, object]:
    """Public JWKS for offline validation by other apps."""
    return dict(KeySet([_key()]).as_dict(private=False))
