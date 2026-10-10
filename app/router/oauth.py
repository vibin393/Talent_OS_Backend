# ruff: noqa: E501
"""OAuth 2.1 protocol endpoints (Epic 67, spec 0.2-0.5): ``/api/oauth/*`` (served as ``/oauth/*`` through the Next.js
rewrite) and ``/.well-known/oauth-authorization-server``. Not under ``/api/v1``.

* ``token`` / ``revoke`` / ``introspect``: client authentication by ``client_secret_basic`` or ``client_secret_post``
  (public clients send ``client_id`` only); a ``client_secret`` in the query string is refused. Errors use the RFC 6749
  JSON shape with ``Cache-Control: no-store`` (not the P1 envelope).
* ``authorize/view`` and ``authorize/decision`` need the user session (the decision also CSRF); both send
  ``frame-ancestors 'none'`` / ``X-Frame-Options: DENY``.
"""

import base64
import binascii
from typing import Any
from urllib.parse import unquote_plus

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core.context import get_context
from app.core.errors import DomainError
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.oauth import AuthorizeDecision, AuthorizeRedirect, ClientAuth, TokenRequest
from app.services import oauth_service

router = APIRouter(tags=["oauth"])
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
FRAME = {"Content-Security-Policy": "frame-ancestors 'none'", "X-Frame-Options": "DENY", "Cache-Control": "no-store"}
GRANTS = ("authorization_code", "client_credentials", "refresh_token")


def _error(exc: oauth_service.OAuthError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status, headers={**NO_STORE, **exc.headers})


def _client(request: Request, form: dict[str, str]) -> ClientAuth:
    if "client_secret" in request.query_params:
        raise oauth_service.OAuthError(
            "invalid_request", "Send client credentials in the Authorization header or the form body"
        )
    header = request.headers.get("authorization") or ""
    if header[:6].lower() == "basic ":
        if form.get("client_secret"):
            raise oauth_service.OAuthError("invalid_request", "Use one client authentication method")
        try:
            raw = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise oauth_service.OAuthError("invalid_client", "Malformed Basic credentials", status=401) from exc
        cid, sep, secret = raw.partition(":")
        if not sep:
            raise oauth_service.OAuthError("invalid_client", "Malformed Basic credentials", status=401)
        return ClientAuth(client_id=unquote_plus(cid), client_secret=unquote_plus(secret) or None)
    return ClientAuth(client_id=form.get("client_id") or "", client_secret=form.get("client_secret") or None)


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


async def _guard(call: Any) -> Response:
    try:
        return await call()
    except oauth_service.OAuthError as exc:
        return _error(exc)
    except DomainError as exc:
        if exc.status_code == 503 or exc.code == "temporarily_unavailable":
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=503, headers={**NO_STORE, "Retry-After": "30"})
        raise


@router.post("/api/oauth/token")
async def token(request: Request) -> Response:
    async def run() -> Response:
        form = await _form(request)
        grant = form.get("grant_type") or ""
        if grant not in GRANTS:
            raise oauth_service.OAuthError("unsupported_grant_type", "Supported: authorization_code, refresh_token, client_credentials")
        client = _client(request, form)
        try:
            body = TokenRequest.model_validate({k: v for k, v in form.items() if k in TokenRequest.model_fields})
        except ValidationError as exc:
            raise oauth_service.OAuthError("invalid_request", "Malformed token request") from exc
        out = await oauth_service.token(body, client)
        return JSONResponse(out.model_dump(exclude_none=True), headers=NO_STORE)

    return await _guard(run)


@router.post("/api/oauth/revoke")
async def revoke(request: Request) -> Response:
    async def run() -> Response:
        form = await _form(request)
        await oauth_service.revoke_token(form.get("token") or "", _client(request, form))
        return JSONResponse({}, headers=NO_STORE)

    return await _guard(run)


@router.post("/api/oauth/introspect")
async def introspect(request: Request) -> Response:
    async def run() -> Response:
        form = await _form(request)
        out = await oauth_service.introspect(form.get("token") or "", _client(request, form))
        return JSONResponse(out.model_dump(exclude_none=True), headers=NO_STORE)

    return await _guard(run)


@router.get("/api/oauth/authorize/view")
async def authorize_view(request: Request, db: TenantDBSessionDep) -> Response:
    query = dict(request.query_params)
    try:
        checked = await oauth_service.check_query(query)
        if isinstance(checked, AuthorizeRedirect):
            return JSONResponse(checked.model_dump(), headers=FRAME)
        view = await oauth_service.authorize_request(get_context(), db, checked)
    except oauth_service.OAuthRedirectError as exc:
        return JSONResponse({"redirect_to": exc.redirect_to}, headers=FRAME)
    except DomainError as exc:
        exc.response_hooks.append(lambda r: r.headers.update(FRAME))  # type: ignore[arg-type, union-attr]
        raise
    return JSONResponse(view.model_dump(mode="json"), headers=FRAME)


@router.post("/api/oauth/authorize/decision", response_model=AuthorizeRedirect)
async def authorize_decision(body: AuthorizeDecision, response: Response, db: TenantDBSessionDep) -> AuthorizeRedirect:
    response.headers.update(FRAME)
    return await oauth_service.authorize_decision(get_context(), db, body)


@router.get("/.well-known/oauth-authorization-server")
async def metadata() -> Response:
    return JSONResponse(oauth_service.metadata(), headers={"Cache-Control": "public, max-age=3600"})
