# ruff: noqa: E501, D401
"""The outbox sink (Epic 68, §0.6), registered as ``register_sink("webhooks", on_rows)``. Runs inside the relay's
SAVEPOINT: an exception leaves the whole outbox batch pending (66). Idempotent: ``webhook_delivery_keys`` holds one
row per (subscription, event); a delivery row is inserted only with a fresh key."""

import contextlib
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.oauth import AppInstallation
from app.models.webhooks import WebhookDelivery, WebhookDeliveryKey, WebhookSubscription
from app.services.webhooks import common

ERASED = "subject.erased"
INSTALLATION_CHANGED = "app.installation_changed"


def candidate_of(row: Any) -> uuid.UUID | None:
    data = dict(row.data or {})
    if row.subject_type == "candidate":
        return row.subject_id
    raw = data.get("candidate_id")
    if raw:
        try:
            return uuid.UUID(str(raw))
        except ValueError:
            return None
    return None


async def insert_pair(db: AsyncSession, sub: WebhookSubscription, row: Any, *, status: str = "pending") -> uuid.UUID | None:
    """Create the (subscription, event) delivery at most once. Returns the new delivery id (None = already there)."""
    delivery_id = uuid.uuid4()
    inserted = (
        await db.execute(
            pg_insert(WebhookDeliveryKey)
            .values(tenant_id=sub.tenant_id, subscription_id=sub.id, event_id=row.id, delivery_id=delivery_id)
            .on_conflict_do_nothing(index_elements=["tenant_id", "subscription_id", "event_id"])
            .returning(WebhookDeliveryKey.delivery_id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        return None
    at = common.now()
    db.add(
        WebhookDelivery(
            id=delivery_id, tenant_id=sub.tenant_id, created_at=at, updated_at=at, subscription_id=sub.id, event_id=row.id,
            event_type=row.event_type, event_seq=getattr(row, "seq", None), subject_type=row.subject_type, subject_id=row.subject_id,
            candidate_id=candidate_of(row), kind="event", status=status, next_attempt_at=at if status == "pending" else None,
        )
    )  # fmt: skip
    return delivery_id


async def _record_exposure(ctx: RequestContext, sub: WebhookSubscription, row: Any) -> None:
    """D-11: an Installation that receives a candidate / worker id is tracked for the ``subject.erased`` fan-out."""
    if sub.installation_id is None:
        return
    from app.services import exposure_service

    subjects: list[tuple[str, uuid.UUID]] = []
    if row.subject_type in ("candidate", "worker"):
        subjects.append((row.subject_type, row.subject_id))
    cand = candidate_of(row)
    if cand is not None and row.subject_type != "candidate":
        subjects.append(("candidate", cand))
    for stype, sid in subjects:
        with contextlib.suppress(Exception):  # exposure buffering never blocks the sink
            await exposure_service.record(ctx, subject_type=stype, subject_id=sid, channel="webhook", installation_id=sub.installation_id)


async def _subscriptions(db: AsyncSession) -> list[WebhookSubscription]:
    return list(
        (await db.execute(select(WebhookSubscription).where(WebhookSubscription.deleted_at.is_(None), WebhookSubscription.status.in_(("active", "disabled"))))).scalars()
    )


async def _installations(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, AppInstallation]:
    if not ids:
        return {}
    rows = (await db.execute(select(AppInstallation).where(AppInstallation.id.in_(list(ids))))).scalars()
    return {r.id: r for r in rows}


def matches(sub: WebhookSubscription, row: Any, inst: AppInstallation | None) -> bool:
    """§0.6 step 2-3 (``subject.erased`` excluded: ``erasure_fanout``)."""
    etype = row.event_type
    if etype == ERASED or etype in common.UNROUTABLE_TYPES:
        return False
    if not common.type_matches(sub, etype):
        return False
    data = dict(row.data or {})
    if etype == INSTALLATION_CHANGED:
        return sub.installation_id is not None and str(data.get("installation_id")) == str(sub.installation_id)
    if sub.installation_id is not None:
        if inst is None or inst.status == "uninstalled":
            return False
        if not common.installation_allows(etype, inst.granted_scopes or []):
            return False
        if sub.suppress_own_changes and row.app_installation_id is not None and row.app_installation_id == sub.installation_id:
            return False
    elif sub.all_events and etype not in common.tenant_wildcard_types():
        return False
    return common.filters_match(sub.filters or {}, data)


async def on_rows(ctx: RequestContext, db: AsyncSession, rows: Sequence[Any]) -> None:
    """The sink: one pass over the batch; ``held`` rows for disabled Subscriptions; then the D-11 fan-out."""
    subs = await _subscriptions(db)
    erased = [r for r in rows if r.event_type == ERASED]
    created = 0
    if subs:
        insts = await _installations(db, {s.installation_id for s in subs if s.installation_id is not None})
        for row in rows:
            if row.event_type == ERASED:
                continue
            for sub in subs:
                inst = insts.get(sub.installation_id) if sub.installation_id else None
                if not matches(sub, row, inst):
                    continue
                status = "pending" if sub.status == "active" else "held"
                if await insert_pair(db, sub, row, status=status) is not None:
                    created += status == "pending"
                    if status == "pending":
                        await _record_exposure(ctx, sub, row)
    if erased:
        from app.services.webhooks import erasure_fanout

        for row in erased:
            created += await erasure_fanout.fan_out(ctx, db, row, subs)
    if created:
        await db.flush()
        assert ctx.tenant_id is not None
        common.kick_after_commit(db, ctx.tenant_id)


__all__ = ["candidate_of", "insert_pair", "matches", "on_rows"]
