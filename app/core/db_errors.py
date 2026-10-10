"""Translate database guard errors into domain errors (AD-21, AD-22).

The generic triggers installed by ``core.migration_helpers`` raise:

* ``TOS01`` ``period_closed:<legal_entity_id>:<yyyy-mm>`` -> 409 ``period_closed``;
* ``TOS02`` ``document_immutable:<table>`` -> 409 ``document_immutable``;
* ``23P01`` (exclusion violation on a named EXCLUDE constraint) -> 409 ``overlap``.

The app-level exception handler (``core.errors.register_exception_handlers``) calls ``translate``; services
may call it directly to raise early. Anything else returns ``None`` (unhandled -> 500).
"""

from typing import Any

from sqlalchemy.exc import DBAPIError

from app.core.errors import ConflictError, DomainError

PERIOD_CLOSED = "TOS01"
DOCUMENT_IMMUTABLE = "TOS02"
EXCLUSION_VIOLATION = "23P01"


def _chain(exc: BaseException) -> list[BaseException]:
    out: list[BaseException] = []
    todo: list[BaseException | None] = [exc]
    while todo:
        cur = todo.pop()
        if cur is None or any(cur is seen for seen in out):
            continue
        out.append(cur)
        todo.extend([getattr(cur, "orig", None), cur.__cause__, cur.__context__])
    return out


def _sqlstate_and_message(exc: BaseException) -> tuple[str | None, str, str | None]:
    for cur in _chain(exc):
        code = getattr(cur, "sqlstate", None) or getattr(cur, "pgcode", None)
        if isinstance(code, str) and code:
            message = getattr(cur, "message", None) or str(cur)
            return code, str(message), getattr(cur, "constraint_name", None)
    return None, str(exc), None


def _after(message: str, prefix: str) -> list[str]:
    idx = message.find(prefix)
    if idx < 0:
        return []
    tail = message[idx + len(prefix) :].strip().splitlines()[0] if message[idx + len(prefix) :].strip() else ""
    return tail.split(":")


def translate(exc: DBAPIError | BaseException) -> DomainError | None:
    code, message, constraint = _sqlstate_and_message(exc)
    if code == PERIOD_CLOSED:
        parts = _after(message, "period_closed:")
        details: dict[str, Any] = {}
        if len(parts) >= 2:
            details = {"legal_entity_id": parts[0], "period": parts[1]}
        return ConflictError("The accounting period is closed", code="period_closed", details=details)
    if code == DOCUMENT_IMMUTABLE:
        parts = _after(message, "document_immutable:")
        return ConflictError(
            "This record can no longer be changed",
            code="document_immutable",
            details={"table": parts[0]} if parts else {},
        )
    if code == EXCLUSION_VIOLATION:
        return ConflictError(
            "The period overlaps an existing one",
            code="overlap",
            details={"constraint": constraint} if constraint else {},
        )
    return None
