"""HR review of onboarding tasks (Epic 23 A6, P2-10.3, FR-124): approve, return, ★ waive (human only), bulk
approve (one savepoint per task), Aadhaar redaction confirmation and eSign re-issue."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, DomainError
from app.schemas.onboarding import ApproveIn, BulkResultOut, ReturnIn, TaskOut, WaiveIn
from app.services.onboarding import tasks as ops
from app.services.onboarding.cases import task_out
from app.services.onboarding.common import load_case, load_task, user_id


def _actor(ctx: RequestContext) -> str:
    kind = actor_from_ctx(ctx).kind
    return "user" if kind == "user" else str(kind)


async def approve(ctx: RequestContext, db: AsyncSession, task_id: uuid.UUID, data: ApproveIn) -> TaskOut:
    task = await load_task(db, task_id, for_update=True)
    await ops.decide(
        ctx,
        db,
        task,
        "approved",
        _actor(ctx),
        decided_by=user_id(ctx),
        extra=data.model_dump(exclude_none=True),
    )
    return task_out(task)


async def return_task(ctx: RequestContext, db: AsyncSession, task_id: uuid.UUID, data: ReturnIn) -> TaskOut:
    task = await load_task(db, task_id, for_update=True)
    await ops.decide(ctx, db, task, "returned", _actor(ctx), decided_by=user_id(ctx), comment=data.comment)
    return task_out(task)


async def waive(
    ctx: RequestContext, db: AsyncSession, task_id: uuid.UUID, data: WaiveIn, *, actor: str | None = None
) -> TaskOut:
    """★ Human only: ``workflow`` / ``system`` / ``ai`` actors get 409 ``transition_not_allowed``."""
    task = await load_task(db, task_id, for_update=True)
    await ops.decide(
        ctx, db, task, "waived", actor or _actor(ctx), decided_by=user_id(ctx), reason=data.reason
    )
    return task_out(task)


async def bulk_approve(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, task_ids: list[uuid.UUID]
) -> BulkResultOut:
    await load_case(db, case_id)
    approved: list[uuid.UUID] = []
    failed: dict[str, str] = {}
    for tid in task_ids:
        try:
            async with db.begin_nested():
                task = await load_task(db, tid, for_update=True)
                if task.case_id != case_id:
                    raise ConflictError("Task of another case", code="not_found")
                await ops.decide(ctx, db, task, "approved", _actor(ctx), decided_by=user_id(ctx))
            approved.append(tid)
        except DomainError as exc:
            failed[str(tid)] = exc.code
    return BulkResultOut(approved=approved, failed=failed)


async def confirm_redaction(ctx: RequestContext, db: AsyncSession, task_id: uuid.UUID) -> TaskOut:
    from app.core import audit

    task = await load_task(db, task_id, for_update=True)
    if not (task.config or {}).get("aadhaar") or not task.data:
        raise ConflictError("Nothing to confirm", code="no_redaction_review")
    task.data = {**task.data, "redaction_status": "confirmed", "redaction_confirmed_by": str(user_id(ctx))}
    await db.flush()
    audit.record("onboarding.redaction_confirmed", "onboarding_task", task.id, session=db)
    return task_out(task)


async def reissue_signature(ctx: RequestContext, db: AsyncSession, task_id: uuid.UUID) -> TaskOut:
    task = await load_task(db, task_id, for_update=True)
    await ops.reissue_signature(ctx, db, task)
    return task_out(task)
