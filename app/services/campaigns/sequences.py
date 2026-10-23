# ruff: file-ignore[line-too-long, unused-function-argument]
"""Nurture Sequences (Epic 39 A7, FR-199, story 39.5; PRD 5.3).

Steps are versioned: the editable draft is ``current_version + 1``; ``publish`` validates the graph (<= 20 steps;
branch targets must be after the branch -- no cycles), makes the draft current and copies it as the next draft.
Enrolments pin the version they started on. One active / paused enrolment per (sequence, candidate) (partial unique
index); a new enrolment within ``re_enrol_gap_days`` of the last one's end is refused (``re_enrol_gap``).

``advance`` (beat every minute, queue ``campaigns``) claims due enrolments ``FOR UPDATE SKIP LOCKED`` and runs steps
until a wait, a deferral or the end: ``send`` creates the ``(enrollment, step)`` message and runs it through
``sending.send_batch`` (governance applies; consent / opt-out suppression ends the enrolment ``suppressed``),
``wait`` re-schedules at the recipient's send-window start ``days`` later, ``branch`` reads engagement, ``task``
creates a task for the owner (or a named user), ``add_to_pool`` adds the candidate to a static Pool.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.campaigns import CHANNELS, CampaignMessage, EngagementEvent, Enrollment, SequenceStep
from app.models.campaigns import Sequence as SequenceModel
from app.models.candidates import Candidate
from app.models.comms import ChannelTemplate
from app.models.pipeline import Application, PipelineStage
from app.schemas.campaigns import (
    EnrollmentOut,
    EnrolOut,
    EnrolRefusal,
    JobLinkIn,
    SequenceCreate,
    SequenceOut,
    SequenceUpdate,
    StepIn,
    StepOut,
)
from app.services.campaigns import audiences, governance, sending, tokens
from app.services.campaigns import settings as mkt_settings
from app.services.campaigns.common import (
    conflict,
    not_found,
    now,
    principal_of,
    system_ctx,
    tenant_of,
    user_id_of,
)

MAX_STEPS = 20
# Epic 78 (Ask First, additive): guards run before a version goes live (``publish``) or a paused sequence resumes;
# called with the steps about to be live (``[{"kind", "config"}]``, position = index + 1). P3 never imports P5.
StepActivationGuard = Callable[[RequestContext, AsyncSession, SequenceModel, list[dict[str, Any]]], Awaitable[None]]
_STEP_ACTIVATION_GUARDS: list[StepActivationGuard] = []


def register_step_activation_guard(fn: StepActivationGuard) -> None:
    """Register a guard (raises to refuse); idempotent per function."""
    if fn not in _STEP_ACTIVATION_GUARDS:
        _STEP_ACTIVATION_GUARDS.append(fn)


async def _run_activation_guards(
    ctx: RequestContext, db: AsyncSession, seq: SequenceModel, steps: list[dict[str, Any]]
) -> None:
    for guard in list(_STEP_ACTIVATION_GUARDS):
        await guard(ctx, db, seq, steps)


BRANCH_ON = ("opened", "clicked", "replied", "applied")
MAX_STEPS_PER_RUN = 25


# ================================================================ validation
def _invalid(path: str, message: str) -> ValidationFailed:
    return ValidationFailed(
        message, code="invalid_sequence", details={"fields": {path: message}, "path": path}
    )


def validate_steps(steps: list[StepIn] | list[dict[str, Any]], *, strict: bool) -> None:
    """Graph + config checks. ``strict`` (publish) also requires content (subject / body / template)."""
    items = [s if isinstance(s, dict) else s.model_dump() for s in steps]
    if len(items) > MAX_STEPS:
        raise _invalid("steps", f"At most {MAX_STEPS} steps")
    if strict and not items:
        raise _invalid("steps", "Add at least one step")
    end = len(items) + 1
    for i, step in enumerate(items, start=1):
        cfg = step.get("config") or {}
        kind = step.get("kind")
        path = f"steps[{i - 1}]"
        if kind == "send":
            channel = cfg.get("channel")
            if channel not in CHANNELS:
                raise _invalid(f"{path}.config.channel", "Pick email, SMS or WhatsApp")
            if (
                strict
                and channel == "email"
                and not (str(cfg.get("subject") or "").strip() and str(cfg.get("body_html") or "").strip())
            ):
                raise _invalid(f"{path}.config", "Email steps need a subject and a body")
            if strict and channel != "email" and not cfg.get("channel_template_id"):
                raise _invalid(f"{path}.config.channel_template_id", "Pick a registered marketing template")
        elif kind == "wait":
            days = cfg.get("days")
            if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 90:
                raise _invalid(f"{path}.config.days", "Wait between 1 and 90 days")
        elif kind == "branch":
            if cfg.get("on") not in BRANCH_ON:
                raise _invalid(f"{path}.config.on", "Branch on opened, clicked, replied or applied")
            within = cfg.get("within_days", 7)
            if not isinstance(within, int) or not 1 <= within <= 90:
                raise _invalid(f"{path}.config.within_days", "Between 1 and 90 days")
            for key in ("then_position", "else_position"):
                target = cfg.get(key)
                if not isinstance(target, int) or isinstance(target, bool) or target <= i or target > end:
                    raise _invalid(f"{path}.config.{key}", "Branches can only jump forward (no cycles)")
        elif kind == "task":
            if not str(cfg.get("title") or "").strip():
                raise _invalid(f"{path}.config.title", "A task needs a title")
            assignee = cfg.get("assignee", "owner")
            if assignee != "owner":
                try:
                    uuid.UUID(str(assignee))
                except ValueError as exc:
                    raise _invalid(f"{path}.config.assignee", "Owner or a user id") from exc
            due = cfg.get("due_days", 1)
            if not isinstance(due, int) or not 0 <= due <= 90:
                raise _invalid(f"{path}.config.due_days", "Between 0 and 90 days")
        elif kind == "add_to_pool":
            try:
                uuid.UUID(str(cfg.get("talent_pool_id")))
            except ValueError as exc:
                raise _invalid(f"{path}.config.talent_pool_id", "Pick a static pool") from exc
        else:
            raise _invalid(f"{path}.kind", "Unknown step kind")


async def _check_refs(db: AsyncSession, steps: list[dict[str, Any]]) -> None:
    from app.models.talent_pools import TalentPool
    from app.services.messaging import templates

    for i, step in enumerate(steps):
        cfg = step.get("config") or {}
        if (
            step.get("kind") == "send"
            and cfg.get("channel") in ("sms", "whatsapp")
            and cfg.get("channel_template_id")
        ):
            t = await db.get(ChannelTemplate, uuid.UUID(str(cfg["channel_template_id"])))
            if (
                t is None
                or t.channel != cfg["channel"]
                or t.purpose != "marketing"
                or not templates.sendable(t)
            ):
                raise conflict(
                    "template_not_registered",
                    "SMS and WhatsApp steps need a registered marketing template",
                    path=f"steps[{i}]",
                )
        if step.get("kind") == "add_to_pool":
            pool = await db.get(TalentPool, uuid.UUID(str(cfg.get("talent_pool_id"))))
            if pool is None or pool.kind != "static":
                raise _invalid(f"steps[{i}].config.talent_pool_id", "Pick a static pool")


async def _normalise(db: AsyncSession, steps: list[StepIn]) -> list[dict[str, Any]]:
    from app.services.campaigns.campaigns import _job_links

    out: list[dict[str, Any]] = []
    for s in steps:
        cfg = dict(s.config)
        if s.kind == "send" and cfg.get("job_links"):
            links = [
                JobLinkIn(posting_id=x.get("posting_id"), label=x.get("label"))
                for x in cfg["job_links"]
                if isinstance(x, dict)
            ]
            cfg["job_links"] = await _job_links(db, links)
        out.append({"kind": s.kind, "config": cfg})
    return out


# ================================================================ CRUD
async def _get(db: AsyncSession, sequence_id: uuid.UUID, *, lock: bool = False) -> SequenceModel:
    stmt = select(SequenceModel).where(SequenceModel.id == sequence_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise not_found("Sequence")
    return row


async def steps_of(db: AsyncSession, sequence_id: uuid.UUID, version: int) -> list[SequenceStep]:
    return list(
        (
            await db.execute(
                select(SequenceStep)
                .where(SequenceStep.sequence_id == sequence_id, SequenceStep.version == version)
                .order_by(SequenceStep.position)
            )
        ).scalars()
    )


async def _write_steps(
    db: AsyncSession, seq: SequenceModel, version: int, steps: list[dict[str, Any]]
) -> None:
    for old in await steps_of(db, seq.id, version):
        await db.delete(old)
    await db.flush()
    for pos, s in enumerate(steps, start=1):
        db.add(
            SequenceStep(
                id=uuid.uuid4(),
                tenant_id=seq.tenant_id,
                sequence_id=seq.id,
                version=version,
                position=pos,
                kind=s["kind"],
                config=s["config"],
            )
        )
    await db.flush()


def _step_out(s: SequenceStep) -> StepOut:
    return StepOut(id=s.id, position=s.position, kind=s.kind, config=dict(s.config or {}))


async def to_out(db: AsyncSession, seq: SequenceModel) -> SequenceOut:
    await db.flush()
    await db.refresh(seq)
    active = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Enrollment)
                .where(Enrollment.sequence_id == seq.id, Enrollment.status.in_(("active", "paused")))
            )
        ).scalar_one()
    )
    return SequenceOut(
        id=seq.id,
        name=seq.name,
        description=seq.description,
        owner_id=seq.owner_id,
        status=seq.status,
        current_version=seq.current_version,
        draft_version=seq.current_version + 1,
        basis=seq.basis,
        exit_on={k: bool(v) for k, v in (seq.exit_on or {}).items()},
        re_enrol_gap_days=seq.re_enrol_gap_days,
        steps=[_step_out(s) for s in await steps_of(db, seq.id, seq.current_version + 1)],
        published_steps=[_step_out(s) for s in await steps_of(db, seq.id, seq.current_version)]
        if seq.current_version
        else [],
        active_enrollments=active,
        created_at=seq.created_at,
        updated_at=seq.updated_at,
    )


async def list_sequences(
    ctx: RequestContext, db: AsyncSession, status: str | None = None
) -> list[SequenceOut]:
    stmt = select(SequenceModel).order_by(SequenceModel.updated_at.desc()).limit(500)
    if status:
        stmt = stmt.where(SequenceModel.status == status)
    return [await to_out(db, s) for s in (await db.execute(stmt)).scalars()]


async def create_sequence(ctx: RequestContext, db: AsyncSession, data: SequenceCreate) -> SequenceOut:
    principal = principal_of(ctx)
    validate_steps(data.steps, strict=False)
    seq = SequenceModel(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        name=data.name,
        description=data.description,
        owner_id=principal.id,
        status="draft",
        current_version=0,
        basis=data.basis,
        exit_on=(
            data.exit_on.model_dump()
            if data.exit_on
            else {"replied": True, "applied": True, "hired": True, "unsubscribed": True}
        ),
        re_enrol_gap_days=data.re_enrol_gap_days,
    )
    db.add(seq)
    await db.flush()
    await _write_steps(db, seq, 1, await _normalise(db, data.steps))
    await db.refresh(seq)
    return await to_out(db, seq)


async def get_sequence(ctx: RequestContext, db: AsyncSession, sequence_id: uuid.UUID) -> SequenceOut:
    return await to_out(db, await _get(db, sequence_id))


async def update_sequence(
    ctx: RequestContext, db: AsyncSession, sequence_id: uuid.UUID, data: SequenceUpdate
) -> SequenceOut:
    seq = await _get(db, sequence_id, lock=True)
    if seq.status == "archived":
        raise conflict("sequence_archived", "The sequence is archived")
    if data.name is not None:
        seq.name = data.name
    if data.description is not None:
        seq.description = data.description or None
    if data.basis is not None:
        seq.basis = data.basis
    if data.exit_on is not None:
        seq.exit_on = data.exit_on.model_dump()
    if data.re_enrol_gap_days is not None:
        seq.re_enrol_gap_days = data.re_enrol_gap_days
    if data.steps is not None:
        validate_steps(data.steps, strict=False)
        await _write_steps(db, seq, seq.current_version + 1, await _normalise(db, data.steps))
    seq.updated_at = now()
    await db.flush()
    await db.refresh(seq)
    return await to_out(db, seq)


async def publish(
    ctx: RequestContext, db: AsyncSession, sequence_id: uuid.UUID, note: str | None = None
) -> SequenceOut:
    seq = await _get(db, sequence_id, lock=True)
    if seq.status == "archived":
        raise conflict("sequence_archived", "The sequence is archived")
    draft = seq.current_version + 1
    steps = [{"kind": s.kind, "config": dict(s.config or {})} for s in await steps_of(db, seq.id, draft)]
    validate_steps(steps, strict=True)
    await _check_refs(db, steps)
    await _run_activation_guards(ctx, db, seq, steps)
    seq.current_version = draft
    if seq.status == "draft":
        seq.status = "active"
    await _write_steps(db, seq, draft + 1, steps)
    seq.updated_at = now()
    await db.flush()
    audit.record(
        "sequence.published",
        "sequence",
        seq.id,
        session=db,
        version=draft,
        note=(note or "")[:500],
        by=str(user_id_of(ctx)),
    )
    await db.refresh(seq)
    return await to_out(db, seq)


async def set_status(ctx: RequestContext, db: AsyncSession, sequence_id: uuid.UUID, to: str) -> SequenceOut:
    seq = await _get(db, sequence_id, lock=True)
    allowed = {"pause": ("active",), "resume": ("paused",), "archive": ("draft", "active", "paused")}[to]
    if seq.status not in allowed:
        raise conflict("invalid_transition", f"A {seq.status} sequence cannot {to}", status=seq.status)
    if to == "resume" and _STEP_ACTIVATION_GUARDS:
        live = [{"kind": x.kind, "config": dict(x.config or {})} for x in await steps_of(db, seq.id, seq.current_version)]
        await _run_activation_guards(ctx, db, seq, live)
    seq.status = {"pause": "paused", "resume": "active", "archive": "archived"}[to]
    if to == "archive":
        rows = (
            await db.execute(
                select(Enrollment).where(
                    Enrollment.sequence_id == seq.id, Enrollment.status.in_(("active", "paused"))
                )
            )
        ).scalars()
        for e in rows:
            e.status, e.exit_reason, e.ended_at, e.next_run_at = "exited", "manual", now(), None
    seq.updated_at = now()
    await db.flush()
    await db.refresh(seq)
    return await to_out(db, seq)


# ================================================================ enrolment
async def _live_application(db: AsyncSession, candidate_id: uuid.UUID) -> uuid.UUID | None:
    return (
        await db.execute(
            select(Application.id)
            .join(PipelineStage, PipelineStage.id == Application.stage_id)
            .where(
                Application.candidate_id == candidate_id,
                Application.archived_at.is_(None),
                PipelineStage.key.not_in(governance.TERMINAL_STAGE_KEYS),
            )
            .order_by(Application.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def enrol(
    ctx: RequestContext,
    db: AsyncSession,
    sequence_id: uuid.UUID,
    candidate_ids: list[uuid.UUID] | None = None,
    *,
    origin: str = "user",
    rule_id: uuid.UUID | None = None,
    audience_id: uuid.UUID | None = None,
    single_conflict: bool = True,
) -> EnrolOut:
    seq = await _get(db, sequence_id)
    if seq.status != "active" or seq.current_version < 1:
        raise conflict(
            "sequence_not_active", "Publish the sequence before enrolling candidates", status=seq.status
        )
    snapshot_id: uuid.UUID | None = None
    ids = list(dict.fromkeys(candidate_ids or []))
    if audience_id is not None:
        snapshot_id = await audiences.freeze(ctx, db, audience_id, "enrollment")
        ids = list(dict.fromkeys([*ids, *await audiences.snapshot_member_ids(db, snapshot_id)]))
    if not ids:
        raise ValidationFailed("Pick candidates or an audience to enrol", code="enrol_empty")
    if origin == "user":
        allowed = await audiences._scoped(ctx, db, ids)
        missing = [i for i in ids if i not in allowed]
        if missing:
            from app.core.errors import NotFound

            raise NotFound("Candidate not found", details={"candidate_id": str(missing[0])})
    refused: list[EnrolRefusal] = []
    created: list[uuid.UUID] = []
    gap = timedelta(days=seq.re_enrol_gap_days)
    current = now()
    for cid in ids:
        existing = (
            await db.execute(
                select(Enrollment)
                .where(Enrollment.sequence_id == seq.id, Enrollment.candidate_id == cid)
                .order_by(Enrollment.started_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None and existing.status in ("active", "paused"):
            refused.append(EnrolRefusal(candidate_id=cid, reason="already_enrolled"))
            continue
        if existing is not None and existing.ended_at is not None and existing.ended_at > current - gap:
            refused.append(EnrolRefusal(candidate_id=cid, reason="re_enrol_gap"))
            continue
        enrollment = Enrollment(
            id=uuid.uuid4(),
            tenant_id=seq.tenant_id,
            sequence_id=seq.id,
            version=seq.current_version,
            candidate_id=cid,
            status="active",
            position=1,
            next_run_at=current,
            application_id=await _live_application(db, cid) if seq.basis == "own_application" else None,
            origin=origin,
            workflow_rule_id=rule_id,
            snapshot_id=snapshot_id,
            enrolled_by=user_id_of(ctx),
            started_at=current,
        )
        try:
            async with db.begin_nested():
                db.add(enrollment)
                await db.flush()
        except IntegrityError:
            refused.append(EnrolRefusal(candidate_id=cid, reason="already_enrolled"))
            continue
        created.append(enrollment.id)
    if single_conflict and len(ids) == 1 and refused:
        raise conflict(
            refused[0].reason,
            "The candidate cannot be enrolled in this sequence now",
            candidate_id=str(ids[0]),
        )
    return EnrolOut(enrolled=len(created), enrollment_ids=created, refused=refused)


async def _enrollment_out(db: AsyncSession, e: Enrollment) -> EnrollmentOut:
    await db.flush()
    await db.refresh(e)
    name = (
        await db.execute(select(Candidate.full_name).where(Candidate.id == e.candidate_id))
    ).scalar_one_or_none()
    return EnrollmentOut(
        id=e.id,
        sequence_id=e.sequence_id,
        candidate_id=e.candidate_id,
        candidate_name=name,
        version=e.version,
        status=e.status,
        exit_reason=e.exit_reason,
        position=e.position,
        next_run_at=e.next_run_at,
        origin=e.origin,
        started_at=e.started_at,
        ended_at=e.ended_at,
    )


async def list_enrollments(
    ctx: RequestContext, db: AsyncSession, sequence_id: uuid.UUID, status: str | None = None
) -> list[EnrollmentOut]:
    await _get(db, sequence_id)
    stmt = (
        select(Enrollment)
        .where(Enrollment.sequence_id == sequence_id)
        .order_by(Enrollment.started_at.desc())
        .limit(1000)
    )
    if status:
        stmt = stmt.where(Enrollment.status == status)
    return [await _enrollment_out(db, e) for e in (await db.execute(stmt)).scalars()]


async def enrollment_action(
    ctx: RequestContext, db: AsyncSession, enrollment_id: uuid.UUID, action: str
) -> EnrollmentOut:
    e = (
        await db.execute(select(Enrollment).where(Enrollment.id == enrollment_id).with_for_update())
    ).scalar_one_or_none()
    if e is None:
        raise not_found("Enrolment")
    if action == "pause":
        if e.status != "active":
            raise conflict("invalid_transition", f"A {e.status} enrolment cannot be paused", status=e.status)
        e.status = "paused"
    elif action == "resume":
        if e.status != "paused":
            raise conflict("invalid_transition", f"A {e.status} enrolment cannot be resumed", status=e.status)
        e.status = "active"
        e.next_run_at = e.next_run_at or now()
    else:
        if e.status not in ("active", "paused"):
            raise conflict("invalid_transition", f"A {e.status} enrolment cannot exit", status=e.status)
        from app.services.campaigns import engagement

        await engagement.exit_enrollments(db, e.candidate_id, "manual", only=e.id)
    await db.flush()
    await db.refresh(e)
    return await _enrollment_out(db, e)


# ================================================================ beat: advance
async def _wait_until(db: AsyncSession, e: Enrollment, days: int, current: datetime) -> datetime:
    """``days`` later, aligned to the recipient's email send-window start (recipient time zone)."""
    row = await mkt_settings.get_row(db)
    location = (
        await db.execute(select(Candidate.location).where(Candidate.id == e.candidate_id))
    ).scalar_one_or_none()
    tz = mkt_settings.zone_for_country(
        (location or {}).get("country") if isinstance(location, dict) else None, row.default_timezone
    )
    start, _ = mkt_settings.effective_windows(row)["email"]
    local_day = (current.astimezone(tz) + timedelta(days=days)).date()
    return datetime.combine(local_day, start, tzinfo=tz).astimezone(current.tzinfo)


