# ruff: file-ignore[line-too-long]
"""Finance agent run starters (Epic 63 A3 / A4); hourly beats on ``maintenance``, deterministic, no model call when
nothing is due.

* ``daily_tick`` -- at 10:30 tenant-local time: one ``collections`` run per Client with a non-empty plan and no hold
  (``trigger_key="daily_scan"``, subject ``("client", id)``, dedup ``daily:{client}:{date}``). The P2
  ``receivables.daily`` beats are untouched.
* ``start_ptp_run`` -- ``PromiseToPayBroken`` (P2 already created the Task): one escalation run (dedup ``ptp:{promise}``).
* ``billing_prep_tick`` -- on the 1st at 09:00 tenant-local: one ``billing`` run per Legal Entity with an earlier
  invoice run, for the previous month (dedup ``{le}:{period}``); owner = the creator of the LE's last run.
"""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agentic import Kickoff
from app.core.context import RequestContext
from app.services import entity_refs
from app.services.collection_rules import facts, rules

COLLECTIONS_HOUR, COLLECTIONS_MINUTE = 10, 30
BILLING_DAY, BILLING_HOUR = 1, 9


async def _local(db: AsyncSession) -> datetime:
    tenant = await entity_refs.tenant(db, entity_refs.session_tenant(db))
    return datetime.now(tenant.tz)


def collections_kickoff(client_id: uuid.UUID, today: date, trigger: str, promise_id: uuid.UUID | None = None) -> Kickoff:
    inputs = {"client_id": str(client_id), "today": today.isoformat(), "trigger": trigger}
    if promise_id is not None:
        inputs["promise_id"] = str(promise_id)
    return Kickoff(inputs=inputs)


async def clients_with_open_documents(db: AsyncSession) -> list[uuid.UUID]:
    from app.models.billing import DebitNote, Invoice

    out: set[uuid.UUID] = set()
    for model in (Invoice, DebitNote):
        rows = (
            await db.execute(select(model.client_id).where(model.status == "issued", model.due_date.is_not(None)).distinct())
        ).scalars()
        out.update(rows)
    return sorted(out, key=str)


async def daily_tick(db: AsyncSession, *, force: bool = False, now: datetime | None = None) -> dict[str, Any]:
    from app.services import agent_runtime_service as runtime

    local = await _local(db)
    if not force and (local.hour != COLLECTIONS_HOUR or local.minute < COLLECTIONS_MINUTE - 30):
        return {"skipped": "not_run_hour"}
    tid = entity_refs.session_tenant(db)
    await rules.ensure_defaults(db)
    ctx = RequestContext(tenant_id=tid, actor_kind="system")
    started: list[str] = []
    for client_id in await clients_with_open_documents(db):
        loaded = await facts.load(db, client_id, now=now)
        if loaded.held or not loaded.plan.items:
            continue
        ref = await runtime.start_run(
            ctx, db, agent_key="collections", trigger_kind="schedule", trigger_key="daily_scan", subject=("client", client_id),
            kickoff=collections_kickoff(client_id, loaded.today, "daily_scan"),
            dedup_key=f"daily:{client_id}:{loaded.today.isoformat()}",
        )  # fmt: skip
        started.append(str(ref.run_id) if ref.run_id else f"not_started:{ref.reason}")
    return {"runs": started}


async def start_ptp_run(db: AsyncSession, client_id: uuid.UUID, promise_id: uuid.UUID) -> str | None:
    from app.services import agent_runtime_service as runtime
    from app.services.collection_rules import holds

    if await holds.is_held(db, client_id):
        return "held"
    local = await _local(db)
    ref = await runtime.start_run(
        RequestContext(tenant_id=entity_refs.session_tenant(db), actor_kind="system"), db, agent_key="collections",
        trigger_kind="event", trigger_key="ptp_broken", subject=("client", client_id),
        kickoff=collections_kickoff(client_id, local.date(), "ptp_broken", promise_id), dedup_key=f"ptp:{promise_id}",
    )  # fmt: skip
    return str(ref.run_id) if ref.run_id else ref.reason


def previous_month(today: date) -> tuple[date, date]:
    first = today.replace(day=1)
    end = first - timedelta(days=1)
    return end.replace(day=1), end


def billing_kickoff(legal_entity_id: uuid.UUID, start: date, end: date, client_id: uuid.UUID | None = None) -> Kickoff:
    return Kickoff(
        inputs={
            "legal_entity_id": str(legal_entity_id), "period_start": start.isoformat(), "period_end": end.isoformat(),
            "client_id": str(client_id) if client_id else "none",
        }
    )  # fmt: skip


async def billing_owner(db: AsyncSession, legal_entity_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.billing import InvoiceRun

    row = (
        await db.execute(
            select(InvoiceRun.created_by)
            .where(InvoiceRun.legal_entity_id == legal_entity_id, InvoiceRun.created_by.is_not(None))
            .order_by(InvoiceRun.created_at.desc())
            .limit(1)
        )
    ).first()
    return row[0] if row else None


async def billing_prep_tick(db: AsyncSession, *, force: bool = False, today: date | None = None) -> dict[str, Any]:
    from app.models.billing import InvoiceRun
    from app.services import agent_runtime_service as runtime

    local = await _local(db)
    day = today or local.date()
    if not force and (day.day != BILLING_DAY or local.hour != BILLING_HOUR):
        return {"skipped": "not_billing_day"}
    start, end = previous_month(day)
    ctx = RequestContext(tenant_id=entity_refs.session_tenant(db), actor_kind="system")
    started: list[str] = []
    les = (await db.execute(select(InvoiceRun.legal_entity_id).distinct())).scalars()
    for le in sorted(set(les), key=str):
        ref = await runtime.start_run(
            ctx, db, agent_key="billing", trigger_kind="schedule", trigger_key="billing_day", subject=("legal_entity", le),
            kickoff=billing_kickoff(le, start, end), dedup_key=f"{le}:{start.isoformat()}:{end.isoformat()}",
        )  # fmt: skip
        started.append(str(ref.run_id) if ref.run_id else f"not_started:{ref.reason}")
    return {"runs": started, "period": [start.isoformat(), end.isoformat()], "at": datetime.now(UTC).isoformat()}


__all__ = [
    "billing_kickoff",
    "billing_owner",
    "billing_prep_tick",
    "clients_with_open_documents",
    "collections_kickoff",
    "daily_tick",
    "previous_month",
    "start_ptp_run",
]
