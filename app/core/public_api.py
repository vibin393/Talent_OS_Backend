# ruff: file-ignore[line-too-long, non-imperative-mood, error-suffix-on-exception-name]
"""Public API conventions (Epic 66, AD-77, FR-333/FR-347; spec 0.7). Pure core: imports nothing from ``app.services``.

* Served to callers at ``/public/v1/...`` (Next.js rewrite) and mounted at ``PUBLIC_PREFIX`` = ``/api/public/v1``.
* ``public_router(resource)`` routers are tagged ``public:<resource>`` and kept out of the internal OpenAPI document; the
  public document (``public_openapi``) is generated only from them.
* ``require_scope`` authorises API keys and App Principals (``dependencies/public.py`` registers the resolver).
* Cursor pagination with HMAC-signed opaque cursors (tampering -> 400 ``invalid_cursor``), keyset paging, ETag /
  ``If-Match`` (412 / 428), deprecation headers, and ``mask_for_scopes`` (P2 field groups unmasked only by ``pii.*``).
"""

import base64
import copy
import hashlib
import hmac
import json
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy import Select, tuple_

from app.core.errors import DomainError, Forbidden, ValidationFailed
from app.core.oauth_scopes import field_groups_for, scopes
from app.core.permissions import Principal, Scope, check
from app.core.settings import get_settings

PUBLIC_PREFIX = "/api/public/v1"
API_VERSION = "v1"
PUBLIC_TAG_PREFIX = "public:"
PUBLIC_PRINCIPAL_TYPES = frozenset({"api_key", "app"})


# ================================================================ errors
class PreconditionFailed(DomainError):
    status_code = 412
    code = "precondition_failed"
    default_message = "The resource changed since you read it"


class PreconditionRequired(DomainError):
    status_code = 428
    code = "precondition_required"
    default_message = "An If-Match header is required"


def insufficient_scope(required: Iterable[str]) -> Forbidden:
    return Forbidden(
        "The token does not hold the required scope",
        code="insufficient_scope",
        details={"required_scopes": sorted(required)},
    )


def invalid_cursor() -> ValidationFailed:
    exc = ValidationFailed("The cursor is invalid", code="invalid_cursor")
    exc.status_code = 400
    return exc


# ================================================================ routers
def public_router(resource: str) -> APIRouter:
    """A Public API router: tag ``public:<resource>``; excluded from the internal OpenAPI document."""
    return APIRouter(tags=[f"{PUBLIC_TAG_PREFIX}{resource}"], include_in_schema=False)


def is_public_route(route: Any) -> bool:
    return isinstance(route, APIRoute) and any(
        str(t).startswith(PUBLIC_TAG_PREFIX) for t in (route.tags or [])
    )


def public_openapi(app: FastAPI) -> dict[str, Any]:
    """The public OpenAPI document: only ``public:*`` routes (FR-333)."""
    routes = []
    for route in app.routes:
        if is_public_route(route):
            clone = copy.copy(route)
            clone.include_in_schema = True
            routes.append(clone)
    return get_openapi(
        title="Talent OS Public API",
        version=API_VERSION,
        description="Talent OS Public API v1. Additive changes only within v1.",
        routes=routes,
        servers=[{"url": get_settings().p5_public_api_base_url().rsplit("/public/v1", 1)[0] or "/"}],
    )


# ================================================================ principal + scopes
PublicResolver = Callable[[Request], Awaitable[Principal]]
_resolver: PublicResolver | None = None


def set_public_principal_resolver(resolver: PublicResolver) -> None:
    global _resolver  # process-wide wiring (dependencies/public.py)
    _resolver = resolver


async def current_public_principal(request: Request) -> Principal:
    if _resolver is None:  # pragma: no cover - wiring error
        msg = "no public principal resolver registered (import app.dependencies.public)"
        raise RuntimeError(msg)
    return await _resolver(request)


def scope_satisfied(principal: Principal, scope_key: str) -> bool:
    """App: the token's effective scopes hold ``scope_key``. API key: the key holds every permission the scope maps and
    the scope is ``api_key_allowed`` (FR-348)."""
    if principal.type == "app":
        return scope_key in principal.oauth_scopes
    if principal.type == "api_key":
        spec = scopes().get(scope_key)
        if spec is None or not spec.api_key_allowed:
            return False
        return all(p in principal.permissions for p in spec.permissions)
    return False


