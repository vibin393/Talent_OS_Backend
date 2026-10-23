# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Scheduled reports (Epic 47 story 47.4, FR-239, NFR-40).

* CRUD ``/report-schedules`` (``report:schedule``, the report's owner only). Recipients are active internal
  Users of the tenant (no Client / Vendor / Candidate principals, no external emails), at most
  ``max_recipients``; at most ``max_schedules`` per tenant. ``next_run_at`` is computed in the schedule's timezone.
* ``evaluate_due`` (beat ``reports.evaluate_schedules``, every 5 minutes): active schedules with
  ``next_run_at <= now`` (``FOR UPDATE SKIP LOCKED``) advance ``next_run_at`` and yield one job per recipient.
  Schedule-level failures (definition needs repair, owner lost ``report:schedule``) are counted here.
* ``run_for_recipient`` -- **once per recipient with that recipient's own permissions**:
  idempotent insert of the ``report_runs`` row (unique ``(schedule_id, slot_at, principal_user_id)``) -> principal
  from ``entity_refs.principal_for_user`` (``None`` / inactive -> ``skipped recipient_inactive``; no ``report:read``,
  no visibility, no source read or export permission -> ``skipped not_authorised``) -> ``execute.run_definition``
  (mode ``scheduled``) -> render + store + deliver (attachment or link) -> ``report.scheduled_run`` audit.
* Settling a slot (after its last recipient): every non-skipped run failed -> a schedule failure; any success
  resets ``consecutive_failures``. Each failure notifies the owner (``report_failed``); the third pauses the
  schedule. ``POST /report-schedules/{id}/resume`` re-activates it.
"""

import calendar
import uuid
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, DomainError, NotFound, ValidationFailed
from app.core.permissions import Principal, check
from app.models.identity import User
from app.models.report_builder import ReportDefinition, ReportRun, ReportSchedule
from app.schemas.report_builder import ScheduleCreate, ScheduleOut, ScheduleUpdate, VizSpec
from app.services.reporting.builder import delivery, governance
from app.services.reporting.builder.common import EXPORT, FINANCE_EXPORT, READ, SCHEDULE, now, principal_of
from app.services.reporting.builder.sharing import can_see, source_permission, visible_report
from app.services.reporting.builder.sources_p1p2 import FINANCE_SOURCES
from app.services.reporting.builder.validate import repair_issues

__all__ = [
    "FAILURES_TO_PAUSE",
    "create",
    "delete",
    "evaluate_due",
    "list_schedules",
    "next_occurrence",
    "next_occurrences",
    "resume",
    "run_for_recipient",
    "update",
]

FAILURES_TO_PAUSE = 3


# ================================================================ recurrence
def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValidationFailed(
            "Unknown timezone", code="schedule_timezone_invalid", details={"path": "timezone"}
        ) from None


def next_occurrence(
    cadence: str,
    weekday: int | None,
    month_day: int | None,
    local_time: dtime,
    timezone: str,
    after: datetime,
) -> datetime:
    """First occurrence strictly after ``after`` (UTC), evaluated in ``timezone``."""
    tz = _tz(timezone)
    start = after.astimezone(tz).date()
    for i in range(0, 400):
        day = start + timedelta(days=i)
        if cadence == "weekly" and day.weekday() != weekday:
            continue
        if cadence == "monthly":
            last = calendar.monthrange(day.year, day.month)[1]
            if day.day != (last if month_day == -1 else month_day):
                continue
        candidate = datetime.combine(day, local_time.replace(tzinfo=None), tzinfo=tz)
        if candidate > after:
            return candidate.astimezone(UTC)
    msg = "no occurrence within 400 days"  # pragma: no cover - every cadence recurs within a month
    raise ValueError(msg)


def next_occurrences(s: Any, after: datetime, n: int = 3) -> list[datetime]:
    out: list[datetime] = []
    cursor = after
    for _ in range(n):
        cursor = next_occurrence(s.cadence, s.weekday, s.month_day, s.local_time, s.timezone, cursor)
        out.append(cursor)
    return out


def preview_next_runs(data: ScheduleCreate, n: int = 3) -> list[datetime]:
    """The next ``n`` occurrences of an unsaved schedule (builder drawer preview)."""
    _check_recurrence(data.cadence, data.weekday, data.month_day)
    _tz(data.timezone)
    return next_occurrences(data, now(), n)


def _check_recurrence(cadence: str, weekday: int | None, month_day: int | None) -> None:
    if (cadence == "weekly") != (weekday is not None):
        raise ValidationFailed(
            "A weekly schedule needs a weekday (and only it)",
            code="schedule_recurrence_invalid",
            details={"path": "weekday"},
        )
    if (cadence == "monthly") != (month_day is not None):
        raise ValidationFailed(
            "A monthly schedule needs a month day (and only it)",
            code="schedule_recurrence_invalid",
            details={"path": "month_day"},
        )
    if month_day is not None and month_day != -1 and not 1 <= month_day <= 28:
        raise ValidationFailed(
            "Month day must be 1-28 or -1 (last day)",
            code="schedule_recurrence_invalid",
            details={"path": "month_day"},
        )


# ================================================================ CRUD
def schedule_out(s: ReportSchedule, report_name: str | None = None) -> ScheduleOut:
    return ScheduleOut(
        id=s.id,
        report_id=s.report_id,
        report_name=report_name,
        owner_id=s.owner_id,
        cadence=s.cadence,
        weekday=s.weekday,
        month_day=s.month_day,
        local_time=s.local_time,
        timezone=s.timezone,
        format=s.format,
        recipient_user_ids=list(s.recipient_user_ids),
        status=s.status,
        paused_reason=s.paused_reason,
        consecutive_failures=s.consecutive_failures,
        next_run_at=s.next_run_at,
        last_run_at=s.last_run_at,
        created_at=s.created_at,
    )


async def _check_recipients(db: AsyncSession, ids: list[uuid.UUID], max_recipients: int) -> list[uuid.UUID]:
    unique = list(dict.fromkeys(ids))
    if len(unique) > max_recipients:
        raise ValidationFailed(
            f"At most {max_recipients} recipients",
            code="schedule_too_many_recipients",
            details={"path": "recipient_user_ids"},
        )
    rows = (await db.execute(select(User.id, User.is_active).where(User.id.in_(unique)))).all()
    active = {r.id for r in rows if r.is_active}
    bad = [str(u) for u in unique if u not in active]
    if bad:
        raise ValidationFailed(
            "Recipients must be active internal users of this tenant",
            code="schedule_recipient_invalid",
            details={"path": "recipient_user_ids", "invalid": bad},
        )
    return unique


async def _own_schedule(
    ctx: RequestContext, db: AsyncSession, schedule_id: uuid.UUID
) -> tuple[Principal, ReportSchedule]:
    principal = principal_of(ctx)
    check(principal, SCHEDULE)
    s = (
        await db.execute(select(ReportSchedule).where(ReportSchedule.id == schedule_id).with_for_update())
    ).scalar_one_or_none()
    if s is None or s.owner_id != principal.id:
        raise NotFound("Schedule not found", code="schedule_not_found")
    return principal, s


def _audit(db: AsyncSession, action: str, s: ReportSchedule) -> None:
    audit.record(
        f"report.schedule_{action}",
        "report_schedule",
        s.id,
        session=db,
        report_id=str(s.report_id),
        cadence=s.cadence,
        format=s.format,
        recipients=[str(u) for u in s.recipient_user_ids],
        status=s.status,
    )


async def create(ctx: RequestContext, db: AsyncSession, data: ScheduleCreate) -> ScheduleOut:
    principal = principal_of(ctx)
    check(principal, SCHEDULE)
    report = await visible_report(db, principal, data.report_id)
    if report.owner_id != principal.id:
        raise NotFound("Report not found", code="report_not_found")
    if report.status == "needs_repair":
        raise ConflictError("Repair this report before scheduling it", code="report_needs_repair")
    caps = await governance.limits(db)
    total = (await db.execute(select(func.count()).select_from(ReportSchedule))).scalar_one()
    if total >= caps.max_schedules:
        raise ValidationFailed(
            f"This workspace already has {caps.max_schedules} schedules", code="schedule_limit_reached"
        )
    _check_recurrence(data.cadence, data.weekday, data.month_day)
    _tz(data.timezone)
    recipients = await _check_recipients(db, data.recipient_user_ids, caps.max_recipients)
    s = ReportSchedule(
        tenant_id=ctx.tenant_id,
        report_id=report.id,
        owner_id=principal.id,
        cadence=data.cadence,
        weekday=data.weekday,
        month_day=data.month_day,
        local_time=data.local_time,
        timezone=data.timezone,
        format=data.format,
        recipient_user_ids=recipients,
        status="active",
        consecutive_failures=0,
        next_run_at=next_occurrence(
            data.cadence, data.weekday, data.month_day, data.local_time, data.timezone, now()
        ),
    )
    db.add(s)
    await db.flush()
    await db.refresh(s)
    _audit(db, "created", s)
    return schedule_out(s, report.name)


async def update(
    ctx: RequestContext, db: AsyncSession, schedule_id: uuid.UUID, data: ScheduleUpdate
) -> ScheduleOut:
    _, s = await _own_schedule(ctx, db, schedule_id)
    fields = data.model_dump(exclude_unset=True)
    if "status" in fields and fields["status"] == "active" and s.status == "paused":
        return await resume(ctx, db, schedule_id)
    if "cadence" in fields:
        # Changing the cadence resets the day selectors the request did not send.
        s.cadence = fields.pop("cadence")
        s.weekday = fields.pop("weekday", None)
        s.month_day = fields.pop("month_day", None)
    caps = await governance.limits(db)
    for key, value in fields.items():
        if key == "recipient_user_ids":
            value = await _check_recipients(db, value, caps.max_recipients)
        if key == "status" and value == "paused":
            s.paused_reason = "paused_by_owner"
        setattr(s, key, value)
    _check_recurrence(s.cadence, s.weekday, s.month_day)
    s.next_run_at = next_occurrence(s.cadence, s.weekday, s.month_day, s.local_time, s.timezone, now())
    await db.flush()
    await db.refresh(s)
    _audit(db, "updated", s)
    return schedule_out(s)


async def resume(ctx: RequestContext, db: AsyncSession, schedule_id: uuid.UUID) -> ScheduleOut:
    _, s = await _own_schedule(ctx, db, schedule_id)
    report = await db.get(ReportDefinition, s.report_id)
    if report is None or report.archived_at is not None:
        raise NotFound("Report not found", code="report_not_found")
    if report.status == "needs_repair" or repair_issues(report.definition):
        raise ConflictError("Repair this report before resuming its schedule", code="report_needs_repair")
    s.status = "active"
    s.paused_reason = None
    s.consecutive_failures = 0
    s.next_run_at = next_occurrence(s.cadence, s.weekday, s.month_day, s.local_time, s.timezone, now())
    await db.flush()
    await db.refresh(s)
    _audit(db, "resumed", s)
    return schedule_out(s, report.name)


async def delete(ctx: RequestContext, db: AsyncSession, schedule_id: uuid.UUID) -> None:
    _, s = await _own_schedule(ctx, db, schedule_id)
    _audit(db, "deleted", s)
    await db.delete(s)
    await db.flush()


async def list_schedules(
    ctx: RequestContext, db: AsyncSession, *, report_id: uuid.UUID | None = None
) -> list[ScheduleOut]:
    principal = principal_of(ctx)
    stmt = (
        select(ReportSchedule, ReportDefinition.name)
        .join(ReportDefinition, ReportDefinition.id == ReportSchedule.report_id)
        .where(ReportSchedule.owner_id == principal.id)
        .order_by(ReportSchedule.created_at.desc())
    )
    if report_id is not None:
        stmt = stmt.where(ReportSchedule.report_id == report_id)
    return [schedule_out(s, name) for s, name in (await db.execute(stmt)).all()]


# ================================================================ failures
async def _notify_owner(
    db: AsyncSession, s: ReportSchedule, report_name: str, reason: str, *, paused: bool, slot: datetime | None
) -> None:
    from app.services import notifications_service as notifications

    title = (
        f"Schedule for '{report_name}' paused after {FAILURES_TO_PAUSE} failures"
        if paused
        else f"Scheduled report '{report_name}' failed"
    )
    try:
        await notifications.deliver(
            db,
            "report_failed",
            [s.owner_id],
            notifications.Content(
                title=title,
                body=f"Reason: {reason.replace('_', ' ')}.",
                entity_type="report_schedule",
                entity_id=s.id,
                link_path=f"/reports/custom/{s.report_id}",
            ),
            event_id=f"report_failed:{s.id}:{slot.isoformat() if slot else 'na'}:{int(paused)}",
            payload={"paused": paused},
        )
    except Exception:
        logger.exception("Could not notify the owner of schedule {}", s.id)


async def record_failure(
    db: AsyncSession, s: ReportSchedule, reason: str, *, slot: datetime | None, pause_now: bool = False
) -> None:
    report = await db.get(ReportDefinition, s.report_id)
    name = report.name if report else "report"
    s.consecutive_failures += 1
    paused = pause_now or s.consecutive_failures >= FAILURES_TO_PAUSE
    if paused:
        s.status = "paused"
        s.paused_reason = reason
    await _notify_owner(db, s, name, reason, paused=paused, slot=slot)
    audit.record(
        "report.schedule_failed",
        "report_schedule",
        s.id,
        session=db,
        report_id=str(s.report_id),
        reason=reason,
        consecutive_failures=s.consecutive_failures,
        paused=paused,
    )
    await db.flush()


# ================================================================ evaluator
async def evaluate_due(
    db: AsyncSession, at: datetime | None = None
) -> list[tuple[uuid.UUID, datetime, uuid.UUID]]:
    """Advance due schedules of the session's tenant; returns ``(schedule_id, slot_at, user_id)`` jobs."""
    from app.services import entity_refs
    from app.services.reporting.builder import repair

    at = at or now()
    due = (
        (
            await db.execute(
                select(ReportSchedule)
                .where(ReportSchedule.status == "active", ReportSchedule.next_run_at <= at)
                .order_by(ReportSchedule.next_run_at)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    jobs: list[tuple[uuid.UUID, datetime, uuid.UUID]] = []
    for s in due:
        slot = s.next_run_at
        assert slot is not None
        s.next_run_at = next_occurrence(
            s.cadence, s.weekday, s.month_day, s.local_time, s.timezone, max(at, slot)
        )
        report = await db.get(ReportDefinition, s.report_id)
        if report is None or report.archived_at is not None:
            await record_failure(db, s, "report_archived", slot=slot, pause_now=True)
            continue
        issues = repair_issues(report.definition)
        if issues or report.status == "needs_repair":
            if issues:
                await repair.flag(db, report, issues)
            await record_failure(db, s, "needs_repair", slot=slot, pause_now=True)
            continue
        owner = await entity_refs.principal_for_user(db, s.tenant_id, s.owner_id)
        if owner is None or not owner.has(SCHEDULE):
            await record_failure(db, s, "owner_lost_permission", slot=slot)
            continue
        jobs.extend((s.id, slot, uid) for uid in s.recipient_user_ids)
    await db.flush()
    return jobs


# ================================================================ per-recipient run
async def _claim(
    db: AsyncSession, s: ReportSchedule, report: ReportDefinition | None, slot: datetime, user_id: uuid.UUID
) -> uuid.UUID | None:
    run_id = uuid.uuid4()
    inserted = (
        await db.execute(
            pg_insert(ReportRun)
            .values(
                id=run_id,
                tenant_id=s.tenant_id,
                report_id=s.report_id,
                source_key=report.source_key if report else "",
                definition_version=report.definition_version if report else None,
                schedule_id=s.id,
                slot_at=slot,
                run_kind="scheduled",
                principal_user_id=user_id,
                status="running",
                format=s.format,
                delivery="none",
                masked_fields=[],
                started_at=now(),
            )
            .on_conflict_do_nothing(
                index_elements=["schedule_id", "slot_at", "principal_user_id"],
                index_where=ReportRun.schedule_id.is_not(None),
            )
            .returning(ReportRun.id)
        )
    ).scalar_one_or_none()
    return inserted


async def _skip(db: AsyncSession, run: ReportRun, reason: str) -> str:
    run.status = "skipped"
    run.skip_reason = reason
    run.finished_at = now()
    await db.flush()
    governance.audit_run(db, "scheduled_run", run, recipient=str(run.principal_user_id), skip_reason=reason)
    return "skipped"


async def _fail(db: AsyncSession, run: ReportRun, code: str) -> str:
    run.status = "failed"
    run.limit_kind = None
    run.error_code = code[:60]
    run.finished_at = now()
    await db.flush()
    governance.audit_run(db, "scheduled_run", run, recipient=str(run.principal_user_id), error_code=code)
    return "failed"


def _authorised(principal: Principal, report: ReportDefinition) -> bool:
    read_perm = source_permission(report.source_key)
    if (
        not principal.has(READ)
        or not principal.has(EXPORT)
        or read_perm is None
        or not principal.has(read_perm)
    ):
        return False
    return not (report.source_key in FINANCE_SOURCES and not principal.has(FINANCE_EXPORT))


async def run_for_recipient(
    db: AsyncSession, schedule_id: uuid.UUID, slot_at: datetime, user_id: uuid.UUID
) -> str:
    """One recipient's run of one slot (idempotent). Returns ``done | limit_hit | skipped | failed | duplicate``."""
    from app.services import entity_refs
    from app.services.reporting.builder.execute import run_definition

    s = await db.get(ReportSchedule, schedule_id)
    if s is None:
        return "missing"
    report = await db.get(ReportDefinition, s.report_id)
    run_id = await _claim(db, s, report, slot_at, user_id)
    if run_id is None:
        return "duplicate"
    run = await db.get(ReportRun, run_id)
    assert run is not None
    status = await _run(db, s, report, run, user_id, entity_refs, run_definition)
    await _settle(db, s.id, slot_at)
    return status


async def _run(
    db: AsyncSession,
    s: ReportSchedule,
    report: ReportDefinition | None,
    run: ReportRun,
    user_id: uuid.UUID,
    entity_refs: Any,
    run_definition: Any,
) -> str:
    if report is None or report.archived_at is not None:
        return await _fail(db, run, "report_not_found")
    principal = await entity_refs.principal_for_user(db, s.tenant_id, user_id)
    if principal is None:
        return await _skip(db, run, "recipient_inactive")
    if not _authorised(principal, report) or not await can_see(db, principal, report.id):
        return await _skip(db, run, "not_authorised")
    ctx = RequestContext(tenant_id=s.tenant_id, principal=principal)
    caps = await governance.limits(db)
    try:
        async with db.begin_nested():
            outcome = await run_definition(
                ctx, db, definition=report.definition, viz=VizSpec.model_validate(report.viz),
                mode="scheduled", report=report, run=run, fmt=s.format,
            )  # fmt: skip
    except DomainError as exc:
        logger.info("Scheduled run {} failed: {}", run.id, exc.code)
        return await _fail(db, run, exc.code)
    try:
        tz = ZoneInfo(s.timezone)
        at = run.finished_at or now()
        data = delivery.render(
            s.format, report.name, outcome.columns, outcome.rows, tz=tz, run_at=at,
            masked_fields=outcome.masked_fields, limit_message=outcome.limit_message,
        )  # fmt: skip
        name = delivery.filename(report.name, s.format, at, tz)
        run.file_id = await delivery.store(ctx, db, run, data, fmt=s.format, name=name)
        kind = delivery.delivery_kind(len(data), outcome.pii_classes, caps.attachment_max_mb)
        user = await entity_refs.user(db, user_id)
        await delivery.deliver(
            db, run, to_email=user.email, recipient_name=user.name, report_name=report.name, data=data,
            fmt=s.format, name=name, kind=kind, link_ttl_days=caps.link_ttl_days,
            masked_fields=outcome.masked_fields, limit_message=outcome.limit_message,
        )  # fmt: skip
        if kind == "attachment":
            run.expires_at = (run.finished_at or now()) + timedelta(days=caps.link_ttl_days)
    except DomainError as exc:
        return await _fail(db, run, exc.code)
    except Exception:
        logger.exception("Delivering scheduled run {} failed", run.id)
        return await _fail(db, run, "delivery_failed")
    await db.flush()
    audit.record(
        "report.delivered",
        "report_definition",
        report.id,
        session=db,
        run_id=str(run.id),
        recipient=str(user_id),
        delivery=run.delivery,
        row_count=run.row_count,
        masked_fields=list(run.masked_fields or []),
    )
    return run.status


async def _settle(db: AsyncSession, schedule_id: uuid.UUID, slot: datetime) -> None:
    s = (
        await db.execute(select(ReportSchedule).where(ReportSchedule.id == schedule_id).with_for_update())
    ).scalar_one_or_none()
    if s is None or (s.last_run_at is not None and s.last_run_at >= slot):
        return
    statuses = list(
        (
            await db.execute(
                select(ReportRun.status).where(ReportRun.schedule_id == s.id, ReportRun.slot_at == slot)
            )
        ).scalars()
    )
    if len(statuses) < len(s.recipient_user_ids) or any(st in ("queued", "running") for st in statuses):
        return
    s.last_run_at = slot
    succeeded = [st for st in statuses if st in ("done", "limit_hit")]
    failed = [st for st in statuses if st == "failed"]
    if failed and not succeeded:
        await record_failure(db, s, "all_recipients_failed", slot=slot)
    elif succeeded:
        s.consecutive_failures = 0
    await db.flush()
