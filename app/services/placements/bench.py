"""Bench entries (Epic 24, FR-125; PRD §5.14).

``transition_entry`` is the only writer of ``bench_entries.status`` (AD-4). Placement facts are read only
through ``placements.service`` (``pay_context``, ``get_placement_summary``, AD-25). Cost is ``Decimal`` only,
quantized once (``bench_cost``, AD-20); masked without ``compensation:view``.

Also: event handlers (``on_placement_ended`` / ``_activated``, ``on_submission_created``,
``on_application_closed``; idempotent, AD-9) and the Worker portal functions (``portal_*``, A5).
"""

import dataclasses
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.fiscal import today_for
from app.core.money import minor_units, to_decimal
from app.core.permissions import Principal, can_view_compensation
from app.events.placements import BenchEntryStatusChanged
from app.models.placements import BENCH_OPEN_STATUSES, BenchEntry, BenchSettings, Placement
from app.schemas.bench import (
    BenchEntryCreate,
    BenchEntryOut,
    BenchEntryPage,
    BenchEntryUpdate,
    BenchSettingsOut,
    BenchSettingsUpdate,
    CostOverrideIn,
    ReleaseIn,
    SkillRef,
)
from app.schemas.bench_portal import OptInIn, WorkerBenchOut, WorkerBenchState
from app.services.placements import bench_cost
from app.services.placements.bench_events import BenchEntryCreated

SYSTEM = ActorRef(kind="system", id=None)
_SYSTEM = frozenset({"system"})
_USER = frozenset({"user"})
# §5.14 exactly. ``-> released`` additionally admits ``candidate`` for ``origin=contractor_opt_in`` (A5).
BENCH_TRANSITIONS: dict[tuple[str, str], frozenset[str]] = {
    ("on_bench", "proposed"): _SYSTEM,
    ("proposed", "on_bench"): _SYSTEM,
    ("on_bench", "redeployed"): _SYSTEM,
    ("proposed", "redeployed"): _SYSTEM,
    ("on_bench", "released"): _USER,
    ("proposed", "released"): _USER,
}
TERMINAL = frozenset({"redeployed", "released"})
CLOSED_STAGES = frozenset({"rejected", "withdrawn"})


def _now() -> datetime:
    return datetime.now(UTC)


def not_found() -> NotFound:
    return NotFound("Bench entry not found")


def allowed_actors(from_status: str, to_status: str, origin: str) -> frozenset[str]:
    actors = BENCH_TRANSITIONS.get((from_status, to_status), frozenset())
    if to_status == "released" and actors and origin == "contractor_opt_in":
        actors = actors | {"candidate"}
    return actors


def check_transition(from_status: str, to_status: str, actor_kind: str, origin: str = "manual") -> None:
    if actor_kind not in allowed_actors(from_status, to_status, origin):
        raise ConflictError(
            f"A bench entry cannot move from {from_status} to {to_status}",
            code="transition_not_allowed",
            details={"from": from_status, "to": to_status, "actor": actor_kind},
        )


def _tenant(ctx: RequestContext, db: AsyncSession) -> uuid.UUID:
    if ctx.tenant_id is not None:
        return ctx.tenant_id
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def _show_comp(ctx: RequestContext) -> bool:
    principal = ctx.principal
    return isinstance(principal, Principal) and can_view_compensation(principal)


# ---------------------------------------------------------------- settings
async def settings_row(db: AsyncSession, tenant_id: uuid.UUID) -> BenchSettings:
    row = await db.get(BenchSettings, tenant_id)
    if row is None:
        row = BenchSettings(
            tenant_id=tenant_id,
            hours_per_day=Decimal("8.00"),
            monthly_rate_divisor=Decimal("22.00"),
            bench_day_basis="weekdays",
            upcoming_roll_off_days=30,
            notify_strong_bench_match=True,
            require_bench_review_for_vendor_release=False,
            contractor_opt_in_enabled=True,
        )
        db.add(row)
        await db.flush()
    return row


