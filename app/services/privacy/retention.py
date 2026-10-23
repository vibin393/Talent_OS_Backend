"""Retention policy (Epic 12 story 12.3, FR-68).

* Policy (``retention_policies``, audited): ``candidate_inactive_months`` (NULL = off, 6..120),
  ``auto_execute`` (default false), ``audit_retention_years`` (1..10, stored only -- purging audit rows needs
  the owner role, see deferred-work).
* A candidate is **inactive** when the latest of ``updated_at`` and every registered activity signal
  (activity entries, stage changes, consent grants, portal sign-ins) is older than N months. Excluded:
  candidates under a legal hold, with an open DSR, already erased, or excluded by another domain (e.g. a
  non-terminal application).
* ``preview`` stores ``retention_runs`` + ``retention_run_items``; ``execute`` (typed ``ANONYMIZE``) is allowed
  only while the run is ``previewed`` and < 7 days old (else 409 ``preview_expired``). Each item is
  re-evaluated (active again -> ``skipped_now_active``); the rest go through ``run_erasure(reason=retention)``
  in batches of 200 under the per-tenant fairness semaphore.
"""

import uuid
from datetime import timedelta
from typing import Any

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page, PageParams, paginate
from app.events.privacy import RetentionExecutionRequested
from app.models.privacy import RetentionPolicy, RetentionRun, RetentionRunItem
from app.schemas.privacy import RetentionItemOut, RetentionPolicyOut, RetentionPolicyUpdate, RetentionRunOut
from app.services.candidates import privacy_port as candidates
from app.services.privacy import dsr as dsr_service
from app.services.privacy import registry
from app.services.privacy.common import actor_id, now, require_tenant
from app.services.privacy.erasure import SessionFactory

PREVIEW_VALID = timedelta(days=7)
BATCH_SIZE = 200
MAX_PREVIEW_ITEMS = 50_000


# ================================================================ policy
async def _policy(db: AsyncSession) -> RetentionPolicy | None:
    return (await db.execute(select(RetentionPolicy))).scalar_one_or_none()


def _policy_out(row: RetentionPolicy | None) -> RetentionPolicyOut:
    if row is None:
        return RetentionPolicyOut(candidate_inactive_months=None, auto_execute=False, audit_retention_years=7)
    return RetentionPolicyOut(
        candidate_inactive_months=row.candidate_inactive_months,
        auto_execute=row.auto_execute,
        audit_retention_years=row.audit_retention_years,
    )


async def get_policy(ctx: RequestContext, db: AsyncSession) -> RetentionPolicyOut:  # ruff: ignore[unused-function-argument]
    return _policy_out(await _policy(db))


async def update_policy(ctx: RequestContext, db: AsyncSession, data: RetentionPolicyUpdate) -> RetentionPolicyOut:
    tenant_id = require_tenant(ctx)
    row = await _policy(db)
    if row is None:
        row = RetentionPolicy(tenant_id=tenant_id, auto_execute=False, audit_retention_years=7)
        db.add(row)
    fields = data.model_fields_set
    if "candidate_inactive_months" in fields:
        row.candidate_inactive_months = data.candidate_inactive_months
    if "auto_execute" in fields and data.auto_execute is not None:
        row.auto_execute = data.auto_execute
    if "audit_retention_years" in fields and data.audit_retention_years is not None:
        row.audit_retention_years = data.audit_retention_years
    await db.flush()
    return _policy_out(row)