async def _branch_hit(db: AsyncSession, e: Enrollment, cfg: dict[str, Any], current: datetime) -> bool:
    kind = str(cfg.get("on"))
    since = current - timedelta(days=int(cfg.get("within_days") or 7))
    stmt = select(EngagementEvent.id).where(
        EngagementEvent.kind == kind, EngagementEvent.occurred_at >= since
    )
    if kind == "applied":
        stmt = stmt.where(EngagementEvent.candidate_id == e.candidate_id)
    else:
        stmt = stmt.join(CampaignMessage, CampaignMessage.id == EngagementEvent.campaign_message_id).where(
            CampaignMessage.enrollment_id == e.id
        )
    return (await db.execute(stmt.limit(1))).first() is not None


async def _task(
    db: AsyncSession, seq: SequenceModel, e: Enrollment, cfg: dict[str, Any], current: datetime
) -> None:
    from app.core.permissions import Principal, Scope
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    assignee = cfg.get("assignee", "owner")
    owner = seq.owner_id if assignee == "owner" else uuid.UUID(str(assignee))
    acting = Principal(
        type="user",
        id=owner,
        tenant_id=seq.tenant_id,
        permissions={"task:create": Scope.TENANT, "candidate:read": Scope.TENANT},
    )
    ctx = RequestContext(tenant_id=seq.tenant_id, principal=acting, actor_kind="system")
    await tasks_service.create_task(
        ctx,
        db,
        TaskCreate(
            title=str(cfg.get("title"))[:200],
            owner_id=owner,
            entity_type="candidate",
            entity_id=e.candidate_id,
            due_at=current + timedelta(days=int(cfg.get("due_days") or 1)),
        ),
        source="workflow",
    )


