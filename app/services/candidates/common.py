"""Shared helpers of the candidates service: normalisation (emails, phones, links, skill text) and
permission-aware candidate lookup (404 when not visible, 403 when visible but not editable)."""

import re
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

import phonenumbers
from email_validator import EmailNotValidError, validate_email
from sqlalchemy import ColumnElement, Select, column, select, table
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.permissions import Principal, forbidden, scope_condition
from app.models.candidates import Candidate

READ = "candidate:read"
CREATE = "candidate:create"
UPDATE = "candidate:update"
DELETE = "candidate:delete"
MERGE = "candidate:merge"

DEFAULT_PHONE_REGION = "IN"


def now() -> datetime:
    return datetime.now(UTC)


def principal_of(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal):  # pragma: no cover - routes always authenticate
        msg = "candidate services require an authenticated principal"
        raise TypeError(msg)
    return principal


def not_found() -> NotFound:
    return NotFound("Candidate not found")


def field_error(field: str, message: str, code: str = "validation_failed") -> ValidationFailed:
    return ValidationFailed(message, code=code, details={"fields": [{"field": field, "message": message}]})


# ================================================================ normalisation
def normalize_email(raw: str) -> str | None:
    try:
        return validate_email(raw.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        return None


def normalize_phone(raw: str, region: str = DEFAULT_PHONE_REGION) -> str | None:
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_url(raw: str) -> str | None:
    """Lower-case host, strip scheme, ``www.``, query/fragment and trailing slash."""
    text = raw.strip()
    if not text:
        return None
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host or "." not in host:
        return None
    host = host.removeprefix("www.")
    port = f":{parts.port}" if parts.port else ""
    path = parts.path.rstrip("/")
    return f"{host}{port}{path}"[:500]


def link_kind(url_norm: str) -> str:
    host = url_norm.split("/", 1)[0]
    if host.endswith("linkedin.com"):
        return "linkedin"
    if host.endswith("github.com"):
        return "github"
    return "portfolio"


_SKILL_KEEP = re.compile(r"[^\w\s+#.]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize_skill(text: str) -> str:
    """lowercase, trim, collapse whitespace; keeps ``+ # .`` (C++, C#, .NET, Node.js)."""
    lowered = _SKILL_KEEP.sub(" ", text.lower())
    return _SPACES.sub(" ", lowered).strip()


def slugify(text: str) -> str:
    norm = normalize_skill(text)
    slug = norm.replace("+", "plus").replace("#", "sharp").replace(".", "dot")
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug[:200] or "skill"


# ================================================================ access
def read_condition(principal: Principal) -> ColumnElement[bool] | None:
    return scope_condition(principal, READ, Candidate.owner_id)


def visible_stmt(principal: Principal, permission: str = READ) -> Select[tuple[Candidate]]:
    stmt = select(Candidate)
    cond = scope_condition(principal, permission, Candidate.owner_id)
    return stmt if cond is None else stmt.where(cond)


async def can_access(
    db: AsyncSession, principal: Principal, candidate_id: uuid.UUID, permission: str
) -> bool:
    stmt = select(Candidate.id).where(Candidate.id == candidate_id)
    cond = scope_condition(principal, permission, Candidate.owner_id)
    if cond is not None:
        stmt = stmt.where(cond)
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def get_visible(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, *, for_update: bool = False
) -> Candidate:
    """Return the candidate if the caller may read it (else 404)."""
    principal = principal_of(ctx)
    stmt = visible_stmt(principal).where(Candidate.id == candidate_id)
    if for_update:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise not_found()
    return row


async def get_for(
    ctx: RequestContext,
    db: AsyncSession,
    candidate_id: uuid.UUID,
    permission: str,
    *,
    for_update: bool = False,
) -> Candidate:
    """Readable (else 404) and ``permission`` in scope (else 403)."""
    row = await get_visible(ctx, db, candidate_id, for_update=for_update)
    if not await can_access(db, principal_of(ctx), candidate_id, permission):
        raise forbidden(permission)
    return row


async def get_editable(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, *, for_update: bool = False
) -> Candidate:
    return await get_for(ctx, db, candidate_id, UPDATE, for_update=for_update)


# ================================================================ users (read-only lookup)
# Display names for owner columns. Identity exposes no bulk user lookup yet; this read-only projection of
# ``users`` (like ``core.permissions`` does for scoping) is RLS-scoped to the tenant.
_users = table("users", column("id"), column("name"), column("is_active"))


async def user_names(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await db.execute(select(_users.c.id, _users.c.name).where(_users.c.id.in_(ids)))
    return {r[0]: r[1] for r in rows}


async def active_user_exists(db: AsyncSession, user_id: uuid.UUID) -> bool:
    stmt = select(_users.c.id).where(_users.c.id == user_id, _users.c.is_active.is_(True))
    return (await db.execute(stmt)).scalar_one_or_none() is not None
