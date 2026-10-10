"""OpenID Connect relying party (Epic 14, AD-30): authorization code + PKCE (S256), state, nonce.

Only this package imports Authlib / joserfc. HTTP goes through ``http_client()`` (tests swap the factory
for an
``httpx.MockTransport``). ID tokens are never logged.
"""

import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode

import httpx
from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from app.core.errors import Unauthorized

DISCOVERY_TTL_SECONDS = 3600
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _default_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=10.0, follow_redirects=False)


http_client: Callable[[], httpx.AsyncClient] = _default_client


def set_http_client_factory(factory: Callable[[], httpx.AsyncClient] | None) -> None:
    """Route IdP HTTP calls (tests) to a fake provider (``None`` restores the default)."""
    global http_client  # process-wide wiring
    http_client = factory or _default_client
    _discovery_cache.clear()
    _jwks_cache.clear()


def invalid(reason: str) -> Unauthorized:
    return Unauthorized("Single sign-on failed", code="sso_assertion_invalid", details={"reason": reason})


def new_token(length: int = 48) -> str:
    return generate_token(length)


def code_challenge(verifier: str) -> str:
    return create_s256_code_challenge(verifier)


async def _get_json(url: str) -> dict[str, Any]:
    async with http_client() as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        raise invalid("idp_unreachable")
    data = resp.json()
    if not isinstance(data, dict):
        raise invalid("idp_bad_response")
    return data


async def discovery(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return the provider metadata: explicit endpoints in ``config`` win over the cached discovery doc."""
    issuer = str(config["issuer"]).rstrip("/")
    explicit = {
        k: config[k] for k in ("authorization_endpoint", "token_endpoint", "jwks_uri") if config.get(k)
    }
    if len(explicit) == 3:
        return {"issuer": issuer, **explicit}
    cached = _discovery_cache.get(issuer)
    if cached is None or time.monotonic() - cached[0] > DISCOVERY_TTL_SECONDS:
        doc = await _get_json(f"{issuer}/.well-known/openid-configuration")
        _discovery_cache[issuer] = (time.monotonic(), doc)
        cached = _discovery_cache[issuer]
    return {**cached[1], **explicit}


def authorize_url(
    meta: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    redirect_uri: str,
    state: str,
    nonce: str,
    verifier: str,
) -> str:
    scopes = config.get("scopes") or ["openid", "email", "profile"]
    if "openid" not in scopes:
        scopes = ["openid", *scopes]
    query = {
        "response_type": "code",
        "client_id": config["client_id"],
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{meta['authorization_endpoint']}?{urlencode(query)}"


async def _jwks(uri: str, *, refresh: bool = False) -> KeySet:
    cached = _jwks_cache.get(uri)
    if refresh or cached is None or time.monotonic() - cached[0] > DISCOVERY_TTL_SECONDS:
        _jwks_cache[uri] = (time.monotonic(), await _get_json(uri))
        cached = _jwks_cache[uri]
    return KeySet.import_key_set(cached[1])  # type: ignore[arg-type]


async def exchange_code(
    meta: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    client_secret: str | None,
    code: str,
    verifier: str,
    redirect_uri: str,
    nonce: str,
) -> dict[str, Any]:
    """Code -> tokens; returns the validated ID token claims (iss, aud, exp, nonce checked)."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": config["client_id"],
        "code_verifier": verifier,
    }
    if client_secret:
        form["client_secret"] = client_secret
    async with http_client() as client:
        resp = await client.post(
            str(meta["token_endpoint"]), data=form, headers={"Accept": "application/json"}
        )
    if resp.status_code != 200:
        raise invalid("token_exchange_failed")
    id_token = resp.json().get("id_token")
    if not isinstance(id_token, str):
        raise invalid("id_token_missing")
    return await verify_id_token(meta, config, id_token, nonce=nonce)


async def verify_id_token(
    meta: Mapping[str, Any], config: Mapping[str, Any], id_token: str, *, nonce: str
) -> dict[str, Any]:
    jwks_uri = str(meta["jwks_uri"])
    try:
        try:
            token = jwt.decode(id_token, await _jwks(jwks_uri), algorithms=["RS256", "ES256", "PS256"])
        except (JoseError, ValueError):  # key rotation: refresh the JWKS once
            token = jwt.decode(
                id_token, await _jwks(jwks_uri, refresh=True), algorithms=["RS256", "ES256", "PS256"]
            )
    except (JoseError, ValueError) as exc:
        raise invalid("signature") from exc
    claims = dict(token.claims)
    issuer = str(meta.get("issuer") or config["issuer"]).rstrip("/")
    if str(claims.get("iss", "")).rstrip("/") != issuer:
        raise invalid("issuer")
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if config["client_id"] not in audiences:
        raise invalid("audience")
    now = time.time()
    exp = claims.get("exp")
    if not isinstance(exp, int | float) or exp < now - 60:
        raise invalid("expired")
    nbf = claims.get("nbf")
    if isinstance(nbf, int | float) and nbf > now + 60:
        raise invalid("not_yet_valid")
    if claims.get("nonce") != nonce:
        raise invalid("nonce")
    if not claims.get("sub"):
        raise invalid("subject")
    return claims
