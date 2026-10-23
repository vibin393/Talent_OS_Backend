"""Per-kind row handlers: map a CSV row onto the owner domain's public function (Epic 35 A8 contract).

Each handler validates (``validate_only=True``) or applies one row and returns a ``result_ref``. Problems are
raised as ``RowErrors``; ``WorkerPending`` marks a row whose Worker does not exist yet (a dependency).
Owner functions are resolved lazily so a kind whose owner is missing or a stub reports ``unavailable``.
"""

import inspect
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
ACCOUNT_RE = re.compile(r"^[0-9]{6,20}$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")

ErrorItem = dict[str, Any]  # {column, code, message}


class RowErrors(Exception):  # ruff: ignore[error-suffix-on-exception-name]
    def __init__(self, errors: list[ErrorItem]) -> None:
        super().__init__("row errors")
        self.errors = errors


class WorkerPending(Exception):  # ruff: ignore[error-suffix-on-exception-name]
    """The referenced Worker (by email) does not exist yet: apply needs a ``workers`` batch first."""


class DependencyPending(WorkerPending):
    """Epic 52: a row references a record of a ``depends_on`` kind that is not imported yet (e.g. a Project Role
    before its Project). Preview treats it like ``WorkerPending``; apply refuses with 422 ``dependency_missing``."""

    def __init__(self, requires: str, column: str) -> None:
        super().__init__(f"{requires} missing")
        self.requires = requires
        self.column = column


def pending_errors(exc: WorkerPending) -> list["ErrorItem"]:
    if isinstance(exc, DependencyPending):
        return [err(exc.column, "dependency_missing", f"Import {exc.requires.replace('_', ' ')} first")]
    return [err("worker_email", "worker_not_found", "The Worker does not exist")]


def err(column: str | None, code: str, message: str) -> ErrorItem:
    return {"column": column, "code": code, "message": message}


# ------------------------------------------------------------------ owner resolution
def _module_fn(facade: str, fallback: str, name: str) -> Callable[..., Awaitable[Any]] | None:
    import importlib

    for path in (facade, fallback):
        try:
            mod = importlib.import_module(path)
        except ImportError:
            continue
        fn = getattr(mod, name, None)
        if fn is not None:
            return fn  # type: ignore[no-any-return]
    return None


# Epic 52: additional kinds (Phase 3, ``golive.p3_imports``): kind -> (facade, fallback module, function name).
EXTRA_OWNERS: dict[str, tuple[str, str, str]] = {}


def owner(kind: str) -> Callable[..., Awaitable[Any]] | None:
    if kind in EXTRA_OWNERS:
        return _module_fn(*EXTRA_OWNERS[kind])
    if kind == "workers":
        return _module_fn(
            "app.services.candidates", "app.services.candidates.golive_import", "find_or_create_for_import"
        )
    if kind == "placements":
        return _module_fn(
            "app.services.placements_service", "app.services.placements.service", "create_direct"
        )
    if kind == "opening_invoices":
        return _module_fn(
            "app.services.billing_service", "app.services.billing.service", "import_opening_invoice"
        )
    if kind == "leave_balances":
        return _module_fn(
            "app.services.time_service", "app.services.time.leave_balances", "import_opening_leave_balance"
        )
    return None


def is_stub(fn: Callable[..., Any]) -> bool:
    """Contract stubs only ``raise NotImplementedError`` (or carry ``__stub__``)."""
    if getattr(fn, "__stub__", False) or getattr(fn, "__contract_stub__", False):
        return True
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError):
        return False
    body = [
        ln.strip()
        for ln in src.splitlines()
        if ln.strip() and not ln.strip().startswith(("#", "@", "def ", "async def ", '"""', "'''"))
    ]
    return "raise NotImplementedError" in src and len(body) <= 6


def available(kind: str) -> bool:
    fn = owner(kind)
    return fn is not None and not is_stub(fn)