async def _add_to_pool(db: AsyncSession, e: Enrollment, cfg: dict[str, Any]) -> None:
    from app.models.talent_pools import TalentPool
    from app.services.talent_pools import members

    pool = await db.get(TalentPool, uuid.UUID(str(cfg.get("talent_pool_id"))))
    if pool is None or pool.kind != "static" or pool.status != "active":
        return
    await members.add_members(
        system_ctx(e.tenant_id), db, pool, [e.candidate_id], source="workflow", note="nurture sequence"
    )


async def _run_send(db: AsyncSession, e: Enrollment, step: SequenceStep, current: datetime) -> str:
    """``advance`` | ``wait`` (deferred / throttled) | ``stop`` (enrolment ended)."""
    channel = str((step.config or {}).get("channel") or "email")
    await db.execute(
        pg_insert(CampaignMessage)
        .values(
            id=uuid.uuid4(),
            tenant_id=e.tenant_id,
            enrollment_id=e.id,
            step_id=step.id,
            candidate_id=e.candidate_id,
            channel=channel,
            status="pending",
            token_nonce=tokens.new_nonce(),
        )
        .on_conflict_do_nothing(
            index_elements=["enrollment_id", "step_id"],
            index_where=CampaignMessage.enrollment_id.is_not(None),
        )
    )
    cm = (
        await db.execute(
            select(CampaignMessage).where(
                CampaignMessage.enrollment_id == e.id, CampaignMessage.step_id == step.id
            )
        )
    ).scalar_one()
    if cm.status in ("pending", "deferred"):
        await sending.send_batch(db, [cm.id], now=current)
        await db.refresh(cm)
        await db.refresh(e)
    if e.status != "active":
        return "stop"
    if cm.status in ("queued", "sent", "delivered", "bounced", "failed", "suppressed", "cancelled"):
        return "advance"
    e.next_run_at = cm.defer_until or current + timedelta(minutes=1)
    return "wait"