def require_scope(*scope_keys: str, at_least: Scope = Scope.OWN) -> Callable[..., Awaitable[Principal]]:
    """Dependency: an API key or App Principal holding EVERY ``scope_keys``; then the mapped permissions are checked
    with ``core.permissions.check`` (403 ``forbidden`` when a delegated user lost the permission)."""

    async def dependency(principal: Annotated[Principal, Depends(current_public_principal)]) -> Principal:
        if principal.type not in PUBLIC_PRINCIPAL_TYPES:
            raise insufficient_scope(scope_keys)
        missing = [k for k in scope_keys if not scope_satisfied(principal, k)]
        if missing:
            raise insufficient_scope(missing)
        for key in scope_keys:
            spec = scopes().get(key)
            for perm in spec.permissions if spec is not None else ():
                check(principal, perm, at_least)
        return principal

    dependency.__name__ = "require_scope_" + "_".join(
        k.replace(":", "_").replace(".", "_") for k in scope_keys
    )
    dependency.required_scopes = tuple(scope_keys)  # type: ignore[attr-defined]  # Epic 67: the contract pins them
    return dependency


# ================================================================ cursors / keyset
class CursorParams(BaseModel):
    limit: int = Field(50, ge=1, le=200)
    cursor: str | None = None


class PublicPage[T](BaseModel):
    data: list[T]
    next_cursor: str | None = None
    has_more: bool = False


def _cursor_key() -> bytes:
    s = get_settings()
    if s.public_cursor_secret:
        return s.public_cursor_secret.encode("utf-8")
    from app.core.security import dev_fernet_key

    return hashlib.sha256(dev_fernet_key("public_cursor").encode("ascii")).digest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return {"$u": str(value)}
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, date):
        return {"$d": value.isoformat()}
    return value


def _unjson(value: Any) -> Any:
    if isinstance(value, dict) and len(value) == 1:
        ((k, v),) = value.items()
        if k == "$u":
            return uuid.UUID(v)
        if k == "$dt":
            return datetime.fromisoformat(v)
        if k == "$d":
            return date.fromisoformat(v)
    return value


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode_cursor(sort_values: tuple[Any, ...], id: uuid.UUID) -> str:
    body = json.dumps(
        {"s": [_jsonable(v) for v in sort_values], "i": str(id)}, separators=(",", ":"), sort_keys=True
    )
    raw = body.encode("utf-8")
    mac = hmac.new(_cursor_key(), raw, hashlib.sha256).digest()[:16]
    return f"{_b64(raw)}.{_b64(mac)}"


