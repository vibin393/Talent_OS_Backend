# ruff: noqa: E501, ARG001
"""Onboarding cases (Epic 23 A4 / A6, P2-10.1 / P2-10.3): creation from ``PlacementCreated``, the compliance
merge, invite, cancel, progress recompute, the board and the start-gate resolver.

* ``handle_placement_created`` is idempotent (UNIQUE ``placement_id``; an initialised case is returned as is).
* ``handle_requirements`` stores compliance items on a holding row (``initialized_at`` NULL) when the case does not
  exist yet, so handler order does not matter. Items for a Complete case are added as optional and HR is
  notified; the case is never reopened.
"""

import uuid
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound
from app.core.events import emit
from app.core.sequences import next_human_id
from app.events.onboarding import (
    OnboardingCaseCancelled,
    OnboardingCaseCompleted,
    OnboardingCaseCreated,
    OnboardingProgressChanged,
)
from app.models.onboarding import OnboardingCase, OnboardingTask, OnboardingTemplate
from app.schemas.onboarding import BoardRow, CaseOut, InviteOut, TaskOut, UnmatchedPlacement
from app.services.onboarding import matching, notify, reuse
from app.services.onboarding.common import (
    DONE_TASK_STATUSES,
    TERMINAL_CASE,
    case_tasks,
    get_settings_row,
    is_overdue,
    load_case,
    now,
    tenant_of,
    today,
)
from app.services.onboarding.status import check_case
from app.services.onboarding.templates import snapshot, template_tasks

PRE_START = ("pre_joining", "joining_confirmed")


def system_ctx(db: AsyncSession) -> RequestContext:
    return RequestContext(tenant_id=tenant_of(db), actor_kind="system")


# ---------------------------------------------------------------- placement facts
async def placement_facts(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID
) -> matching.PlacementFacts:
    from app.services import legal_entities_service, placements_service

    p = await placements_service.get_placement_summary(ctx, db, placement_id)
    country: str | None = None
    try:
        le = await legal_entities_service.get_entity(ctx, db, p.legal_entity_id)
        country = getattr(le, "country", None)
    except Exception:
        logger.debug("legal entity {} not readable for onboarding matching", p.legal_entity_id)
    return matching.PlacementFacts(
        client_id=p.client_id, legal_entity_id=p.legal_entity_id, worker_type=p.worker_type, country=country
    )


async def select_template(db: AsyncSession, facts: matching.PlacementFacts) -> OnboardingTemplate | None:
    rows = (
        await db.execute(
            select(OnboardingTemplate).where(
                OnboardingTemplate.active.is_(True), OnboardingTemplate.archived_at.is_(None)
            )
        )
    ).scalars()
    return matching.select(list(rows), facts)


async def _placement_row(db: AsyncSession, placement_id: uuid.UUID) -> Any:
    from app.models.placements import Placement

    return await db.get(Placement, placement_id)


