# ruff: file-ignore[line-too-long]
"""Outbox rows, the frozen wire envelope and the replay read model (Epic 66, AD-79, FR-334/350)."""

import base64
import dataclasses
import hashlib
import hmac
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import public_events as registry
from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.core.settings import get_settings
from app.models.public_events import OutboxEvent
from app.schemas.public_events import EventOut, EventQuery, EventSubject, EventTenantRef, PublicPage

API_VERSION = "v1"


@dataclasses.dataclass(frozen=True, slots=True)
class PublicEventRow:
    id: uuid.UUID
    seq: int
    tenant_id: uuid.UUID
    event_type: str
    event_version: int
    visibility: str
    subject_type: str
    subject_id: uuid.UUID
    occurred_at: datetime
    data: Mapping[str, Any]
    actor_kind: str | None
    app_installation_id: uuid.UUID | None


OutboxRow = PublicEventRow


def row_factory(draft: registry.OutboxDraft) -> OutboxEvent:
    """Installed with ``core.public_events.set_outbox_row_factory``: the ORM row of one outbox draft."""
    return OutboxEvent(
        tenant_id=draft.tenant_id,
        event_type=draft.event_type,
        event_version=draft.event_version,
        visibility=draft.visibility,
        subject_type=draft.subject_type,
        subject_id=draft.subject_id,
        occurred_at=draft.occurred_at,
        data=draft.data,
        source_event=draft.source_event,
        actor_kind=draft.actor_kind,
        app_installation_id=draft.app_installation_id,
        dispatch_status="pending",
    )


def to_row(e: OutboxEvent) -> PublicEventRow:
    return PublicEventRow(
        id=e.id, seq=int(e.seq), tenant_id=e.tenant_id, event_type=e.event_type, event_version=int(e.event_version),
        visibility=e.visibility, subject_type=e.subject_type, subject_id=e.subject_id, occurred_at=e.occurred_at,
        data=dict(e.data or {}), actor_kind=e.actor_kind, app_installation_id=e.app_installation_id,
    )  # fmt: skip