def decode_cursor(cursor: str) -> tuple[tuple[Any, ...], uuid.UUID]:
    try:
        body_b64, mac_b64 = cursor.split(".", 1)
        raw = _unb64(body_b64)
        expected = hmac.new(_cursor_key(), raw, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(expected, _unb64(mac_b64)):
            raise invalid_cursor()
        payload = json.loads(raw)
        return tuple(_unjson(v) for v in payload["s"]), uuid.UUID(payload["i"])
    except DomainError:
        raise
    except Exception as exc:
        raise invalid_cursor() from exc


def keyset(stmt: Select[Any], sort_cols: tuple[Any, ...], id_col: Any, cursor: str | None) -> Select[Any]:
    """``(sort..., id) > cursor`` in ascending order; stable under inserts."""
    if cursor:
        values, last_id = decode_cursor(cursor)
        if len(values) != len(sort_cols):
            raise invalid_cursor()
        if sort_cols:
            stmt = stmt.where(tuple_(*sort_cols, id_col) > tuple_(*values, last_id))
        else:
            stmt = stmt.where(id_col > last_id)
    return stmt.order_by(*sort_cols, id_col)


def page_of[T](
    rows: list[T], limit: int, key: Callable[[T], tuple[tuple[Any, ...], uuid.UUID]]
) -> PublicPage[T]:
    """Build a page from ``limit + 1`` fetched rows (the extra row only signals ``has_more``)."""
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = encode_cursor(*key(rows[-1])) if has_more and rows else None
    return PublicPage[T](data=rows, next_cursor=next_cursor, has_more=has_more)


# ================================================================ ETag / If-Match
def etag_for(version: int | str) -> str:
    return f'W/"{version}"'


def check_if_match(request: Request, current_version: int | str) -> None:
    header = request.headers.get("if-match")
    if header is None:
        if request.method.upper() in ("PATCH", "PUT"):
            raise PreconditionRequired()
        return
    wanted = {h.strip() for h in header.split(",")}
    if "*" in wanted:
        return
    current = etag_for(current_version)
    if current not in wanted and current.removeprefix("W/") not in wanted:
        raise PreconditionFailed()


# ================================================================ deprecation
def deprecate(*, since: date, sunset: date, link: str) -> Callable[..., Awaitable[None]]:
    """Dependency adding ``Deprecation`` / ``Sunset`` / ``Link`` headers; marks the request log row deprecated."""

    async def dependency(request: Request, response: Response) -> None:
        response.headers["Deprecation"] = f"@{int(datetime(since.year, since.month, since.day).timestamp())}"
        response.headers["Sunset"] = sunset.strftime("%a, %d %b %Y 00:00:00 GMT")
        response.headers["Link"] = f'<{link}>; rel="deprecation"'
        request.state.tos_deprecated = True

    return dependency


# ================================================================ masking
FIELD_GROUPS: Mapping[str, frozenset[str]] = {
    "contact": frozenset(
        {
            "email",
            "emails",
            "phone",
            "phones",
            "mobile",
            "phone_e164",
            "alternate_phone",
            "address",
            "address_line1",
            "address_line2",
        }
    ),
    "identity": frozenset(
        {
            "pan",
            "pan_number",
            "aadhaar",
            "aadhaar_ref",
            "aadhaar_last4",
            "passport",
            "passport_number",
            "dob",
            "date_of_birth",
            "uan",
            "bank_account",
            "bank_account_number",
            "account_number",
            "ifsc",
        }
    ),
    "financial": frozenset(
        {
            "salary",
            "current_ctc",
            "expected_ctc",
            "ctc",
            "compensation",
            "bill_rate",
            "pay_rate",
            "vendor_rate",
            "margin",
            "margin_pct",
            "rate",
            "bank_account",
            "bank_account_number",
            "account_number",
            "ifsc",
        }
    ),
    "recordings": frozenset(
        {"recording_url", "recording_file_id", "transcript", "transcript_text", "recording"}
    ),
    "documents": frozenset({"document_url", "download_url", "file_url"}),
}
MASK = None


def masked_fields(principal: Principal) -> frozenset[str]:
    """Field names hidden from ``principal``: every group not unlocked by a held ``pii.*`` / sensitive scope. API keys
    hold no sensitive scope (FR-348), so they see every group masked."""
    held = field_groups_for(principal.oauth_scopes) if principal.type == "app" else frozenset()
    out: set[str] = set()
    for group, names in FIELD_GROUPS.items():
        if group not in held:
            out.update(names)
    # A field shared by two groups is visible only when every group holding it is unlocked.
    return frozenset(out)


def mask_for_scopes(data: Any, principal: Principal) -> Any:
    """Recursively null the fields of locked groups (on top of the user's own masking)."""
    hidden = masked_fields(principal)

    def walk(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return walk(value.model_dump(mode="json"))
        if isinstance(value, Mapping):
            return {k: (MASK if k in hidden and v is not None else walk(v)) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [walk(v) for v in value]
        return value

    return walk(data)


__all__ = [
    "API_VERSION",
    "FIELD_GROUPS",
    "PUBLIC_PREFIX",
    "CursorParams",
    "PreconditionFailed",
    "PreconditionRequired",
    "PublicPage",
    "check_if_match",
    "current_public_principal",
    "decode_cursor",
    "deprecate",
    "encode_cursor",
    "etag_for",
    "insufficient_scope",
    "invalid_cursor",
    "is_public_route",
    "keyset",
    "mask_for_scopes",
    "masked_fields",
    "page_of",
    "public_openapi",
    "public_router",
    "require_scope",
    "scope_satisfied",
    "set_public_principal_resolver",
]
