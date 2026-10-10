# ruff: noqa: E501, ARG001
"""Re-engagement triggers (Epic 39 A8, FR-200, story 39.6).

* Workflow action ``campaigns.enrol_in_sequence`` (contract key, Epic 36 catalogue): params ``{sequence_id,
  daily_cap 1..5000 (default 200)}``; trigger entities ``candidate`` / ``application`` / ``placement`` (resolved to
  their candidate) and ``talent_pool`` (the members added by the refresh, via ``pools_service.member_ids(change=
  "added", refresh_id=...)``). Runs as actor ``workflow`` through ``sequences.enrol`` (governance applies at send).
  The per-rule daily cap is ``marketing_trigger_caps`` (``UPDATE ... SET count = count + 1 WHERE count < cap``);
  over the cap the result is ``skipped: daily_cap``.
* Triggers: ``campaigns.candidate_dormant`` (daily beat, last activity crossed 3 / 6 / 12 months today -- rules
  filter ``event.months``, default 6), ``campaigns.silver_medalist_matched`` (``RediscoveryCompleted`` x the Epic 38
  "Silver Medalists" Pool x stored matches >= 70), ``campaigns.pool_members_added`` (``PoolMembershipChanged`` with
  ``added_count > 0``). Ending placements reuse the P2 ``placements.ending_soon`` trigger.
"""

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError, ValidationFailed
from app.core.events import emit
from app.models.campaigns import Sequence as SequenceModel
from app.models.candidates import Candidate
from app.services.campaigns.common import as_uuid, now, system_ctx
from app.services.campaigns.events import CandidateDormant, SilverMedalistMatched

ACTION_KEY = "campaigns.enrol_in_sequence"
ENTITY_TYPES = ("candidate", "application", "placement", "talent_pool")
DORMANT_MONTHS = (3, 6, 12)
SILVER_TEMPLATE = "silver_medalists"
SILVER_MIN_SCORE = 70


class EnrolParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence_id: uuid.UUID
    daily_cap: int = Field(default=200, ge=1, le=5000)


def _invalid(path: str, reason: str) -> ValidationFailed:
    return ValidationFailed(
        f"Invalid action at {path}: {reason}", code="invalid_action", details={"path": path, "reason": reason}
    )


def _validate(params: Any, entity_type: str, path: str) -> None:
    if entity_type not in ENTITY_TYPES:
        raise _invalid(path, "only for candidate, application, placement or talent pool triggers")


async def _check(db: AsyncSession, params: Any, path: str) -> None:
    seq = await db.get(SequenceModel, params.sequence_id)
    if seq is None or seq.status == "archived":
        raise _invalid(f"{path}.sequence_id", "unknown_sequence")


async def _candidates(ac: Any) -> list[uuid.UUID]:
    if ac.entity_type == "candidate":
        return [uuid.UUID(str(ac.entity_id))]
    if ac.entity_type == "talent_pool":
        from app.services import pools_service

        refresh_id = as_uuid((ac.snapshot or {}).get("event.refresh_id"))
        if refresh_id is None:
            return []
        return await pools_service.member_ids(
            system_ctx(ac.tenant_id),
            ac.db,
            uuid.UUID(str(ac.entity_id)),
            change="added",
            refresh_id=refresh_id,
        )
    cid = as_uuid((ac.snapshot or {}).get("candidate_id"))
    if cid is not None:
        return [cid]
    if ac.entity_type == "application":
        from app.models.pipeline import Application

        found = (
            await ac.db.execute(select(Application.candidate_id).where(Application.id == ac.entity_id))
        ).scalar_one_or_none()
    else:
        from app.models.placements import Placement

        found = (
            await ac.db.execute(select(Placement.candidate_id).where(Placement.id == ac.entity_id))
        ).scalar_one_or_none()
    return [found] if found else []


async def _tenant_day(db: AsyncSession, tenant_id: uuid.UUID) -> date:
    from app.services import directory_service
    from app.services.campaigns import settings as mkt_settings

    return now().astimezone(mkt_settings.zone(await directory_service.tenant_timezone(db, tenant_id))).date()


