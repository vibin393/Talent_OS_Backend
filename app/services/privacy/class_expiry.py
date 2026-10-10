"""Retention-class expiry (Epic 35 A3, FR-180, AD-34).

* ``preview(ctx, db, class_key)`` -- per bound table, the rows whose anchor + period has passed, skipping rows of
  candidates under a legal hold and rows covered by a ``retention_overrides`` row. A ``report_only`` class (the
  ``financial`` class: issued financial rows are DB-immutable, AD-21) only reports -- no run is created and the
  reason is "immutable record, awaiting policy". Otherwise a ``retention_runs`` row (``kind='class_expiry'``,
  status ``previewed``) keeps the row ids in ``criteria.rows`` (capped).
* Execution goes through the P1 ``/retention/runs/{id}/execute`` endpoint with the typed confirmation ``EXPIRE``
  (``retention.execute``), or directly when the class has ``auto_execute``. The worker (``process_run``) calls
  each binding's ``expire_fn(db, ids, action)`` in batches of ``RETENTION_EXPIRY_BATCH`` (the owner deletes
  files through ``core.storage`` and tombstones document versions), audits ``retention.expired`` per batch and
  emits ``RetentionExpired``.
* ``scan_tenant`` -- the daily beat ``privacy.retention_expiry_scan`` (03:00 IST, queue ``maintenance``).
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.core.events import emit
from app.core.settings import get_settings
from app.events.privacy import RetentionExpired, RetentionExpiryPreviewed
from app.models.privacy import LegalHold, RetentionClass, RetentionOverride, RetentionRun
from app.schemas.privacy import ClassExpiryPreviewOut, ClassExpiryTableOut
from app.services.privacy.common import actor_id, now, require_tenant
from app.services.privacy.erasure import SessionFactory
from app.services.privacy.retention_bindings import (
    FINANCIAL_REASON,
    BoundRow,
    anchor_of,
    register_fallback_bindings,
    until_for,
)
from app.services.privacy.retention_classes import retention_bindings

MAX_ROWS = 50_000


async def _class(db: AsyncSession, class_key: str) -> RetentionClass:
    from app.services.privacy.retention_classes import seed_system_classes

    await seed_system_classes(db)
    row = (
        await db.execute(
            select(RetentionClass).where(
                RetentionClass.key == class_key, RetentionClass.archived_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Retention class not found")
    return row


async def _candidate_rows(db: AsyncSession, table_name: str) -> list[BoundRow]:
    """Every bound row of ``table_name`` with its candidate (tenant-wide)."""
    from app.core.models import Base

    table = Base.metadata.tables.get(table_name)
    if table is None or "id" not in table.c:
        return []
    cols = table.c
    if "candidate_id" in cols:
        stmt = select(cols.id, cols.candidate_id)
    elif "placement_id" in cols and "placements" in Base.metadata.tables:
        placements = Base.metadata.tables["placements"]
        stmt = select(cols.id, placements.c.candidate_id).join(
            placements, placements.c.id == cols.placement_id
        )
    elif "owner_kind" in cols and "owner_id" in cols:
        stmt = select(cols.id, cols.owner_id).where(cols.owner_kind.in_(("candidate", "worker")))
    else:
        stmt = select(cols.id, cols.id.is_(None))  # no subject link: candidate unknown
    rows = (await db.execute(stmt.limit(MAX_ROWS))).all()
    return [BoundRow(table_name, rid, cid if isinstance(cid, uuid.UUID) else None) for rid, cid in rows]


async def collect(
    db: AsyncSession, cls: RetentionClass, *, today: date | None = None
) -> dict[str, dict[str, Any]]:
    """``{table: {"ids": [...], "skipped_legal_hold": n, "skipped_override": n}}`` of expired rows."""
    register_fallback_bindings()
    today = today or datetime.now(UTC).date()
    held = set(
        (await db.execute(select(LegalHold.candidate_id).where(LegalHold.released_at.is_(None)))).scalars()
    )
    overrides = {
        r.subject_id: r.until_date
        for r in (
            await db.execute(
                select(RetentionOverride).where(
                    RetentionOverride.class_key == cls.key, RetentionOverride.subject_kind == "candidate"
                )
            )
        ).scalars()
    }
    out: dict[str, dict[str, Any]] = {}
    for table_name, binding in sorted(retention_bindings().items()):
        if binding.class_key != cls.key:
            continue
        entry: dict[str, Any] = {"ids": [], "skipped_legal_hold": 0, "skipped_override": 0}
        for row in await _candidate_rows(db, table_name):
            until = until_for(await anchor_of(db, binding, row), cls)
            if until is None or until > today:
                continue
            if row.candidate_id in held:
                entry["skipped_legal_hold"] += 1
                continue
            override = overrides.get(row.candidate_id) if row.candidate_id else None
            if override is not None and override > today:
                entry["skipped_override"] += 1
                continue
            entry["ids"].append(str(row.row_id))
        out[table_name] = entry
    return out


async def preview(
    ctx: RequestContext, db: AsyncSession, class_key: str, *, today: date | None = None
) -> ClassExpiryPreviewOut:
    tenant_id = require_tenant(ctx)
    cls = await _class(db, class_key)
    found = await collect(db, cls, today=today)
    by_table = [
        ClassExpiryTableOut(
            table=t,
            count=len(e["ids"]),
            skipped_legal_hold=e["skipped_legal_hold"],
            skipped_override=e["skipped_override"],
        )
        for t, e in found.items()
    ]
    total = sum(t.count for t in by_table)
    report_only = cls.expiry_action == "report_only"
    run_id: uuid.UUID | None = None
    valid_until: datetime | None = None
    if not report_only and total:
        run = RetentionRun(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            kind="class_expiry",
            status="previewed",
            criteria={
                "class_key": cls.key,
                "action": cls.expiry_action,
                "rows": {t: e["ids"] for t, e in found.items() if e["ids"]},
            },
            candidate_count=total,
            created_by=actor_id(ctx),
        )
        db.add(run)
        await db.flush()
        run_id = run.id
        valid_until = run.created_at + timedelta(days=get_settings().retention_preview_valid_days)
        emit(db, RetentionExpiryPreviewed(tenant_id=tenant_id, run_id=run.id, class_key=cls.key, total=total))
    audit.record(
        "retention.expiry_previewed", "retention_class", cls.id, session=db, class_key=cls.key, total=total,
        report_only=report_only,
    )  # fmt: skip
    return ClassExpiryPreviewOut(
        run_id=run_id,
        class_key=cls.key,
        report_only=report_only,
        reason=FINANCIAL_REASON if report_only else None,
        total=total,
        by_table=by_table,
        valid_until=valid_until,
    )


async def _call_expire(binding: Any, db: AsyncSession, ids: list[uuid.UUID], action: str) -> Any:
    return await binding.expire_fn(db, ids, action)


async def process_run(
    tenant_id: uuid.UUID, run_id: uuid.UUID, *, sessions: SessionFactory = tenant_session
) -> dict[str, int]:
    """Worker side of an ``EXPIRE``: call each binding's ``expire_fn`` in batches, audit each batch."""
    batch_size = get_settings().retention_expiry_batch
    async with sessions(tenant_id) as db:
        run = await db.get(RetentionRun, run_id)
        if run is None or run.status != "executing" or run.kind != "class_expiry":
            return {}
        class_key = str(run.criteria["class_key"])
        action = str(run.criteria.get("action") or "delete")
        rows: dict[str, list[str]] = dict(run.criteria.get("rows") or {})
    bindings = retention_bindings()
    counts: dict[str, int] = {}
    for table_name, ids in rows.items():
        binding = bindings.get(table_name)
        if binding is None:
            counts[f"{table_name}:unbound"] = len(ids)
            continue
        for start in range(0, len(ids), batch_size):
            chunk = [uuid.UUID(i) for i in ids[start : start + batch_size]]
            try:
                async with sessions(tenant_id) as db:
                    audit.set_actor(db, "system", None)
                    await _call_expire(binding, db, chunk, action)
                    audit.record(
                        "retention.expired", "retention_run", run_id, session=db, class_key=class_key,
                        table=table_name, count=len(chunk), action=action,
                    )  # fmt: skip
                    emit(
                        db,
                        RetentionExpired(
                            tenant_id=tenant_id,
                            run_id=run_id,
                            class_key=class_key,
                            table=table_name,
                            count=len(chunk),
                        ),
                    )
                counts[table_name] = counts.get(table_name, 0) + len(chunk)
            except Exception:
                logger.exception("retention expiry of {} failed (run {})", table_name, run_id)
                counts[f"{table_name}:failed"] = counts.get(f"{table_name}:failed", 0) + len(chunk)
    async with sessions(tenant_id) as db:
        audit.set_actor(db, "system", None)
        run = await db.get(RetentionRun, run_id, with_for_update=True)
        if run is not None and run.status == "executing":
            run.status = "completed"
            run.criteria = {**run.criteria, "result": counts}
    return counts