# ---------------------------------------------------------------- case rows
async def _case_row(
    db: AsyncSession, placement_id: uuid.UUID, candidate_id: uuid.UUID, *, create: bool
) -> OnboardingCase | None:
    if create:
        await db.execute(
            pg_insert(OnboardingCase)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_of(db),
                placement_id=placement_id,
                candidate_id=candidate_id,
                status="not_started",
                progress_pct=0,
                pending_requirements={},
            )
            .on_conflict_do_nothing(constraint="uq_onboarding_cases_placement_id")
        )
    return (
        await db.execute(
            select(OnboardingCase)
            .where(OnboardingCase.placement_id == placement_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _hr_users(db: AsyncSession, case: OnboardingCase | None = None) -> list[uuid.UUID]:
    from app.services import entity_refs

    settings = await get_settings_row(db)
    ids: list[uuid.UUID] = []
    for uid in (case.owner_user_id if case else None, settings.default_hr_owner_id):
        if uid and uid not in ids:
            ids.append(uid)
    if not ids:
        ids = sorted(await entity_refs.role_user_ids(db, "hr_onboarding"))
    return ids


async def _resolve_owner(
    db: AsyncSession, case: OnboardingCase, owner_kind: str | None, role_key: str | None, placement: Any
) -> uuid.UUID | None:
    from app.services import entity_refs

    settings = await get_settings_row(db)
    candidate: uuid.UUID | None = None
    if owner_kind == "role" and role_key:
        raw = (settings.default_owner_by_role or {}).get(role_key)
        candidate = uuid.UUID(str(raw)) if raw else None
        if candidate is None:
            holders = sorted(await entity_refs.role_user_ids(db, role_key))
            candidate = holders[0] if holders else None
    elif owner_kind == "placement_account_manager" and placement is not None:
        candidate = placement.account_manager_user_id
    elif owner_kind == "placement_recruiter" and placement is not None:
        candidate = placement.recruiter_user_id
    if candidate is not None and await entity_refs.active_user_ids(db, [candidate]):
        return candidate
    return case.owner_user_id


async def _create_p1_task(
    ctx: RequestContext, db: AsyncSession, case: OnboardingCase, task: OnboardingTask, placement: Any
) -> None:
    from datetime import UTC, datetime, time

    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    owner = task.owner_user_id
    if owner is None:
        return
    due_at = datetime.combine(task.due_on, time(12, 0), tzinfo=UTC) if task.due_on else None
    human = getattr(placement, "human_id", None) or "the placement"
    try:
        async with db.begin_nested():
            out = await tasks_service.create_system_task(
                ctx,
                db,
                TaskCreate(
                    title=f"Onboarding: {task.label}"[:200],
                    notes=f"Onboarding {case.human_id} for {human}. Complete this task when done.",
                    owner_id=owner,
                    entity_type="candidate",
                    entity_id=case.candidate_id,
                    due_at=due_at,
                    due_all_day=due_at is not None,
                ),
                source="onboarding",
            )
            task.p1_task_id = out.id
    except Exception:
        logger.exception("could not create the internal task for onboarding task {}", task.key)


async def add_task(
    ctx: RequestContext,
    db: AsyncSession,
    case: OnboardingCase,
    spec: Mapping[str, Any],
    *,
    source: str,
    position: int,
    planned_start: date | None,
    placement: Any,
    default_days: int,
) -> OnboardingTask | None:
    exists = (
        await db.execute(
            select(OnboardingTask.id).where(
                OnboardingTask.case_id == case.id, OnboardingTask.key == spec["key"]
            )
        )
    ).first()
    if exists:
        return None
    kind = spec["kind"]
    offset = int(spec.get("due_offset_days") or 0)
    task = OnboardingTask(
        tenant_id=case.tenant_id,
        case_id=case.id,
        candidate_id=case.candidate_id,
        key=spec["key"],
        position=position,
        kind=kind,
        label=spec["label"],
        instructions=spec.get("instructions"),
        mandatory=bool(spec.get("mandatory", True)),
        assignee="user" if kind == "internal" else "worker",
        due_on=planned_start + timedelta(days=offset) if planned_start else None,
        source=source,
        status="pending",
        auto_approve=bool(spec.get("auto_approve", False)),
        reusable=bool(spec.get("reusable", False)) and kind != "esign",
        validity_days=spec.get("validity_days"),
        config=dict(spec.get("config") or {}),
        document_ids=[],
    )
    db.add(task)
    await db.flush()
    if kind == "internal":
        task.owner_user_id = await _resolve_owner(
            db, case, spec.get("owner_kind"), spec.get("owner_role_key"), placement
        )
        await _create_p1_task(ctx, db, case, task, placement)
    elif not await reuse.apply(ctx, db, task, default_days):
        await reuse.link_valid_credential(ctx, db, task)
    await db.flush()
    return task


def _compliance_specs(items: Mapping[str, Any], *, mandatory: bool) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for tid in items.get("policy_template_ids") or []:
        specs.append(
            {
                "key": f"policy:{tid}",
                "kind": "policy_ack",
                "label": "Read and accept the policy",
                "mandatory": mandatory,
                "config": {"document_template_id": str(tid)},
                "reusable": True,
            }
        )
    for key in items.get("credential_type_keys") or []:
        specs.append(
            {
                "key": f"credential:{key}",
                "kind": "credential",
                "label": f"Provide credential: {str(key).replace('_', ' ')}",
                "mandatory": mandatory,
                "config": {"credential_type_key": str(key)},
                "reusable": True,
            }
        )
    return specs


async def _policy_labels(db: AsyncSession, specs: list[dict[str, Any]]) -> None:
    from app.services import documents_service

    for spec in specs:
        if spec["kind"] != "policy_ack":
            continue
        try:
            t = await documents_service.templates.get_template_row(
                db, uuid.UUID(str(spec["config"]["document_template_id"]))
            )
            spec["label"] = f"Read and accept: {t.name}"[:200]
        except Exception:
            logger.debug("policy template {} not readable", spec["config"]["document_template_id"])


async def _add_compliance_items(
    ctx: RequestContext, db: AsyncSession, case: OnboardingCase, items: Mapping[str, Any], *, mandatory: bool
) -> list[OnboardingTask]:
    placement = await _placement_row(db, case.placement_id)
    settings = await get_settings_row(db)
    specs = _compliance_specs(items, mandatory=mandatory)
    await _policy_labels(db, specs)
    pos = len(await case_tasks(db, case.id))
    added: list[OnboardingTask] = []
    for spec in specs:
        t = await add_task(
            ctx,
            db,
            case,
            spec,
            source="compliance",
            position=pos,
            planned_start=getattr(placement, "planned_start_date", None),
            placement=placement,
            default_days=settings.reuse_validity_days,
        )
        if t is not None:
            added.append(t)
            pos += 1
    return added


# ---------------------------------------------------------------- handlers
async def handle_placement_created(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID
) -> OnboardingCase | None:
    placement = await _placement_row(db, placement_id)
    if placement is None:
        return None
    case = await _case_row(db, placement_id, placement.candidate_id, create=False)
    if case is not None and case.initialized_at is not None:
        return case
    if placement.status not in PRE_START and placement.status != "active":
        return None
    facts = await placement_facts(ctx, db, placement_id)
    template = await select_template(db, facts)
    pending = dict(case.pending_requirements or {}) if case is not None else {}
    has_pending = any(pending.get(k) for k in ("policy_template_ids", "credential_type_keys"))
    if template is None and not has_pending:
        await notify.to_users(
            db,
            notify.NO_TEMPLATE,
            await _hr_users(db),
            f"No onboarding template matches placement {placement.human_id} -- start onboarding manually",
            placement_id=placement_id,
        )
        return None
    if case is None:
        case = await _case_row(db, placement_id, placement.candidate_id, create=True)
        assert case is not None
        if case.initialized_at is not None:  # pragma: no cover - concurrent creation
            return case
    settings = await get_settings_row(db)
    case.human_id = await next_human_id(db, "onboarding_case", "ONB", width=5)
    case.owner_user_id = (
        settings.default_hr_owner_id or placement.recruiter_user_id or placement.account_manager_user_id
    )
    case.initialized_at = now()
    if template is not None:
        rows = await template_tasks(db, template.id)
        case.template_id = template.id
        case.template_snapshot = snapshot(template, rows)
        await db.flush()
        for i, row in enumerate(rows):
            await add_task(
                ctx,
                db,
                case,
                {
                    "key": row.key,
                    "kind": row.kind,
                    "label": row.label,
                    "instructions": row.instructions,
                    "mandatory": row.mandatory,
                    "due_offset_days": row.due_offset_days,
                    "auto_approve": row.auto_approve,
                    "reusable": row.reusable,
                    "validity_days": row.validity_days,
                    "owner_kind": row.owner_kind,
                    "owner_role_key": row.owner_role_key,
                    "config": row.config,
                },
                source="template",
                position=i,
                planned_start=placement.planned_start_date,
                placement=placement,
                default_days=settings.reuse_validity_days,
            )
    await db.flush()
    if has_pending:
        await _add_compliance_items(ctx, db, case, pending, mandatory=True)
    case.pending_requirements = {}
    await db.flush()
    audit.record("onboarding.case_created", "onboarding_case", case.id, session=db, human_id=case.human_id)
    emit(
        db,
        OnboardingCaseCreated(
            tenant_id=case.tenant_id,
            case_id=case.id,
            placement_id=case.placement_id,
            candidate_id=case.candidate_id,
        ),
    )
    await recompute(ctx, db, case)
    if settings.auto_invite and case.status not in TERMINAL_CASE:
        await invite(ctx, db, case)
    return case


async def handle_requirements(ctx: RequestContext, db: AsyncSession, payload: Mapping[str, Any]) -> int:
    """``ComplianceRequirementsEvaluated`` (Placement subject only). Returns the number of tasks added."""
    placement_id = payload.get("placement_id")
    if payload.get("subject_kind") != "placement" and not placement_id:
        return 0
    pid = uuid.UUID(str(placement_id or payload.get("subject_id")))
    items = {
        "policy_template_ids": [str(x) for x in payload.get("policy_template_ids") or []],
        "credential_type_keys": [str(x) for x in payload.get("credential_type_keys") or []],
    }
    if not any(items.values()):
        return 0
    placement = await _placement_row(db, pid)
    if placement is None:
        return 0
    case = await _case_row(db, pid, placement.candidate_id, create=True)
    assert case is not None
    if case.status == "cancelled":
        return 0
    if case.initialized_at is None:  # holding row: merged when the PlacementCreated handler runs
        held = dict(case.pending_requirements or {})
        for k, values in items.items():
            held[k] = sorted(set(held.get(k) or []) | set(values))
        case.pending_requirements = held
        await db.flush()
        return 0
    complete = case.status == "complete"
    added = await _add_compliance_items(ctx, db, case, items, mandatory=not complete)
    if added and complete:
        await notify.to_users(
            db,
            notify.REQUIREMENTS_ADDED,
            await _hr_users(db, case),
            f"New compliance items were added to completed onboarding {case.human_id} (optional)",
            case_id=case.id,
            placement_id=case.placement_id,
        )
    if added:
        await recompute(ctx, db, case)
        if case.invited_at is not None:
            from app.services.onboarding import tasks as task_service

            for t in added:
                await task_service.prepare_worker_task(ctx, db, case, t)
    return len(added)


async def cancel_for_placement(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, reason: str | None
) -> bool:
    case = (
        await db.execute(
            select(OnboardingCase).where(OnboardingCase.placement_id == placement_id).with_for_update()
        )
    ).scalar_one_or_none()
    if case is None or case.status in TERMINAL_CASE:
        return False
    check_case(case.status, "cancelled", "system")
    case.status = "cancelled"
    case.cancelled_reason = (reason or "placement_dropped")[:200]
    await db.flush()
    await _close_open_work(db, case)
    if case.initialized_at is not None:
        audit.record("onboarding.case_cancelled", "onboarding_case", case.id, session=db)
        emit(
            db,
            OnboardingCaseCancelled(
                tenant_id=case.tenant_id, case_id=case.id, placement_id=case.placement_id
            ),
        )
    del ctx
    return True


async def _close_open_work(db: AsyncSession, case: OnboardingCase) -> None:
    from app.models.tasks import Task
    from app.services import documents_service

    for t in await case_tasks(db, case.id):
        if t.signature_request_id and t.status not in DONE_TASK_STATUSES:
            try:
                async with db.begin_nested():
                    req = await documents_service.esign.load_request(db, t.signature_request_id)
                    if req.status in ("draft", "sent", "partially_signed"):
                        await documents_service.esign.cancel_request(
                            system_ctx(db), db, req, "Onboarding cancelled", actor="system"
                        )
            except Exception:
                logger.exception("could not cancel signature request {}", t.signature_request_id)
        if t.p1_task_id:
            p1 = await db.get(Task, t.p1_task_id)
            if p1 is not None and p1.status == "open":
                p1.status = "cancelled"
    await db.flush()


# ---------------------------------------------------------------- recompute
async def recompute(ctx: RequestContext, db: AsyncSession, case: OnboardingCase) -> None:
    """Progress = mandatory approved|waived / mandatory (integer %). Emits progress / completion events."""
    if case.status == "cancelled" or case.initialized_at is None:
        return
    tasks = await case_tasks(db, case.id)
    mandatory = [t for t in tasks if t.mandatory]
    done = [t for t in mandatory if t.status in DONE_TASK_STATUSES]
    pct = 100 if not mandatory else (len(done) * 100) // len(mandatory)
    if case.status == "complete":
        return  # never reopened; optional late items do not change the outcome
    changed = pct != case.progress_pct
    case.progress_pct = pct
    started = any(t.status != "pending" or t.submitted_at is not None for t in tasks)
    if case.status == "not_started" and started:
        check_case("not_started", "in_progress", "system")
        case.status = "in_progress"
    if len(done) == len(mandatory):
        check_case(case.status, "complete", "system")
        case.status = "complete"
        case.completed_at = now()
    await db.flush()
    if changed:
        emit(
            db,
            OnboardingProgressChanged(
                tenant_id=case.tenant_id, case_id=case.id, placement_id=case.placement_id, pct=pct
            ),
        )
    if case.status == "complete":
        audit.record("onboarding.case_completed", "onboarding_case", case.id, session=db)
        emit(
            db,
            OnboardingCaseCompleted(
                tenant_id=case.tenant_id, case_id=case.id, placement_id=case.placement_id
            ),
        )


# ---------------------------------------------------------------- invite
async def invite(ctx: RequestContext, db: AsyncSession, case: OnboardingCase) -> InviteOut:
    from app.services.onboarding import tasks as task_service

    if case.status in TERMINAL_CASE:
        raise ConflictError("The onboarding case is closed", code="case_closed")
    for t in await case_tasks(db, case.id):
        await task_service.prepare_worker_task(ctx, db, case, t)
    first = case.invited_at is None
    case.invited_at = now()
    await db.flush()
    await notify.to_worker(
        db,
        notify.INVITED,
        case.candidate_id,
        "Complete your onboarding" if first else "Reminder: complete your onboarding",
        "Sign in to the candidate portal and open My Work > Onboarding to complete your joining formalities.",
    )
    audit.record("onboarding.invited", "onboarding_case", case.id, session=db)
    return InviteOut(invited_at=case.invited_at)


async def invite_by_id(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> InviteOut:
    return await invite(ctx, db, await load_case(db, case_id, for_update=True))


async def start_for_placement(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> CaseOut:
    case = await handle_placement_created(ctx, db, placement_id)
    if case is None:
        raise ConflictError("No onboarding template matches this placement", code="no_template")
    return await case_detail(ctx, db, case.id)


# ---------------------------------------------------------------- reads
async def _names(
    db: AsyncSession, candidate_ids: Iterable[uuid.UUID], client_ids: Iterable[uuid.UUID]
) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
    from app.models.candidates import Candidate
    from app.models.clients import Client

    cids = list(set(candidate_ids))
    kids = list(set(client_ids))
    cands = (
        dict(
            (await db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(cids)))).all()
        )  # type: ignore[arg-type]
        if cids
        else {}
    )
    clients = (
        dict((await db.execute(select(Client.id, Client.name).where(Client.id.in_(kids)))).all())  # type: ignore[arg-type]
        if kids
        else {}
    )
    return cands, {k: str(v) for k, v in clients.items()}


def task_out(t: OnboardingTask) -> TaskOut:
    out = TaskOut.model_validate(t)
    out.overdue = is_overdue(t)
    return out


async def case_detail(ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID) -> CaseOut:
    from app.services import placements_service

    case = await load_case(db, case_id)
    placement = await _placement_row(db, case.placement_id)
    cands, clients = await _names(db, [case.candidate_id], [placement.client_id] if placement else [])
    out = CaseOut.model_validate(case)
    out.placement_human_id = placement.human_id if placement else None
    out.worker_name = cands.get(case.candidate_id)
    out.client_id = placement.client_id if placement else None
    out.client_name = clients.get(placement.client_id) if placement else None
    out.planned_start_date = placement.planned_start_date if placement else None
    if case.template_id:
        tpl = await db.get(OnboardingTemplate, case.template_id)
        out.template_name = tpl.name if tpl else (case.template_snapshot or {}).get("name")
    out.gate_status = gate_status(case)
    ident = await placements_service.worker_payment_identity(ctx, db, case.candidate_id)
    out.bank_last4 = ident.bank_account_last4
    out.pan_last4 = ident.pan_last4
    out.tasks = [task_out(t) for t in await case_tasks(db, case.id)]
    return out


def gate_status(case: OnboardingCase | None) -> str | None:
    if case is None or case.initialized_at is None:
        return None
    return {"complete": "met", "cancelled": "not_required"}.get(case.status, "pending")


async def board(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    starts_within: int | None,
    status: list[str] | None,
    owner_id: uuid.UUID | None,
    placement_id: uuid.UUID | None = None,
) -> list[BoardRow]:
    from app.models.placements import Placement

    stmt = (
        select(OnboardingCase, Placement)
        .join(Placement, Placement.id == OnboardingCase.placement_id)
        .where(OnboardingCase.initialized_at.is_not(None))
    )
    if starts_within is not None:
        stmt = stmt.where(Placement.planned_start_date <= today() + timedelta(days=starts_within))
    if status:
        stmt = stmt.where(OnboardingCase.status.in_(status))
    else:
        stmt = stmt.where(OnboardingCase.status != "cancelled")
    if owner_id:
        stmt = stmt.where(OnboardingCase.owner_user_id == owner_id)
    if placement_id:
        stmt = stmt.where(OnboardingCase.placement_id == placement_id)
    rows = (
        await db.execute(stmt.order_by(Placement.planned_start_date, OnboardingCase.human_id).limit(500))
    ).all()
    case_ids = [c.id for c, _ in rows]
    tasks_by: dict[uuid.UUID, list[OnboardingTask]] = {}
    if case_ids:
        for t in (
            await db.execute(select(OnboardingTask).where(OnboardingTask.case_id.in_(case_ids)))
        ).scalars():
            tasks_by.setdefault(t.case_id, []).append(t)
    cands, clients = await _names(db, [c.candidate_id for c, _ in rows], [p.client_id for _, p in rows])
    out: list[BoardRow] = []
    current = now()
    for c, p in rows:
        ts = tasks_by.get(c.id, [])
        counts: dict[str, int] = {}
        for t in ts:
            counts[t.status] = counts.get(t.status, 0) + 1
        submitted = [t.submitted_at for t in ts if t.status == "submitted" and t.submitted_at]
        out.append(
            BoardRow(
                id=c.id,
                human_id=c.human_id,
                placement_id=c.placement_id,
                placement_human_id=p.human_id,
                candidate_id=c.candidate_id,
                worker_name=cands.get(c.candidate_id),
                client_id=p.client_id,
                client_name=clients.get(p.client_id),
                planned_start_date=p.planned_start_date,
                status=c.status,
                owner_user_id=c.owner_user_id,
                progress_pct=c.progress_pct,
                counts=counts,
                overdue=sum(1 for t in ts if is_overdue(t)),
                oldest_submitted_days=(current - min(submitted)).days if submitted else None,
            )
        )
    return out


async def unmatched(ctx: RequestContext, db: AsyncSession) -> list[UnmatchedPlacement]:
    from app.models.placements import Placement

    has_case = select(OnboardingCase.placement_id).where(OnboardingCase.initialized_at.is_not(None))
    # Active placements without a case are listed too (``handle_placement_created`` accepts them): a placement
    # that started before any template matched would otherwise have no case and no "Start onboarding" entry.
    rows = (
        await db.execute(
            select(Placement)
            .where(Placement.status.in_((*PRE_START, "active")), Placement.id.not_in(has_case))
            .order_by(Placement.planned_start_date)
            .limit(500)
        )
    ).scalars()
    return [
        UnmatchedPlacement(
            placement_id=p.id,
            human_id=p.human_id,
            candidate_id=p.candidate_id,
            client_id=p.client_id,
            planned_start_date=p.planned_start_date,
        )
        for p in rows
    ]


async def case_for_placement(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> CaseOut:
    cid = (
        await db.execute(
            select(OnboardingCase.id).where(
                OnboardingCase.placement_id == placement_id, OnboardingCase.initialized_at.is_not(None)
            )
        )
    ).scalar_one_or_none()
    if cid is None:
        raise NotFound("No onboarding case for this placement")
    return await case_detail(ctx, db, cid)


# ---------------------------------------------------------------- start gate / worker 360
async def gate_resolver(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> list[Any]:
    """``onboarding`` start gate: complete -> met, cancelled -> not_required, no case -> none, else pending."""
    from app.services.placements_service import GateState

    case = (
        await db.execute(
            select(OnboardingCase).where(
                OnboardingCase.placement_id == placement_id, OnboardingCase.initialized_at.is_not(None)
            )
        )
    ).scalar_one_or_none()
    status = gate_status(case)
    if case is None or status is None:
        return []
    return [GateState(str(case.id), status, status != "not_required", {"progress_pct": case.progress_pct})]  # type: ignore[arg-type]


async def worker_360(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(OnboardingCase)
            .where(OnboardingCase.candidate_id == candidate_id, OnboardingCase.initialized_at.is_not(None))
            .order_by(OnboardingCase.created_at.desc())
        )
    ).scalars()
    out: list[dict[str, Any]] = []
    for c in rows:
        counts = dict(
            (
                await db.execute(
                    select(OnboardingTask.status, func.count())
                    .where(OnboardingTask.case_id == c.id)
                    .group_by(OnboardingTask.status)
                )
            ).all()  # type: ignore[arg-type]
        )
        out.append(
            {
                "case_id": str(c.id),
                "human_id": c.human_id,
                "placement_id": str(c.placement_id),
                "status": c.status,
                "progress_pct": c.progress_pct,
                "counts": counts,
            }
        )
    return out


async def sla_deadline(db: AsyncSession, case_id: uuid.UUID) -> date | None:
    from app.models.placements import Placement

    return (
        await db.execute(
            select(Placement.planned_start_date)
            .join(OnboardingCase, OnboardingCase.placement_id == Placement.id)
            .where(OnboardingCase.id == case_id)
        )
    ).scalar_one_or_none()