async def run_enrollment(db: AsyncSession, e: Enrollment, current: datetime) -> None:
    seq = await db.get(SequenceModel, e.sequence_id)
    if seq is None or seq.status != "active":
        return
    steps = {s.position: s for s in await steps_of(db, seq.id, e.version)}
    for _ in range(MAX_STEPS_PER_RUN):
        step = steps.get(e.position)
        if step is None:
            e.status, e.ended_at, e.next_run_at = "completed", current, None
            return
        cfg = dict(step.config or {})
        if step.kind == "send":
            outcome = await _run_send(db, e, step, current)
            if outcome != "advance":
                return
            e.position += 1
        elif step.kind == "wait":
            e.position += 1
            e.next_run_at = await _wait_until(db, e, int(cfg.get("days") or 1), current)
            return
        elif step.kind == "branch":
            hit = await _branch_hit(db, e, cfg, current)
            e.position = int(cfg["then_position"] if hit else cfg["else_position"])
        elif step.kind == "task":
            await _task(db, seq, e, cfg, current)
            e.position += 1
        elif step.kind == "add_to_pool":
            await _add_to_pool(db, e, cfg)
            e.position += 1
        e.next_run_at = current
    e.next_run_at = current + timedelta(minutes=1)


async def advance(db: AsyncSession, at: datetime | None = None, limit: int = 1000) -> int:
    current = at or now()
    rows = list(
        (
            await db.execute(
                select(Enrollment)
                .join(SequenceModel, SequenceModel.id == Enrollment.sequence_id)
                .where(
                    Enrollment.status == "active",
                    Enrollment.next_run_at <= current,
                    SequenceModel.status == "active",
                )
                .order_by(Enrollment.next_run_at)
                .with_for_update(of=Enrollment, skip_locked=True)
                .limit(limit)
            )
        ).scalars()
    )
    for e in rows:
        async with db.begin_nested():
            await run_enrollment(db, e, current)
    await db.flush()
    return len(rows)