async def take_cap(db: AsyncSession, tenant_id: uuid.UUID, rule_id: uuid.UUID, cap: int) -> bool:
    day = await _tenant_day(db, tenant_id)
    await db.execute(
        text(
            "INSERT INTO marketing_trigger_caps (tenant_id, rule_id, day, count) VALUES (:t, :r, :d, 0) "
            "ON CONFLICT (tenant_id, rule_id, day) DO NOTHING"
        ),
        {"t": tenant_id, "r": rule_id, "d": day},
    )
    row = (
        await db.execute(
            text(
                "UPDATE marketing_trigger_caps SET count = count + 1 "
                "WHERE tenant_id = :t AND rule_id = :r AND day = :d AND count < :cap RETURNING count"
            ),
            {"t": tenant_id, "r": rule_id, "d": day, "cap": cap},
        )
    ).first()
    return row is not None


async def release_cap(db: AsyncSession, tenant_id: uuid.UUID, rule_id: uuid.UUID) -> None:
    day = await _tenant_day(db, tenant_id)
    await db.execute(
        text(
            "UPDATE marketing_trigger_caps SET count = count - 1 WHERE tenant_id = :t AND rule_id = :r AND day = :d AND count > 0"
        ),
        {"t": tenant_id, "r": rule_id, "d": day},
    )


async def _run(ac: Any) -> str:
    from app.services.campaigns import sequences
    from app.services.workflow.actions import ActionError

    params: EnrolParams = ac.params
    seq = await ac.db.get(SequenceModel, params.sequence_id)
    if seq is None:
        raise ActionError("sequence_not_found", "The sequence no longer exists")
    ctx = system_ctx(ac.tenant_id, "workflow", ac.rule_id)
    enrolled = skipped_cap = refused = 0
    for cid in await _candidates(ac):
        if ac.rule_id is not None and not await take_cap(ac.db, ac.tenant_id, ac.rule_id, params.daily_cap):
            skipped_cap += 1
            continue
        try:
            out = await sequences.enrol(
                ctx, ac.db, seq.id, [cid], origin="workflow", rule_id=ac.rule_id, single_conflict=False
            )
        except DomainError as exc:
            if ac.rule_id is not None:
                await release_cap(ac.db, ac.tenant_id, ac.rule_id)
            raise ActionError(exc.code, exc.message) from exc
        if out.enrolled:
            enrolled += 1
        else:
            refused += 1
            if ac.rule_id is not None:
                await release_cap(ac.db, ac.tenant_id, ac.rule_id)
    if skipped_cap and not enrolled:
        return "skipped: daily_cap"
    parts = [f"Enrolled {enrolled} in sequence {seq.name}"]
    if refused:
        parts.append(f"{refused} not enrolled (already enrolled or re-enrolment gap)")
    if skipped_cap:
        parts.append(f"{skipped_cap} skipped: daily_cap")
    return "; ".join(parts)


async def _dry(ac: Any) -> str:
    seq = await ac.db.get(SequenceModel, ac.params.sequence_id)
    return f"Would enrol the candidate in sequence {seq.name if seq else ac.params.sequence_id}"


def register_action() -> None:
    from app.services.workflow.actions import ACTIONS, ActionDef
    from app.services.workflow.actions import register_action as _register

    if ACTION_KEY in ACTIONS:
        return
    _register(
        ACTION_KEY,
        ActionDef(
            "Enrol in sequence", EnrolParams, _validate, _run, _dry, entity_types=ENTITY_TYPES, check=_check
        ),
    )


