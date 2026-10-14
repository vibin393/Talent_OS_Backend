"""Central tenant-claim token (Phase 1, Epic 3 / AD-6) — the hybrid model.

The opaque ``tos_session`` cookie stays the source of truth (long-lived, revocable). From a live
session, central identity mints a **short-lived RS256 JWT** carrying the tenant claim, which other
apps (Croar, Portal, …) validate **offline** against the JWKS served here — no per-request call back
to Talent OS. A revoked session stops working within the token's short TTL.

Local/dev: the signing key is generated once per process. Production loads a managed key from the
secret store (TODO) and rotates it; consumers always re-read JWKS, so rotation is transparent.
"""

import time
import uuid

from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

ISSUER = "talent-os"
AUDIENCE = "unified-platform"
TTL_SECONDS = 300  # 5 minutes — short enough that session revocation takes effect quickly
_KID = "tos-local-1"

_signing_key: RSAKey | None = None


def _key() -> RSAKey:
    global _signing_key
    if _signing_key is None:
        # Dev: generate once per process. Prod: load PEM from the secret store instead.
        _signing_key = RSAKey.generate_key(2048, {"kid": _KID, "use": "sig", "alg": "RS256"}, private=True)
    return _signing_key


def mint_central_token(*, user_id: uuid.UUID, tenant_id: uuid.UUID, tenant_slug: str | None = None) -> str:
    """Mint a short-lived tenant-claim JWT for a signed-in user."""
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "iat": now,
        "exp": now + TTL_SECONDS,
    }
    if tenant_slug:
        claims["tenant_slug"] = tenant_slug
    header = {"alg": "RS256", "kid": _KID, "typ": "JWT"}
    return jwt.encode(header, claims, _key())


def jwks() -> dict[str, object]:
    """Public JWKS for offline validation by other apps."""
    return dict(KeySet([_key()]).as_dict(private=False))