# ================================================================ inactivity
async def last_activity(db: AsyncSession, candidate_ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
    """Latest activity per candidate: ``updated_at`` and every registered signal."""
    latest: dict[uuid.UUID, Any] = dict(await candidates.updated_at(db, candidate_ids))
    for _, signal in registry.activity_signals():
        for cid, at in (await signal(db, candidate_ids)).items():
            if at is not None and (latest.get(cid) is None or at > latest[cid]):
                latest[cid] = at
    return latest


async def _excluded(db: AsyncSession, candidate_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    excluded = await dsr_service.held_candidates(db) | await dsr_service.open_dsr_candidates(db)
    for _, exclusion in registry.retention_exclusions():
        excluded |= await exclusion(db, candidate_ids)
    return excluded


async def inactive_candidates(db: AsyncSession, months: int) -> dict[uuid.UUID, Any]:
    cutoff = now() - relativedelta(months=months)
    pre = await candidates.updated_before(db, cutoff)
    ids = list(pre)
    if not ids:
        return {}
    excluded = await _excluded(db, ids)
    ids = [cid for cid in ids if cid not in excluded]
    latest = await last_activity(db, ids)
    return {cid: latest.get(cid) for cid in ids if latest.get(cid) is None or latest[cid] < cutoff}


# ================================================================ runs
async def _counts(db: AsyncSession, run_id: uuid.UUID) -> dict[str, int]:
    rows = await db.execute(
        select(RetentionRunItem.status, func.count()).where(RetentionRunItem.run_id == run_id).group_by(RetentionRunItem.status)
    )
    return {status: int(n) for status, n in rows.all()}


async def _run_out(db: AsyncSession, run: RetentionRun) -> RetentionRunOut:
    out = RetentionRunOut.model_validate(run)
    out.counts = await _counts(db, run.id)
    return out


async def preview(
    ctx: RequestContext, db: AsyncSession, months: int | None = None, *, auto_execute: bool = False
) -> RetentionRunOut:
    tenant_id = require_tenant(ctx)
    policy = await _policy(db)
    months = months or (policy.candidate_inactive_months if policy is not None else None)
    if months is None:
        raise ValidationFailed(
            "Set candidate_inactive_months first",
            details={"fields": {"candidate_inactive_months": "Required (6-120 months)"}},
        )
    found = await inactive_candidates(db, months)
    run = RetentionRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        status="previewed",
        criteria={"candidate_inactive_months": months, "cutoff": (now() - relativedelta(months=months)).isoformat()},
        candidate_count=min(len(found), MAX_PREVIEW_ITEMS),
        created_by=actor_id(ctx),
        auto_execute_after=now() + PREVIEW_VALID if auto_execute else None,
    )
    db.add(run)
    await db.flush()
    for cid, last in list(found.items())[:MAX_PREVIEW_ITEMS]:
        db.add(RetentionRunItem(id=uuid.uuid4(), tenant_id=tenant_id, run_id=run.id, candidate_id=cid, last_activity_at=last))
    await db.flush()
    audit.record("retention.previewed", "retention_run", run.id, session=db, candidates=run.candidate_count)
    return await _run_out(db, run)


async def list_runs(ctx: RequestContext, db: AsyncSession, params: PageParams) -> Page[RetentionRunOut]:  # ruff: ignore[unused-function-argument]
    page = await paginate(db, select(RetentionRun).order_by(RetentionRun.created_at.desc(), RetentionRun.id), params)
    return Page[RetentionRunOut](
        items=[await _run_out(db, r) for r in page.items], total=page.total, page=page.page, page_size=page.page_size
    )


async def get_run(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> RetentionRunOut:  # ruff: ignore[unused-function-argument]
    run = await db.get(RetentionRun, run_id)
    if run is None:
        raise NotFound("Retention run not found")
    return await _run_out(db, run)


async def list_items(
    ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID, params: PageParams
) -> Page[RetentionItemOut]:
    await get_run(ctx, db, run_id)
    stmt = select(RetentionRunItem).where(RetentionRunItem.run_id == run_id).order_by(
        RetentionRunItem.last_activity_at.asc().nulls_first(), RetentionRunItem.id
    )
    page = await paginate(db, stmt, params)
    return Page[RetentionItemOut](
        items=[RetentionItemOut.model_validate(i) for i in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


async def execute(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID, *, actor: str = "user") -> RetentionRunOut:
    run = await db.get(RetentionRun, run_id, with_for_update=True)
    if run is None:
        raise NotFound("Retention run not found")
    if run.status != "previewed":
        raise ConflictError("Only a previewed run can be executed", code="invalid_transition")
    if actor == "user" and now() - run.created_at > PREVIEW_VALID:
        run.status = "expired"
        await db.flush()
        await db.commit()  # keep the expiry even though the request fails
        raise ConflictError("The preview is older than 7 days; preview again", code="preview_expired")
    run.status = "executing"
    run.executed_by = actor_id(ctx) if actor == "user" else None
    run.executed_at = now()
    await db.flush()
    audit.record("retention.executed", "retention_run", run.id, session=db, candidates=run.candidate_count)
    emit(db, RetentionExecutionRequested(tenant_id=run.tenant_id, run_id=run.id))
    return await _run_out(db, run)


async def cancel(ctx: RequestContext, db: AsyncSession, run_id: uuid.UUID) -> RetentionRunOut:  # ruff: ignore[unused-function-argument]
    run = await db.get(RetentionRun, run_id, with_for_update=True)
    if run is None:
        raise NotFound("Retention run not found")
    if run.status != "previewed":
        raise ConflictError("Only a previewed run can be cancelled", code="invalid_transition")
    run.status = "cancelled"
    await db.flush()
    return await _run_out(db, run)


async def process_run(tenant_id: uuid.UUID, run_id: uuid.UUID, *, sessions: SessionFactory = tenant_session) -> dict[str, int]:
    """Worker side of ``execute``: re-evaluate every pending item, erase the still-inactive ones."""
    from app.services.privacy import erasure

    ctx = RequestContext(tenant_id=tenant_id)
    async with sessions(tenant_id) as db:
        run = await db.get(RetentionRun, run_id)
        if run is None or run.status != "executing":
            return {}
        if run.kind == "class_expiry":  # Epic 35: retention-class expiry
            from app.services.privacy import class_expiry

            return await class_expiry.process_run(tenant_id, run_id, sessions=sessions)
        months = int(run.criteria["candidate_inactive_months"])
    while True:
        async with sessions(tenant_id) as db:
            audit.set_actor(db, "system", None)
            items = list(
                (
                    await db.execute(
                        select(RetentionRunItem)
                        .where(RetentionRunItem.run_id == run_id, RetentionRunItem.status == "pending")
                        .limit(BATCH_SIZE)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars()
            )
            if not items:
                break
            still = await inactive_candidates(db, months)
            to_erase: list[uuid.UUID] = []
            for item in items:
                if item.candidate_id not in still:
                    item.status = "skipped_now_active"
                else:
                    to_erase.append(item.candidate_id)
            run_ids = []
            for cid in to_erase:
                try:
                    er = await erasure.create_run(
                        ctx, db, cid, reason="retention", retention_run_id=run_id, enqueue=False
                    )
                    run_ids.append(er.id)
                except ConflictError:
                    for item in items:
                        if item.candidate_id == cid:
                            item.status = "skipped_now_active"
        for erasure_run_id in run_ids:
            status = await erasure.run_erasure(tenant_id, erasure_run_id, sessions=sessions)
            if status != "completed":
                async with sessions(tenant_id) as db:
                    er_row = await erasure.get_run(db, erasure_run_id)
                    if er_row is not None:
                        item = (  # type: ignore[assignment]
                            await db.execute(
                                select(RetentionRunItem).where(
                                    RetentionRunItem.run_id == run_id, RetentionRunItem.candidate_id == er_row.candidate_id
                                )
                            )
                        ).scalar_one_or_none()
                        if item is not None and item.status == "pending":
                            item.status = "failed"
    async with sessions(tenant_id) as db:
        audit.set_actor(db, "system", None)
        run = await db.get(RetentionRun, run_id, with_for_update=True)
        if run is not None and run.status == "executing":
            run.status = "completed"
        return await _counts(db, run_id)


async def monthly(tenant_id: uuid.UUID, *, sessions: SessionFactory = tenant_session) -> str:
    """Beat (1st of the month): preview with the policy and notify admins; auto-execute after 7 days."""
    from app.services import identity_bridge
    from app.services.privacy.common import email_users

    ctx = RequestContext(tenant_id=tenant_id)
    async with sessions(tenant_id) as db:
        audit.set_actor(db, "system", None)
        policy = await _policy(db)
        if policy is None or policy.candidate_inactive_months is None:
            return "no_policy"
        run = await preview(ctx, db, policy.candidate_inactive_months, auto_execute=policy.auto_execute)
        admins = await identity_bridge.users_with_permission(db, "retention:manage")
    await email_users(
        admins,
        "Retention preview ready",
        f"{run.candidate_count} inactive candidates match the retention policy. Review the preview in the "
        "privacy center" + (" (it executes automatically in 7 days unless cancelled)." if policy.auto_execute else ".")
        + "\n",
        tenant_id=tenant_id,
        kind="retention_preview",
    )
    return "previewed"


async def execute_due_auto_runs(tenant_id: uuid.UUID, *, sessions: SessionFactory = tenant_session) -> int:
    """Execute auto-execute previews whose 7-day window has passed (daily)."""
    ctx = RequestContext(tenant_id=tenant_id)
    started = 0
    async with sessions(tenant_id) as db:
        audit.set_actor(db, "system", None)
        due = list(
            (
                await db.execute(
                    select(RetentionRun).where(
                        RetentionRun.status == "previewed",
                        RetentionRun.auto_execute_after.is_not(None),
                        RetentionRun.auto_execute_after <= now(),
                    )
                )
            ).scalars()
        )
        for run in due:
            await execute(ctx, db, run.id, actor="system")
            started += 1
    return started