# ------------------------------------------------------------------ error mapping
def from_domain(exc: DomainError, columns: dict[str, str] | None = None) -> list[ErrorItem]:
    columns = columns or {}
    details = exc.details or {}
    column: str | None = None
    fields = details.get("fields")
    if isinstance(fields, list) and fields and isinstance(fields[0], dict):
        column = fields[0].get("field")
    elif isinstance(fields, dict) and fields:
        column = next(iter(fields))
    elif isinstance(details.get("field"), str):
        column = details["field"]
    if column is not None:
        column = columns.get(column, column)
    return [err(column, exc.code, exc.message)]


def from_pydantic(exc: ValidationError, columns: dict[str, str] | None = None) -> list[ErrorItem]:
    columns = columns or {}
    out: list[ErrorItem] = []
    for e in exc.errors():
        field = str(e["loc"][0]) if e.get("loc") else None
        out.append(
            err(columns.get(field, field) if field else None, "invalid", str(e.get("msg", "Invalid value")))
        )
    return out


def from_any(exc: Exception, columns: dict[str, str] | None = None) -> list[ErrorItem]:
    if isinstance(exc, RowErrors):
        return exc.errors
    if isinstance(exc, DomainError):
        return from_domain(exc, columns)
    if isinstance(exc, ValidationError):
        return from_pydantic(exc, columns)
    from app.core import db_errors

    translated = db_errors.translate(exc)
    if translated is not None:  # period guard (period_closed), immutability, overlap
        return from_domain(translated, columns)
    if isinstance(exc, IntegrityError):
        return [err(None, "invalid_reference", "A referenced record does not exist or conflicts")]
    # Unexpected: log the type only (messages can carry row values).
    logger.warning("go-live row failed with {}", type(exc).__name__)
    return [err(None, "apply_failed", "The row could not be applied")]


# ------------------------------------------------------------------ field parsing
class _Fields:
    def __init__(self, row: dict[str, str]) -> None:
        self.row = row
        self.errors: list[ErrorItem] = []

    def s(self, col: str) -> str | None:
        return (self.row.get(col) or "").strip() or None

    def uuid(self, col: str) -> uuid.UUID | None:
        v = self.s(col)
        if v is None:
            return None
        try:
            return uuid.UUID(v)
        except ValueError:
            self.errors.append(err(col, "invalid_id", "Must be an id"))
            return None

    def date(self, col: str) -> date | None:
        v = self.s(col)
        if v is None:
            return None
        try:
            return date.fromisoformat(v) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else _bad_date()
        except ValueError:
            self.errors.append(err(col, "invalid_date", "Use yyyy-mm-dd"))
            return None

    def dec(self, col: str) -> Decimal | None:
        v = self.s(col)
        if v is None:
            return None
        try:
            d = Decimal(v.replace(",", ""))
        except InvalidOperation:
            d = None
        if d is None or not d.is_finite():
            self.errors.append(err(col, "invalid_decimal", "Must be a decimal number"))
            return None
        return d

    def need(self, col: str, value: Any) -> None:
        if value is None and not any(e["column"] == col for e in self.errors):
            self.errors.append(err(col, "required", "This column is required"))

    def check(self) -> None:
        if self.errors:
            raise RowErrors(self.errors)


def _bad_date() -> date:
    raise ValueError