def register_triggers() -> None:
    from app.services.workflow import registry
    from app.services.workflow.conditions import FieldDef

    defs = (
        registry.Trigger(
            key="campaigns.candidate_dormant",
            label="Candidate became dormant",
            event_module="app.services.campaigns.events",
            event_class="CandidateDormant",
            entity_type="candidate",
            entity_attr="candidate_id",
            extras={"months": ("months", FieldDef("int", "Months since last activity"))},
        ),
        registry.Trigger(
            key="campaigns.silver_medalist_matched",
            label="Silver medalist matched a new demand",
            event_module="app.services.campaigns.events",
            event_class="SilverMedalistMatched",
            entity_type="candidate",
            entity_attr="candidate_id",
            extras={
                "score": ("score", FieldDef("int", "Match score")),
                "demand_id": ("demand_id", FieldDef("uuid", "Demand")),
            },
        ),
        registry.Trigger(
            key="campaigns.pool_members_added",
            label="Candidates joined a talent pool",
            event_module="app.events.talent_pools",
            event_class="PoolMembershipChanged",
            entity_type="talent_pool",
            entity_attr="pool_id",
            extras={
                "added_count": ("added_count", FieldDef("int", "Members added")),
                "refresh_id": ("refresh_id", FieldDef("uuid", "Refresh")),
            },
            matches=lambda e: int(getattr(e, "added_count", 0) or 0) > 0,
        ),
    )
    for trigger in defs:
        if trigger.key not in registry.TRIGGERS:
            registry.register_trigger(trigger.key, trigger)


# ================================================================ beats / handlers
def _months_ago(day: date, months: int) -> date:
    y, m = divmod(day.month - 1 - months, 12)
    year, month = day.year + y, m + 1
    import calendar

    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


async def detect_dormant(db: AsyncSession, tenant_id: uuid.UUID, today: date | None = None) -> int:
    """Once per tenant-local day: emit ``CandidateDormant`` for candidates whose last activity (max of the profile
    update and their latest application) fell exactly 3 / 6 / 12 months before today (one crossing each)."""
    from app.models.pipeline import Application
    from app.services.campaigns import settings as mkt_settings

    row = await mkt_settings.get_row(db)
    day = today or await _tenant_day(db, tenant_id)
    if row.last_dormant_scan_on == day:
        return 0
    row.last_dormant_scan_on = day
    last_app = (
        select(Application.candidate_id, func.max(Application.created_at).label("at"))
        .group_by(Application.candidate_id)
        .subquery()
    )
    last_activity = func.greatest(Candidate.updated_at, func.coalesce(last_app.c.at, Candidate.updated_at))
    emitted = 0
    for months in DORMANT_MONTHS:
        target = _months_ago(day, months)
        rows = await db.execute(
            select(Candidate.id)
            .outerjoin(last_app, last_app.c.candidate_id == Candidate.id)
            .where(
                Candidate.erased_at.is_(None),
                Candidate.merged_into_id.is_(None),
                Candidate.archived_at.is_(None),
                func.date(last_activity) == target,
            )
            .limit(50_000)
        )
        for cid in rows.scalars():
            emit(db, CandidateDormant(tenant_id=tenant_id, candidate_id=cid, months=months))
            emitted += 1
    await db.flush()
    return emitted


async def silver_medalists_matched(db: AsyncSession, tenant_id: uuid.UUID, demand_id: uuid.UUID) -> int:
    """``RediscoveryCompleted``: Silver Medalists pool members with a stored match >= 70 -> ``SilverMedalistMatched``."""
    from app.models.matching import Match
    from app.models.talent_pools import TalentPool
    from app.services import pools_service

    pool_ids = list(
        (
            await db.execute(
                select(TalentPool.id).where(
                    TalentPool.template_key == SILVER_TEMPLATE, TalentPool.status == "active"
                )
            )
        ).scalars()
    )
    members: set[uuid.UUID] = set()
    for pid in pool_ids:
        members.update(await pools_service.member_ids(system_ctx(tenant_id), db, pid))
    if not members:
        return 0
    rows = await db.execute(
        select(Match.candidate_id, Match.overall).where(
            Match.demand_id == demand_id,
            Match.candidate_id.in_(sorted(members)),
            Match.overall >= SILVER_MIN_SCORE,
        )
    )
    emitted = 0
    for cid, overall in rows.all():
        emit(
            db,
            SilverMedalistMatched(
                tenant_id=tenant_id, candidate_id=cid, demand_id=demand_id, score=int(overall)
            ),
        )
        emitted += 1
    return emitted


def last_activity_cutoff(months: int, at: datetime | None = None) -> date:
    return _months_ago((at or now()).date(), months) - timedelta(days=0)