def _settings_out(row: BenchSettings) -> BenchSettingsOut:
    return BenchSettingsOut(
        hours_per_day=row.hours_per_day,
        monthly_rate_divisor=row.monthly_rate_divisor,
        bench_day_basis=row.bench_day_basis,  # type: ignore[arg-type]
        upcoming_roll_off_days=row.upcoming_roll_off_days,
        notify_strong_bench_match=row.notify_strong_bench_match,
        require_bench_review_for_vendor_release=row.require_bench_review_for_vendor_release,
        contractor_opt_in_enabled=row.contractor_opt_in_enabled,
    )


async def get_settings(ctx: RequestContext, db: AsyncSession) -> BenchSettingsOut:
    return _settings_out(await settings_row(db, _tenant(ctx, db)))


async def update_settings(
    ctx: RequestContext, db: AsyncSession, data: BenchSettingsUpdate
) -> BenchSettingsOut:
    row = await settings_row(db, _tenant(ctx, db))
    for key, value in data.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, key, value)
    await db.flush()
    return _settings_out(row)


def cost_settings(row: BenchSettings) -> bench_cost.CostSettings:
    return bench_cost.CostSettings(
        hours_per_day=to_decimal(row.hours_per_day), monthly_rate_divisor=to_decimal(row.monthly_rate_divisor)
    )


async def tenant_today(db: AsyncSession, tenant_id: uuid.UUID) -> date:
    from app.services.directory_service import tenant_timezone

    try:
        tz = await tenant_timezone(db, tenant_id)
    except Exception:  # pragma: no cover - fall back to the launch market
        tz = "Asia/Kolkata"
    return today_for(tz or "Asia/Kolkata")


def until_for(entry: BenchEntry, today: date, tz: str = "Asia/Kolkata") -> date:
    if entry.closed_at is not None:
        from app.core.fiscal import local_date

        return local_date(entry.closed_at, tz)
    return today


# ---------------------------------------------------------------- output
async def _candidates(db: AsyncSession, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Any]:
    from app.models.candidates import Candidate

    wanted = list(set(ids))
    if not wanted:
        return {}
    rows = await db.execute(
        select(Candidate.id, Candidate.full_name, Candidate.human_id).where(Candidate.id.in_(wanted))
    )
    return {r.id: r for r in rows}


def entry_out(
    entry: BenchEntry, *, show_comp: bool, today: date, basis: str, cand: Any | None = None
) -> BenchEntryOut:
    days = bench_cost.bench_days(entry.available_from, until_for(entry, today), basis)
    to_date = bench_cost.cost_to_date(entry.cost_per_day, days, entry.cost_currency)
    return BenchEntryOut(
        id=entry.id,
        candidate_id=entry.candidate_id,
        candidate_name=getattr(cand, "full_name", None),
        candidate_human_id=getattr(cand, "human_id", None),
        source_placement_id=entry.source_placement_id,
        legal_entity_id=entry.legal_entity_id,
        status=entry.status,  # type: ignore[arg-type]
        origin=entry.origin,  # type: ignore[arg-type]
        available_from=entry.available_from,
        skills=[SkillRef(**s) for s in entry.skills_snapshot or []],
        location=entry.location,
        work_authorization=entry.work_authorization,
        preferred_locations=list(entry.preferred_locations or []),
        bench_days=days,
        cost_per_day=entry.cost_per_day if show_comp else None,
        cost_to_date=to_date if show_comp else None,
        cost_currency=entry.cost_currency,
        cost_source=entry.cost_source,
        cost_rate_unit_basis=entry.cost_rate_unit_basis if show_comp else None,
        cost_unknown=entry.cost_per_day is None,
        cost_masked=not show_comp,
        proposed_application_ids=list(entry.proposed_application_ids or []),
        proposed_count=len(entry.proposed_application_ids or []),
        redeployed_placement_id=entry.redeployed_placement_id,
        released_reason=entry.released_reason,  # type: ignore[arg-type]
        released_note=entry.released_note,
        closed_at=entry.closed_at,
        notes=entry.notes,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
    )


