"""Domain errors raised by services, and FastAPI handlers rendering the AD-12 error envelope.

Services raise these; they never raise ``HTTPException``.
Envelope: ``{"error": {"code": "snake_code", "message": "...", "details": {...}}}``.
"""

from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_context

if TYPE_CHECKING:
    from collections.abc import Callable


class DomainError(Exception):
    status_code: int = 400
    code: str = "bad_request"
    default_message: str = "Bad request"

    def __init__(
        self, message: str | None = None, *, code: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        self.message = message or self.default_message
        if code is not None:
            self.code = code
        self.details: dict[str, Any] = details or {}
        # Callbacks applied to the error response, e.g. clearing an invalid session cookie.
        self.response_hooks: list[Callable[[Response], None]] = []
        super().__init__(self.message)


class Unauthorized(DomainError):  # ruff: ignore[error-suffix-on-exception-name] - public names fixed by the architecture
    status_code = 401
    code = "unauthorized"
    default_message = "Authentication required"


class Forbidden(DomainError):  # ruff: ignore[error-suffix-on-exception-name] - public names fixed by the architecture
    """403. Epic 14 (FR-82): optional ``action`` (human label), ``required_permission`` and ``ask`` (who can grant
    it: ``tenant_admin`` for Users, ``account_manager`` for portal principals) go into ``details``; when
    ``ask`` is omitted the error handler derives it from the calling principal."""

    status_code = 403
    code = "forbidden"
    default_message = "You do not have permission to perform this action"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        action: str | None = None,
        required_permission: str | None = None,
        ask: str | None = None,
    ) -> None:
        merged = dict(details or {})
        if action is not None:
            merged["action"] = action
        if required_permission is not None:
            merged["required_permission"] = required_permission
        if ask is not None:
            merged["ask"] = ask
        super().__init__(message, code=code, details=merged)


# Principal types whose access is granted by the client's / vendor's Account Manager (FR-82).
_PORTAL_PRINCIPALS = frozenset({"client_user", "vendor_user", "candidate"})
# 403 codes that are not "you lack a permission" (no Access Denied details).
_NO_ASK_CODES = frozenset({"csrf_failed", "mfa_enrollment_required", "tenant_suspended", "tenant_offboarding"})


def _with_ask(exc: "DomainError") -> dict[str, Any]:
    details = exc.details
    if not isinstance(exc, Forbidden) or "ask" in details or exc.code in _NO_ASK_CODES:
        return details
    principal = getattr(get_context(), "principal", None)
    kind = getattr(principal, "type", None)
    return {**details, "ask": "account_manager" if kind in _PORTAL_PRINCIPALS else "tenant_admin"}


class NotFound(DomainError):  # ruff: ignore[error-suffix-on-exception-name] - public names fixed by the architecture
    status_code = 404
    code = "not_found"
    default_message = "Resource not found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"
    default_message = "The request conflicts with the current state of the resource"


class Gone(DomainError):  # ruff: ignore[error-suffix-on-exception-name] - mirrors HTTP 410
    """A single-use / expiring link (invitation, password reset) that can no longer be used."""

    status_code = 410
    code = "gone"
    default_message = "This link is no longer valid"


class ValidationFailed(DomainError):  # ruff: ignore[error-suffix-on-exception-name] - public names fixed by the architecture
    status_code = 422
    code = "validation_failed"
    default_message = "Validation failed"


class RateLimited(DomainError):  # ruff: ignore[error-suffix-on-exception-name] - public names fixed by the architecture
    status_code = 429
    code = "rate_limited"
    default_message = "Too many requests"


def error_body(code: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=jsonable_encoder(error_body(code, message, details)), headers=headers
    )


def _status_code_name(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).name.lower()
    except ValueError:
        return "http_error"


async def _domain_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DomainError)
    response = error_response(exc.status_code, exc.code, exc.message, _with_ask(exc))
    for hook in exc.response_hooks:
        try:
            hook(response)
        except Exception:
            logger.exception("Error response hook failed (request_id={})", get_context().request_id)
    return response


async def _validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    fields = [
        {
            "field": ".".join(str(part) for part in err.get("loc", ())),
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in exc.errors()
    ]
    return error_response(422, "validation_failed", "Request validation failed", {"fields": fields})


async def _http_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    is_str = isinstance(exc.detail, str)
    message = exc.detail if is_str else _status_code_name(exc.status_code)
    details = None if is_str or exc.detail is None else {"detail": exc.detail}
    return error_response(
        exc.status_code,
        _status_code_name(exc.status_code),
        message,
        details,
        headers=getattr(exc, "headers", None),
    )


def internal_error_response() -> JSONResponse:
    return error_response(500, "internal_error", "An unexpected error occurred")


async def _unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    # Fallback only: RequestContextMiddleware normally handles unhandled errors first.
    logger.opt(exception=exc).error("Unhandled error (request_id={})", get_context().request_id)
    return internal_error_response()


async def _db_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Epic 13: database guard errors (period lock, immutability, overlap) become 409s; others are 500s."""
    from app.core.db_errors import translate

    domain = translate(exc)
    if domain is not None:
        return await _domain_error_handler(request, domain)
    return await _unhandled_error_handler(request, exc)


def register_exception_handlers(app: FastAPI) -> None:
    from sqlalchemy.exc import DBAPIError

    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(DBAPIError, _db_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)


# ================================================================ field errors (Epic 13)
import dataclasses  # ruff: ignore[module-import-not-at-top-of-file]


@dataclasses.dataclass(frozen=True, slots=True)
class FieldError:
    """One invalid field: ``code`` is the machine code (e.g. ``gstin_checksum``)."""

    field: str
    code: str
    message: str


def field_errors_failed(errors: "list[FieldError]", message: str = "Validation failed") -> ValidationFailed:
    """422 whose ``code`` is the first error's code and ``details.fields`` lists every error."""
    first = errors[0]
    return ValidationFailed(
        message if message != "Validation failed" else first.message,
        code=first.code,
        details={"fields": [{"field": e.field, "code": e.code, "message": e.message} for e in errors]},
    )
