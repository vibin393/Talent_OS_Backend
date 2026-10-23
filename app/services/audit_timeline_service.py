"""Record timeline (Epic 35 story 35.5, FR-183 / A6).

``timeline(db, subject_type, subject_id, cursor, limit)`` merges the ``audit_logs`` rows of a subject (e.g. an
invoice) with the rows of its related entities, resolved through ``audit.register_timeline_source``
(registered by the owning domains or, as a fallback, by ``audit_timeline_sources``). Rows are newest first
and keyset-paginated with an opaque cursor; each carries a one-line summary (``register_audit_summarizer``)
and the actor's display name (``register_actor_resolver``).

Masking: values of a model's ``__pii_fields__``, ``EncryptedStr`` columns, blind indexes and audit-encrypted
values are never revealed -- they read ``••••1234`` (the ``<field>_last4`` sibling when audited) or ``••••``.
A subject with no visible rows (unknown, or another tenant's under RLS) is 404.
"""

import base64
import binascii
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import and_, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.audit import ENC, ERASED, AuditLog
from app.core.encrypted import EncryptedStr
from app.core.errors import NotFound, ValidationFailed
from app.core.models import Base
from app.services import (
    audit_timeline_sources,  # ruff: ignore[unused-import] - registers the fallback sources / resolvers
)

MASK = "••••"
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
_MODELS: dict[str, type[Any]] | None = None


def _models() -> dict[str, type[Any]]:
    global _MODELS
    if _MODELS is None:
        _MODELS = {audit.entity_type_of(m.class_): m.class_ for m in Base.registry.mappers}
    return _MODELS


def _sensitive_fields(entity_type: str | None) -> set[str]:
    cls = _models().get(entity_type or "")
    if cls is None:
        return set()
    out = set(getattr(cls, "__pii_fields__", ()) or ())
    table = getattr(cls, "__table__", None)
    if table is not None:
        for col in table.columns:
            if isinstance(col.type, EncryptedStr):
                out.add(col.key)
    return out


def _is_protected_value(value: Any) -> bool:
    return (isinstance(value, Mapping) and ENC in value) or value == ERASED


def _last4(changes: Mapping[str, Any], field: str, side: str) -> str | None:
    sibling = changes.get(f"{field}_last4")
    if isinstance(sibling, Mapping):
        value = sibling.get(side)
        if isinstance(value, str) and value and not _is_protected_value(value):
            return value[-4:]
    return None


def _changes(entity_type: str | None, raw: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    sensitive = _sensitive_fields(entity_type)
    out: list[dict[str, Any]] = []
    for field in sorted(raw):
        diff = raw[field]
        if not isinstance(diff, Mapping):
            diff = {"old": None, "new": diff}
        old, new = diff.get("old"), diff.get("new")
        masked = (
            field in sensitive
            or field.endswith("_bidx")
            or _is_protected_value(old)
            or _is_protected_value(new)
        )
        if masked:
            old = None if old is None else MASK + (_last4(raw, field, "old") or "")
            new = None if new is None else MASK + (_last4(raw, field, "new") or "")
        out.append({"field": field, "old": old, "new": new, "masked": masked})
    return out


def _encode_cursor(at: datetime, row_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{row_id}".encode()).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        at, _, raw_id = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8").partition("|")
        return datetime.fromisoformat(at), uuid.UUID(raw_id)
    except (ValueError, binascii.Error, UnicodeError) as exc:
        raise ValidationFailed("Invalid cursor", details={"fields": {"cursor": "invalid"}}) from exc


def _humanize(entity_type: str | None) -> str:
    return (entity_type or "record").replace("_", " ").capitalize()


def _summary(row: AuditLog) -> str:
    fn = audit.audit_summarizer(row.action, row.entity_type)
    if fn is not None:
        try:
            text = fn(row)
        except Exception:
            logger.exception("audit summarizer failed for {}", row.action)
            text = None
        if text:
            return text
    verb = row.action.rsplit(".", 1)[-1].replace("_", " ")
    fields = sorted((row.changes or {}).keys())
    tail = (
        f": {', '.join(fields[:5])}{'…' if len(fields) > 5 else ''}" if verb == "updated" and fields else ""
    )
    return f"{_humanize(row.entity_type)} {verb}{tail}"


async def _actor_names(db: AsyncSession, rows: list[AuditLog]) -> dict[tuple[str, uuid.UUID], str]:
    by_kind: dict[str, set[uuid.UUID]] = {}
    for row in rows:
        if row.actor_id is not None:
            by_kind.setdefault(row.actor_type, set()).add(row.actor_id)
    names: dict[tuple[str, uuid.UUID], str] = {}
    for kind, ids in by_kind.items():
        resolver = audit.actor_resolver(kind)
        if resolver is None:
            continue
        try:
            resolved = await resolver(db, sorted(ids))
        except Exception:
            logger.exception("audit actor resolver failed for {}", kind)
            continue
        names.update({(kind, k): v for k, v in resolved.items()})
    return names


def _actor_display(row: AuditLog, names: Mapping[tuple[str, uuid.UUID], str]) -> str:
    if row.actor_id is not None and (row.actor_type, row.actor_id) in names:
        return names[row.actor_type, row.actor_id]
    return {"system": "System", "ai": "AI", "workflow": "Workflow", "platform": "Platform support"}.get(
        row.actor_type, row.actor_type.replace("_", " ").capitalize()
    )


async def related_entities(
    db: AsyncSession, subject_type: str, subject_id: uuid.UUID
) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = [(subject_type, str(subject_id))]
    for source in audit.timeline_sources(subject_type):
        for entity_type, entity_id in await source(db, subject_id):
            pair = (str(entity_type), str(entity_id))
            if pair not in pairs:
                pairs.append(pair)
    return pairs


async def timeline(
    db: AsyncSession,
    subject_type: str,
    subject_id: uuid.UUID,
    *,
    cursor: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    limit = max(1, min(limit, MAX_LIMIT))
    pairs = await related_entities(db, subject_type, subject_id)
    own = (
        select(AuditLog.id)
        .where(
            AuditLog.tenant_id.is_not(None),
            AuditLog.entity_type == subject_type,
            AuditLog.entity_id == str(subject_id),
        )
        .limit(1)
    )
    if (await db.execute(own)).first() is None:
        raise NotFound("Record not found", code="not_found")
    stmt = select(AuditLog).where(
        AuditLog.tenant_id.is_not(None),
        or_(*(and_(AuditLog.entity_type == t, AuditLog.entity_id == i) for t, i in pairs)),
    )
    if cursor:
        at, row_id = _decode_cursor(cursor)
        stmt = stmt.where(tuple_(AuditLog.occurred_at, AuditLog.id) < tuple_(at, row_id))
    stmt = stmt.order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(limit + 1)
    rows = list((await db.execute(stmt)).scalars().all())
    more = len(rows) > limit
    rows = rows[:limit]
    names = await _actor_names(db, rows)
    items = [
        {
            "id": row.id,
            "at": row.occurred_at,
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "actor_type": row.actor_type,
            "actor_id": row.actor_id,
            "actor_display": _actor_display(row, names),
            "summary": _summary(row),
            "changes": _changes(row.entity_type, row.changes),
        }
        for row in rows
    ]
    next_cursor = _encode_cursor(rows[-1].occurred_at, rows[-1].id) if more and rows else None
    return {"items": items, "next_cursor": next_cursor}