async def _out(ctx: RequestContext, db: AsyncSession, entry: BenchEntry) -> BenchEntryOut:
    tid = _tenant(ctx, db)
    settings = await settings_row(db, tid)
    cands = await _candidates(db, [entry.candidate_id])
    await db.refresh(entry)
    return entry_out(
        entry,
        show_comp=_show_comp(ctx),
        today=await tenant_today(db, tid),
        basis=settings.bench_day_basis,
        cand=cands.get(entry.candidate_id),
    )


# ---------------------------------------------------------------- reads
async def _get(db: AsyncSession, entry_id: uuid.UUID, *, lock: bool = False) -> BenchEntry:
    stmt = select(BenchEntry).where(BenchEntry.id == entry_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise not_found()
    return row


async def open_entry_for(
    db: AsyncSession, candidate_id: uuid.UUID, *, lock: bool = False
) -> BenchEntry | None:
    stmt = select(BenchEntry).where(
        BenchEntry.candidate_id == candidate_id, BenchEntry.status.in_(BENCH_OPEN_STATUSES)
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_entry(ctx: RequestContext, db: AsyncSession, entry_id: uuid.UUID) -> BenchEntryOut:
    return await _out(ctx, db, await _get(db, entry_id))


async def list_entries(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    status: Sequence[str] = (),
    skill_ids: Sequence[uuid.UUID] = (),
    location: str | None = None,
    available_before: date | None = None,
    legal_entity_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 25,
) -> BenchEntryPage:
    from app.models.candidates import Candidate

    stmt = select(BenchEntry, Candidate.full_name, Candidate.human_id).join(
        Candidate, Candidate.id == BenchEntry.candidate_id
    )
    if status:
        stmt = stmt.where(BenchEntry.status.in_(list(status)))
    if skill_ids:
        stmt = stmt.where(
            or_(
                *[
                    or_(
                        BenchEntry.skills_snapshot.contains([{"skill_id": str(s)}]),
                        BenchEntry.skills_snapshot.contains([{"tenant_skill_id": str(s)}]),
                    )
                    for s in skill_ids
                ]
            )
        )
    if location:
        term = f"%{location.strip()}%"
        stmt = stmt.where(
            or_(
                BenchEntry.location["city"].astext.ilike(term),
                BenchEntry.location["state"].astext.ilike(term),
                BenchEntry.location["country"].astext.ilike(term),
            )
        )
    if available_before is not None:
        stmt = stmt.where(BenchEntry.available_from <= available_before)
    if legal_entity_id is not None:
        stmt = stmt.where(BenchEntry.legal_entity_id == legal_entity_id)
    if client_id is not None:
        stmt = stmt.where(
            BenchEntry.source_placement_id.in_(select(Placement.id).where(Placement.client_id == client_id))
        )
    if q:
        stmt = stmt.where(or_(Candidate.full_name.ilike(f"%{q.strip()}%"), Candidate.human_id == q.strip()))
    total = int((await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
    rows = (
        await db.execute(
            stmt.order_by(BenchEntry.available_from, BenchEntry.id)
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()
    tid = _tenant(ctx, db)
    settings = await settings_row(db, tid)
    today = await tenant_today(db, tid)
    show = _show_comp(ctx)
    items = [
        entry_out(
            e,
            show_comp=show,
            today=today,
            basis=settings.bench_day_basis,
            cand=_Name(full_name=name, human_id=hid),
        )
        for e, name, hid in rows
    ]
    return BenchEntryPage(items=items, total=total, page=page, page_size=page_size)


@dataclasses.dataclass(frozen=True, slots=True)
class _Name:
    full_name: str | None
    human_id: str | None


# ---------------------------------------------------------------- snapshots / cost
async def skills_snapshot(db: AsyncSession, candidate_id: uuid.UUID) -> list[dict[str, Any]]:
    from app.models.candidates import CandidateSkill, Skill, TenantSkill

    rows = await db.execute(
        select(CandidateSkill.skill_id, CandidateSkill.tenant_skill_id, Skill.name, TenantSkill.name)
        .outerjoin(Skill, Skill.id == CandidateSkill.skill_id)
        .outerjoin(TenantSkill, TenantSkill.id == CandidateSkill.tenant_skill_id)
        .where(CandidateSkill.candidate_id == candidate_id)
    )
    out = []
    for skill_id, tenant_skill_id, name, tname in rows:
        out.append(
            {
                "skill_id": str(skill_id) if skill_id else None,
                "tenant_skill_id": str(tenant_skill_id) if tenant_skill_id else None,
                "name": name or tname or "",
            }
        )
    return sorted(out, key=lambda s: s["name"].lower())


async def _candidate_facts(
    db: AsyncSession, candidate_id: uuid.UUID
) -> tuple[dict[str, Any] | None, str | None]:
    from app.models.candidates import Candidate

    row = (
        await db.execute(
            select(Candidate.location, Candidate.work_authorization).where(Candidate.id == candidate_id)
        )
    ).first()
    if row is None:
        raise NotFound("Candidate not found")
    loc = row.location if isinstance(row.location, dict) else {}
    location = {k: loc[k] for k in ("city", "state", "country") if loc.get(k)} or None
    return location, row.work_authorization


async def cost_from_placement(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, on_date: date, settings: BenchSettings
) -> tuple[Decimal | None, str | None, str | None]:
    """``(cost_per_day, currency, basis)`` from the pay Rate Segment active on ``on_date`` (vendor rate for
    vendor-supplied Workers); ``(None, currency, None)`` when it cannot be derived."""
    from app.services.placements.service import pay_context

    try:
        pc = await pay_context(ctx, db, placement_id, on_date)
    except NotFound:
        return None, None, None
    rate = pc.vendor_rate if pc.payee_kind == "vendor" else pc.pay_rate
    cs = cost_settings(settings)
    cost = bench_cost.daily_cost(rate, pc.rate_unit, pc.currency, cs)
    return cost, pc.currency, bench_cost.basis_label(pc.rate_unit, cs) if cost is not None else None


# ---------------------------------------------------------------- writes
def _open_conflict(entry: BenchEntry) -> ConflictError:
    return ConflictError(
        "This worker already has an open bench entry",
        code="bench_entry_open",
        details={"bench_entry_id": str(entry.id)},
    )


async def _insert(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    candidate_id: uuid.UUID,
    available_from: date,
    origin: str,
    source_placement_id: uuid.UUID | None = None,
    legal_entity_id: uuid.UUID | None = None,
    work_authorization: str | None = None,
    location: dict[str, Any] | None = None,
    preferred_locations: Sequence[str] = (),
    notes: str | None = None,
    cost: tuple[Decimal | None, str | None, str | None] = (None, None, None),
) -> BenchEntry | None:
    """Insert an open entry; ``None`` when one is already open (partial-unique violation -> no-op)."""
    tid = _tenant(ctx, db)
    cand_location, cand_auth = await _candidate_facts(db, candidate_id)
    entry = BenchEntry(
        tenant_id=tid,
        candidate_id=candidate_id,
        source_placement_id=source_placement_id,
        legal_entity_id=legal_entity_id,
        status="on_bench",
        available_from=available_from,
        origin=origin,
        skills_snapshot=await skills_snapshot(db, candidate_id),
        location=location if location is not None else cand_location,
        work_authorization=work_authorization if work_authorization is not None else cand_auth,
        preferred_locations=list(preferred_locations),
        cost_per_day=cost[0],
        cost_currency=cost[1],
        cost_source="rate_segment" if cost[0] is not None else None,
        cost_rate_unit_basis=cost[2],
        proposed_application_ids=[],
        notes=notes,
    )
    try:
        async with db.begin_nested():
            db.add(entry)
            await db.flush()
    except IntegrityError:
        return None
    emit(
        db,
        BenchEntryCreated(tenant_id=tid, bench_entry_id=entry.id, candidate_id=candidate_id, origin=origin),
    )
    return entry


async def create_entry(ctx: RequestContext, db: AsyncSession, data: BenchEntryCreate) -> BenchEntryOut:
    """Manual add (``origin=manual``); 409 ``bench_entry_open`` (+ id) when one is already open."""
    existing = await open_entry_for(db, data.candidate_id)
    if existing is not None:
        raise _open_conflict(existing)
    tid = _tenant(ctx, db)
    settings = await settings_row(db, tid)
    cost: tuple[Decimal | None, str | None, str | None] = (None, None, None)
    legal_entity_id = data.legal_entity_id
    if data.source_placement_id is not None:
        from app.services.placements.service import get_placement_summary

        summary = await get_placement_summary(ctx, db, data.source_placement_id)
        if summary.candidate_id != data.candidate_id:
            raise ValidationFailed("The placement belongs to another worker", code="placement_mismatch")
        legal_entity_id = legal_entity_id or summary.legal_entity_id
        cost = await cost_from_placement(
            ctx,
            db,
            data.source_placement_id,
            summary.end_date or data.available_from - timedelta(days=1),
            settings,
        )
    entry = await _insert(
        ctx,
        db,
        candidate_id=data.candidate_id,
        available_from=data.available_from,
        origin="manual",
        source_placement_id=data.source_placement_id,
        legal_entity_id=legal_entity_id,
        work_authorization=data.work_authorization,
        location=data.location,
        notes=data.notes,
        cost=cost,
    )
    if entry is None:
        existing = await open_entry_for(db, data.candidate_id)
        if existing is not None:
            raise _open_conflict(existing)
        raise ConflictError("Could not create the bench entry", code="bench_entry_open")  # pragma: no cover
    return await _out(ctx, db, entry)


def _require_open(entry: BenchEntry) -> None:
    if entry.status in TERMINAL:
        raise ConflictError("The bench entry is closed", code="bench_entry_closed")


async def update_entry(
    ctx: RequestContext, db: AsyncSession, entry_id: uuid.UUID, data: BenchEntryUpdate
) -> BenchEntryOut:
    entry = await _get(db, entry_id, lock=True)
    fields = data.model_dump(exclude_unset=True)
    if "available_from" in fields:
        _require_open(entry)
        if data.available_from is None:
            raise ValidationFailed("available_from cannot be empty", code="validation_error")
        entry.available_from = data.available_from
    for key in ("notes", "location", "work_authorization"):
        if key in fields:
            setattr(entry, key, fields[key])
    if "preferred_locations" in fields and data.preferred_locations is not None:
        entry.preferred_locations = list(data.preferred_locations)
    if data.refresh_skills:
        entry.skills_snapshot = await skills_snapshot(db, entry.candidate_id)
    await db.flush()
    return await _out(ctx, db, entry)


async def override_cost(
    ctx: RequestContext, db: AsyncSession, entry_id: uuid.UUID, data: CostOverrideIn
) -> BenchEntryOut:
    entry = await _get(db, entry_id, lock=True)
    _require_open(entry)
    currency = data.currency.upper()
    try:
        places = minor_units(currency)
    except ValueError as exc:
        raise ValidationFailed("Unknown currency", code="currency_not_supported") from exc
    if data.cost_per_day != data.cost_per_day.quantize(Decimal(1).scaleb(-places)):
        raise ValidationFailed("Too many decimal places for the currency", code="validation_error")
    entry.cost_per_day = data.cost_per_day
    entry.cost_currency = currency
    entry.cost_source = "manual"
    entry.cost_rate_unit_basis = "manual"
    await db.flush()
    return await _out(ctx, db, entry)


async def transition(
    db: AsyncSession,
    entry: BenchEntry,
    to: str,
    actor: ActorRef,
    *,
    reason: str | None = None,
    note: str | None = None,
    redeployed_placement_id: uuid.UUID | None = None,
) -> BenchEntry:
    """Move ``entry`` to ``to``: the only writer of ``bench_entries.status`` (AD-4)."""
    from_status = entry.status
    check_transition(from_status, to, actor.kind, entry.origin)
    if to == "released":
        if reason is None:
            raise ValidationFailed("A release reason is required", code="validation_error")
        if reason == "other" and not (note or "").strip():
            raise ValidationFailed("A note is required when the reason is 'other'", code="validation_error")
        entry.released_reason = reason
        entry.released_note = note
    if to == "redeployed":
        entry.redeployed_placement_id = redeployed_placement_id
    if to in TERMINAL:
        entry.closed_at = _now()
    entry.status = to
    await db.flush()
    emit(
        db,
        BenchEntryStatusChanged(
            tenant_id=entry.tenant_id,
            bench_entry_id=entry.id,
            candidate_id=entry.candidate_id,
            from_status=from_status,
            to_status=to,
        ),
    )
    return entry


async def transition_entry(
    ctx: RequestContext,
    db: AsyncSession,
    entry_id: uuid.UUID,
    to: str,
    actor: ActorRef,
    reason: str | None = None,
    note: str | None = None,
) -> BenchEntryOut:
    entry = await _get(db, entry_id, lock=True)
    await transition(db, entry, to, actor, reason=reason, note=note)
    return await _out(ctx, db, entry)


async def release(
    ctx: RequestContext, db: AsyncSession, entry_id: uuid.UUID, data: ReleaseIn
) -> BenchEntryOut:
    principal = ctx.principal
    actor = ActorRef(kind="user", id=getattr(principal, "id", None))
    return await transition_entry(ctx, db, entry_id, "released", actor, data.reason, data.note)


# ---------------------------------------------------------------- event handlers (system, idempotent)
async def on_placement_ended(
    ctx: RequestContext, db: AsyncSession, payload: dict[str, Any]
) -> uuid.UUID | None:
    if not payload.get("bench_eligible"):
        return None
    placement_id = uuid.UUID(str(payload["placement_id"]))
    candidate_id = uuid.UUID(str(payload["candidate_id"]))
    end_date = payload["end_date"]
    end_date = end_date if isinstance(end_date, date) else date.fromisoformat(str(end_date))
    seen = (
        await db.execute(select(BenchEntry.id).where(BenchEntry.source_placement_id == placement_id).limit(1))
    ).scalar_one_or_none()
    if seen is not None or await open_entry_for(db, candidate_id) is not None:
        return None
    from app.services.placements.service import get_placement_summary

    summary = await get_placement_summary(ctx, db, placement_id)
    settings = await settings_row(db, _tenant(ctx, db))
    cost = await cost_from_placement(ctx, db, placement_id, end_date, settings)
    entry = await _insert(
        ctx,
        db,
        candidate_id=candidate_id,
        available_from=end_date + timedelta(days=1),
        origin="placement_ended",
        source_placement_id=placement_id,
        legal_entity_id=summary.legal_entity_id,
        cost=cost,
    )
    return entry.id if entry is not None else None


async def on_placement_activated(ctx: RequestContext, db: AsyncSession, payload: dict[str, Any]) -> bool:  # ruff: ignore[unused-function-argument]
    placement_id = uuid.UUID(str(payload["placement_id"]))
    entry = await open_entry_for(db, uuid.UUID(str(payload["candidate_id"])), lock=True)
    if entry is None or entry.source_placement_id == placement_id:
        return False
    await transition(db, entry, "redeployed", SYSTEM, redeployed_placement_id=placement_id)
    return True


async def _application_candidate(db: AsyncSession, application_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.pipeline import Application

    return (
        await db.execute(select(Application.candidate_id).where(Application.id == application_id))
    ).scalar_one_or_none()


async def on_submission_created(ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID) -> bool:  # ruff: ignore[unused-function-argument]
    candidate_id = await _application_candidate(db, application_id)
    if candidate_id is None:
        return False
    entry = await open_entry_for(db, candidate_id, lock=True)
    if entry is None:
        return False
    ids = list(entry.proposed_application_ids or [])
    if application_id not in ids:
        entry.proposed_application_ids = [*ids, application_id]
    if entry.status == "on_bench":
        await transition(db, entry, "proposed", SYSTEM)
    else:
        await db.flush()
    return True


async def on_application_closed(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    application_id: uuid.UUID,
    candidate_id: uuid.UUID,
) -> bool:
    entry = await open_entry_for(db, candidate_id, lock=True)
    if entry is None or application_id not in (entry.proposed_application_ids or []):
        return False
    entry.proposed_application_ids = [a for a in entry.proposed_application_ids if a != application_id]
    await db.flush()
    if entry.status == "proposed" and not entry.proposed_application_ids:
        await transition(db, entry, "on_bench", SYSTEM)
    return True


# ---------------------------------------------------------------- Worker portal (A5; no cost fields)
def _portal_out(entry: BenchEntry) -> WorkerBenchOut:
    return WorkerBenchOut(
        id=entry.id,
        status=entry.status,  # type: ignore[arg-type]
        origin=entry.origin,  # type: ignore[arg-type]
        available_from=entry.available_from,
        work_authorization=entry.work_authorization,
        preferred_locations=[str(x) for x in entry.preferred_locations or []],
    )


async def _opt_in_eligible(db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> bool:
    settings = await settings_row(db, tenant_id)
    if not settings.contractor_opt_in_enabled:
        return False
    latest = (
        await db.execute(
            select(Placement.status, Placement.worker_type)
            .where(Placement.candidate_id == candidate_id, Placement.status != "dropped")
            .order_by(Placement.planned_start_date.desc(), Placement.created_at.desc())
            .limit(1)
        )
    ).first()
    return latest is not None and latest.status == "ended" and latest.worker_type == "contractor"


async def portal_state(db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> WorkerBenchState:
    entry = await open_entry_for(db, candidate_id)
    if entry is not None:
        return WorkerBenchState(
            entry=_portal_out(entry), can_opt_in=False, can_opt_out=entry.origin == "contractor_opt_in"
        )
    return WorkerBenchState(
        entry=None, can_opt_in=await _opt_in_eligible(db, tenant_id, candidate_id), can_opt_out=False
    )


async def portal_opt_in(
    ctx: RequestContext, db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID, data: OptInIn
) -> WorkerBenchState:
    if await open_entry_for(db, candidate_id) is not None:
        raise ConflictError("You are already on the bench", code="bench_entry_open")
    if not await _opt_in_eligible(db, tenant_id, candidate_id):
        raise ConflictError("Bench opt-in is not available for you right now", code="opt_in_not_eligible")
    sys_ctx = RequestContext(tenant_id=tenant_id, actor_kind="candidate") if ctx.tenant_id is None else ctx
    entry = await _insert(
        sys_ctx,
        db,
        candidate_id=candidate_id,
        available_from=data.available_from,
        origin="contractor_opt_in",
        work_authorization=data.work_authorization,
        preferred_locations=data.preferred_locations,
    )
    if entry is None:
        raise ConflictError("You are already on the bench", code="bench_entry_open")
    return await portal_state(db, tenant_id, candidate_id)


async def portal_opt_out(db: AsyncSession, tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> WorkerBenchState:
    entry = await open_entry_for(db, candidate_id, lock=True)
    if entry is None or entry.origin != "contractor_opt_in":
        raise NotFound("You have no bench opt-in to withdraw")
    await transition(
        db, entry, "released", ActorRef(kind="candidate", id=candidate_id), reason="contractor_unavailable"
    )
    return await portal_state(db, tenant_id, candidate_id)