def iso_z(dt: datetime) -> str:
    """ISO-8601 UTC, second precision, ``Z`` suffix (the frozen envelope format)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope_dict(row: PublicEventRow, tenant_slug: str, livemode: bool) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "type": row.event_type,
        "version": row.event_version,
        "api_version": API_VERSION,
        "occurred_at": iso_z(row.occurred_at),
        "sequence": row.seq,
        "tenant_ref": {"id": str(row.tenant_id), "slug": tenant_slug},
        "livemode": bool(livemode),
        "subject": {"type": row.subject_type, "id": str(row.subject_id)},
        "data": dict(row.data),
    }


def envelope(row: PublicEventRow, tenant_slug: str, livemode: bool) -> bytes:
    """Canonical JSON of the frozen wire envelope: sorted keys, ``(",", ":")`` separators, UTF-8 (the bytes Epic 68 signs)."""
    return json.dumps(
        envelope_dict(row, tenant_slug, livemode), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


async def tenant_ref(db: AsyncSession, tenant_id: uuid.UUID) -> tuple[str, bool]:
    """``(slug, livemode)`` of a tenant (``livemode`` = mode is ``production``)."""
    row = (
        await db.execute(
            text("SELECT slug, mode FROM tenants WHERE id = CAST(:t AS uuid)"), {"t": str(tenant_id)}
        )
    ).first()
    if row is None:
        raise NotFound("Tenant not found")
    return str(row[0]), (row[1] or "production") == "production"


# ------------------------------------------------------------------ replay / backfill read model
def _cursor_secret() -> bytes:
    return (get_settings().public_cursor_secret or "talent-os-dev-cursor-secret").encode()


def encode_cursor(seq: int) -> str:
    payload = (
        base64.urlsafe_b64encode(json.dumps({"s": seq}, separators=(",", ":")).encode()).decode().rstrip("=")
    )
    sig = hmac.new(_cursor_secret(), f"events.{payload}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def decode_cursor(cursor: str) -> int:
    try:
        payload, sig = cursor.rsplit(".", 1)
        want = hmac.new(_cursor_secret(), f"events.{payload}".encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, want):
            raise ValueError
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        return int(json.loads(raw)["s"])
    except Exception as exc:
        raise ValidationFailed("Invalid cursor", code="invalid_cursor") from exc


def _visible_types(ctx: RequestContext) -> tuple[set[str] | None, uuid.UUID | None]:
    """App principals see the public types their scopes allow (scope-less types only for their own Installation's
    ``app.installation_changed``); internal principals see every public type."""
    principal = ctx.principal
    if getattr(principal, "type", None) != "app":
        return None, None
    granted = set(getattr(principal, "oauth_scopes", ()) or ())
    allowed = {t for t, s in registry.catalogue().items() if s.scopes and granted.intersection(s.scopes)}
    return allowed, getattr(principal, "app_installation_id", None) or getattr(principal, "id", None)


def _out(row: PublicEventRow, slug: str, livemode: bool) -> EventOut:
    return EventOut(
        id=row.id, type=row.event_type, version=row.event_version, api_version=API_VERSION,
        occurred_at=row.occurred_at, sequence=row.seq, tenant_ref=EventTenantRef(id=row.tenant_id, slug=slug),
        livemode=livemode, subject=EventSubject(type=row.subject_type, id=row.subject_id), data=dict(row.data),
    )  # fmt: skip


def _scoped(stmt: Any, ctx: RequestContext) -> Any:
    stmt = stmt.where(OutboxEvent.visibility == "public")
    cutoff = datetime.now(UTC) - timedelta(days=get_settings().public_event_retention_days)
    stmt = stmt.where(OutboxEvent.occurred_at >= cutoff)
    allowed, installation_id = _visible_types(ctx)
    if allowed is not None:
        own = and_(
            OutboxEvent.event_type == "app.installation_changed",
            OutboxEvent.data["installation_id"].astext == str(installation_id),
        )
        stmt = stmt.where(OutboxEvent.event_type.in_(sorted(allowed)) | own)
    return stmt


async def list_events(ctx: RequestContext, db: AsyncSession, q: EventQuery) -> PublicPage[EventOut]:
    stmt = _scoped(select(OutboxEvent), ctx)
    if q.type:
        stmt = stmt.where(OutboxEvent.event_type == q.type)
    if q.subject_type:
        stmt = stmt.where(OutboxEvent.subject_type == q.subject_type)
    if q.subject_id:
        stmt = stmt.where(OutboxEvent.subject_id == q.subject_id)
    if q.occurred_after:
        stmt = stmt.where(OutboxEvent.occurred_at >= q.occurred_after)
    if q.occurred_before:
        stmt = stmt.where(OutboxEvent.occurred_at < q.occurred_before)
    if q.cursor:
        stmt = stmt.where(OutboxEvent.seq > decode_cursor(q.cursor))
    found = list((await db.execute(stmt.order_by(OutboxEvent.seq).limit(q.limit + 1))).scalars())
    has_more = len(found) > q.limit
    rows = [to_row(e) for e in found[: q.limit]]
    if not rows:
        return PublicPage[EventOut](data=[], next_cursor=None, has_more=False)
    slug, livemode = await tenant_ref(db, rows[0].tenant_id)
    return PublicPage[EventOut](
        data=[_out(r, slug, livemode) for r in rows],
        next_cursor=encode_cursor(rows[-1].seq) if has_more else None,
        has_more=has_more,
    )


async def get_event(ctx: RequestContext, db: AsyncSession, event_id: uuid.UUID) -> EventOut:
    e = (
        await db.execute(_scoped(select(OutboxEvent), ctx).where(OutboxEvent.id == event_id))
    ).scalar_one_or_none()
    if e is None:
        raise NotFound("Event not found")
    row = to_row(e)
    slug, livemode = await tenant_ref(db, row.tenant_id)
    return _out(row, slug, livemode)


async def purge(db: AsyncSession, *, older_than_days: int) -> int:
    """Delete dispatched rows older than the retention window (pending rows are never dropped: no loss)."""
    cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
    result = await db.execute(
        delete(OutboxEvent).where(
            OutboxEvent.dispatch_status == "dispatched", OutboxEvent.occurred_at < cutoff
        )
    )
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = [
    "API_VERSION",
    "OutboxRow",
    "PublicEventRow",
    "decode_cursor",
    "encode_cursor",
    "envelope",
    "envelope_dict",
    "get_event",
    "iso_z",
    "list_events",
    "purge",
    "row_factory",
    "tenant_ref",
    "to_row",
]