async def default_legal_entity(db: AsyncSession) -> uuid.UUID | None:
    from app.models.legal_entities import LegalEntity

    return (
        await db.execute(
            select(LegalEntity.id)
            .where(LegalEntity.status == "active")
            .order_by(LegalEntity.is_backfilled.desc(), LegalEntity.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()


async def _legal_entity(db: AsyncSession, f: _Fields) -> uuid.UUID | None:
    le = f.uuid("legal_entity_id")
    if le is None and f.s("legal_entity_id") is None:
        le = await default_legal_entity(db)
        if le is None:
            f.errors.append(
                err("legal_entity_id", "legal_entity_missing", "The tenant has no active legal entity")
            )
    return le


async def worker_id(ctx: RequestContext, db: AsyncSession, email: str) -> uuid.UUID | None:
    fn = owner("workers")
    if fn is None:
        return None
    try:
        found = await fn(ctx, db, {"email": email, "full_name": "-"}, validate_only=True)
    except DomainError:
        return None
    cid = found.get("candidate_id") if isinstance(found, dict) else None
    return uuid.UUID(str(cid)) if cid else None


# ------------------------------------------------------------------ handlers
Ctx = dict[str, Any]  # per-run scratch (in-file duplicate detection)


async def workers(
    ctx: RequestContext,
    db: AsyncSession,
    row: dict[str, str],
    batch_id: uuid.UUID,  # ruff: ignore[unused-function-argument]
    *,
    validate_only: bool,
    scratch: Ctx,
) -> str | None:
    f = _Fields(row)
    pan = (f.s("pan") or "").upper().replace(" ", "")
    account = (f.s("bank_account_number") or "").replace(" ", "")
    ifsc = (f.s("ifsc") or "").upper()
    if pan and not PAN_RE.match(pan):
        f.errors.append(err("pan", "invalid_pan", "Enter a valid PAN (e.g. ABCDE1234F)"))
    if account and not ACCOUNT_RE.match(account):
        f.errors.append(err("bank_account_number", "invalid_account_number", "Enter a valid account number"))
    if account and not IFSC_RE.match(ifsc):
        f.errors.append(err("ifsc", "invalid_ifsc", "Enter a valid IFSC (e.g. HDFC0001234)"))
    key = (f.s("email") or f.s("phone") or "").lower()
    seen: set[str] = scratch.setdefault("keys", set())
    if key and key in seen:
        f.errors.append(err("email", "duplicate_in_file", "This Worker appears earlier in the file"))
    seen.add(key)
    fn = owner("workers")
    assert fn is not None
    try:
        found = await fn(ctx, db, row, validate_only=validate_only)
    except DomainError as exc:
        f.errors.extend(from_domain(exc))
        found = {}
    f.check()
    if validate_only:
        return None
    cid = uuid.UUID(str(found["candidate_id"]))
    from app.services import placements_service

    if pan:
        await placements_service.record_pan(ctx, db, cid, pan, None)
    if account:
        from app.schemas.placements_api import BankAccountIn

        holder = f.s("bank_holder_name") or f.s("full_name") or "-"
        data = BankAccountIn(
            holder_name=holder, account_number=account, account_number_confirm=account, ifsc=ifsc
        )
        await placements_service.record_bank_details(ctx, db, cid, data, "import")
    return f"candidate:{cid}"


_PLACEMENT_COLUMNS = {
    "planned_start_date": "start_date",
    "actual_start_date": "start_date",
    "planned_end_date": "end_date",
    "candidate_id": "worker_email",
    "end_date": "end_date",
    "start_date": "start_date",
}


async def placements(
    ctx: RequestContext,
    db: AsyncSession,
    row: dict[str, str],
    batch_id: uuid.UUID,  # ruff: ignore[unused-function-argument]
    *,
    validate_only: bool,
    scratch: Ctx,  # ruff: ignore[unused-function-argument]
) -> str | None:
    from app.schemas.placements import PlacementCreateIn

    f = _Fields(row)
    email = f.s("worker_email")
    f.need("worker_email", email)
    client_id = f.uuid("client_id")
    f.need("client_id", client_id)
    start = f.date("start_date")
    f.need("start_date", start)
    end = f.date("end_date")
    rates = {k: f.dec(k) for k in ("pay_rate", "bill_rate", "vendor_rate", "burden_pct")}
    ids = {k: f.uuid(k) for k in ("vendor_id", "contract_id", "po_id")}
    le = await _legal_entity(db, f)
    f.check()
    assert email is not None
    cid = await worker_id(ctx, db, email)
    if cid is None:
        raise WorkerPending
    try:
        data = PlacementCreateIn(
            candidate_id=cid,
            client_id=client_id,
            legal_entity_id=le,
            worker_type=f.s("worker_type"),
            engagement_kind=f.s("engagement_kind") or "contract",
            planned_start_date=start,
            actual_start_date=start,
            planned_end_date=end,
            currency=(f.s("currency") or "INR").upper(),
            work_state_code=f.s("work_state_code"),
            rate_unit=f.s("rate_unit"),
            imported=True,
            validate_only=validate_only,
            **rates,
            **ids,
        )
    except ValidationError as exc:
        raise RowErrors(from_pydantic(exc, _PLACEMENT_COLUMNS)) from exc
    fn = owner("placements")
    assert fn is not None
    out = await fn(ctx, db, data)
    return None if validate_only else f"placement:{out.id}"


async def opening_invoices(
    ctx: RequestContext,
    db: AsyncSession,
    row: dict[str, str],
    batch_id: uuid.UUID,
    *,
    validate_only: bool,
    scratch: Ctx,
) -> str | None:
    from app.schemas.billing import OpeningInvoiceRow

    f = _Fields(row)
    number = f.s("number")
    seen: set[str] = scratch.setdefault("numbers", set())
    if number and number.upper() in seen:
        f.errors.append(err("number", "duplicate_in_file", "This invoice number appears earlier in the file"))
    if number:
        seen.add(number.upper())
    le = await _legal_entity(db, f)
    values: dict[str, Any] = {k: v for k, v in row.items() if v and k != "legal_entity_id"}
    values["legal_entity_id"] = le
    for col in ("doc_date", "due_date"):
        if f.s(col) is not None:
            values[col] = f.date(col)
    f.check()
    try:
        data = OpeningInvoiceRow(**values)
    except ValidationError as exc:
        raise RowErrors(from_pydantic(exc)) from exc
    fn = owner("opening_invoices")
    assert fn is not None
    out = await fn(ctx, db, data, batch_id, validate_only=validate_only)
    return None if validate_only else f"invoice:{out.id}"


_LEAVE_MESSAGES = (
    ("leave type", "leave_type_code", "unknown_leave_type"),
    ("days", "days", "invalid_days"),
    ("worker", "worker_email", "worker_not_found"),
)


async def leave_balances(
    ctx: RequestContext,
    db: AsyncSession,
    row: dict[str, str],
    batch_id: uuid.UUID,
    *,
    validate_only: bool,
    scratch: Ctx,
) -> str | None:
    f = _Fields(row)
    email = f.s("worker_email")
    f.need("worker_email", email)
    f.need("leave_type_code", f.s("leave_type_code"))
    f.need("days", f.s("days"))
    le = await _legal_entity(db, f)
    f.check()
    assert email is not None
    cid = await worker_id(ctx, db, email)
    if cid is None:
        raise WorkerPending
    fn = owner("leave_balances")
    assert fn is not None
    payload = {
        "candidate_id": str(cid),
        "leave_type_code": f.s("leave_type_code"),
        "legal_entity_id": str(le),
        "days": f.s("days"),
        "leave_year": f.s("leave_year"),
        "row_number": scratch.get("row_no", 0),
    }
    out = await fn(ctx, db, payload, batch_id, validate_only=validate_only)
    if isinstance(out, dict) and not out.get("ok", True):
        errors: list[ErrorItem] = []
        for message in out.get("errors") or ["Invalid row"]:
            column, code = None, "invalid"
            for needle, col, c in _LEAVE_MESSAGES:
                if needle in str(message).lower():
                    column, code = col, c
                    break
            errors.append(err(column, code, str(message)))
        raise RowErrors(errors)
    entry = out.get("entry_id") if isinstance(out, dict) else None
    return None if validate_only else f"leave_entry:{entry}"


HANDLERS: dict[str, Callable[..., Awaitable[str | None]]] = {
    "workers": workers,
    "placements": placements,
    "opening_invoices": opening_invoices,
    "leave_balances": leave_balances,
}