async def scan_tenant(tenant_id: uuid.UUID, *, sessions: SessionFactory = tenant_session) -> dict[str, int]:
    """Daily beat: preview every non-report-only class; auto-execute where the class allows it."""
    from app.services.privacy import retention

    ctx = RequestContext(tenant_id=tenant_id)
    totals: dict[str, int] = {}
    async with sessions(tenant_id) as db:
        audit.set_actor(db, "system", None)
        keys = list(
            (
                await db.execute(
                    select(RetentionClass.key).where(
                        RetentionClass.archived_at.is_(None), RetentionClass.expiry_action != "report_only"
                    )
                )
            ).scalars()
        )
    bound = {b.class_key for b in retention_bindings().values()}
    for key in keys:
        if key not in bound:
            continue
        async with sessions(tenant_id) as db:
            audit.set_actor(db, "system", None)
            # one open preview per class: cancel the stale ones
            for stale in (
                await db.execute(
                    select(RetentionRun).where(
                        RetentionRun.kind == "class_expiry",
                        RetentionRun.status == "previewed",
                        RetentionRun.criteria["class_key"].astext == key,
                    )
                )
            ).scalars():
                stale.status = "cancelled"
            out = await preview(ctx, db, key)
            totals[key] = out.total
            cls = await _class(db, key)
            if out.run_id is not None and cls.auto_execute:
                await retention.execute(ctx, db, out.run_id, actor="system")
    return totals


def is_expiry_run(run: RetentionRun) -> bool:
    return run.kind == "class_expiry"


__all__ = ["collect", "is_expiry_run", "now", "preview", "process_run", "scan_tenant"]
