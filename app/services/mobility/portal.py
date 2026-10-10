# ruff: noqa: E501
"""Employee self-service (Epic 41): internal Users on ``/mobility/*`` and Workers on ``/portal/work/opportunities*``.

No permission key: any active internal User, any Worker (a Candidate with a non-terminal Placement); the board
switches and eligibility are the gates. Workers:

* the board rows are read in the Worker's org-scoped session (``internal_openings`` admits worker sessions; it holds
  no client or compensation data);
* the Worker's own applications are read in the Worker's org-scoped session (``mobility_applications`` carries
  worker org RLS on ``candidate_id``: another Worker's rows are invisible -> 404);
* eligibility, fit and applying need Demands, rules, placements and matches, which a Worker session cannot read:
  those run in an internal tenant session after the principal check, with the Employee fixed to the signed-in
  Worker.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import Principal
from app.schemas.mobility import ApplyIn, MyApplicationOut, OpeningDetailOut, OpeningPage, ProfilePreviewOut
from app.schemas.mobility_portal import WorkerApplicationOut, WorkerOpeningDetailOut, WorkerOpeningPage
from app.services.mobility import applications, board, employees
from app.services.mobility.common import not_found, settings_row


# ================================================================ internal Users
async def _user(principal: Principal, db: AsyncSession) -> employees.Employee:
    return await employees.user_employee(db, principal.id)


async def user_board(principal: Principal, db: AsyncSession, *, q: str | None, location: str | None, include_ineligible: bool, page: int, page_size: int) -> OpeningPage:
    emp = await _user(principal, db)
    settings = await settings_row(db)
    return await board.board_for(
        db, emp, q=q, location=location, include_ineligible=include_ineligible, page=page, page_size=page_size, enabled=settings.board_enabled
    )


async def user_opening(principal: Principal, db: AsyncSession, demand_id: uuid.UUID) -> OpeningDetailOut:
    emp = await _user(principal, db)
    return await board.detail_for(db, emp, demand_id, enabled=(await settings_row(db)).board_enabled)


async def user_profile(ctx: RequestContext, principal: Principal, db: AsyncSession) -> ProfilePreviewOut:
    return await employees.profile_preview(ctx, db, principal.id)


async def user_apply(ctx: RequestContext, principal: Principal, db: AsyncSession, demand_id: uuid.UUID, data: ApplyIn) -> MyApplicationOut:
    emp = await _user(principal, db)
    row = await applications.apply(ctx, db, emp, demand_id, data)
    return (await applications.my_views(db, [row]))[0]


async def user_applications(principal: Principal, db: AsyncSession) -> list[MyApplicationOut]:
    await _user(principal, db)
    return await applications.my_views(db, await applications.user_rows(db, principal.id))


async def user_withdraw(ctx: RequestContext, principal: Principal, db: AsyncSession, mobility_application_id: uuid.UUID) -> MyApplicationOut:
    emp = await _user(principal, db)
    row = await applications.withdraw(ctx, db, emp, mobility_application_id)
    return (await applications.my_views(db, [row]))[0]


# ================================================================ Workers
def _worker_ctx(principal: Any, ip: str | None = None) -> RequestContext:
    return RequestContext(tenant_id=principal.tenant_id, ip=ip)


async def worker_board(principal: Any, worker_db: AsyncSession, *, q: str | None, location: str | None, include_ineligible: bool, page: int, page_size: int) -> WorkerOpeningPage:
    async with tenant_session(principal.tenant_id) as db:
        emp = await employees.worker_employee(db, principal.candidate_id)
        settings = await settings_row(db)
        if not settings.worker_board_enabled:
            return WorkerOpeningPage(items=[], total=0, page=page, page_size=page_size, board_enabled=False)
        await board.refresh(db)
    rows = await board.load_openings(worker_db, q=q, location=location)  # Worker session: the board policy admits it
    async with tenant_session(principal.tenant_id) as db:
        decorated = await board.decorate(db, emp, rows)
    page_out = board.page_of([o for o, _ in decorated], include_ineligible=include_ineligible, page=page, page_size=page_size, board_enabled=True)
    return WorkerOpeningPage.model_validate(page_out.model_dump())


async def worker_opening(principal: Any, worker_db: AsyncSession, demand_id: uuid.UUID) -> WorkerOpeningDetailOut:
    async with tenant_session(principal.tenant_id) as db:
        emp = await employees.worker_employee(db, principal.candidate_id)
        settings = await settings_row(db)
        if not settings.worker_board_enabled:
            raise not_found("Opening")
        await board.refresh(db)
        disclosure = settings.disclosure_text
    rows = await board.load_openings(worker_db, demand_id=demand_id)
    if not rows:
        raise not_found("Opening")
    async with tenant_session(principal.tenant_id) as db:
        ((o, row),) = await board.decorate(db, emp, rows)
    return WorkerOpeningDetailOut.model_validate(board.detail_of(o, row, disclosure).model_dump())


def _worker_out(item: MyApplicationOut) -> WorkerApplicationOut:
    return WorkerApplicationOut.model_validate(item.model_dump())


async def worker_apply(principal: Any, demand_id: uuid.UUID, data: ApplyIn, ip: str | None) -> WorkerApplicationOut:
    async with tenant_session(principal.tenant_id) as db:
        audit.set_actor(db, "candidate", principal.candidate_id)
        emp = await employees.worker_employee(db, principal.candidate_id)
        row = await applications.apply(_worker_ctx(principal, ip), db, emp, demand_id, data)
        return _worker_out((await applications.my_views(db, [row]))[0])


async def worker_applications(principal: Any, worker_db: AsyncSession) -> list[WorkerApplicationOut]:
    async with tenant_session(principal.tenant_id) as db:
        await employees.worker_employee(db, principal.candidate_id)
    rows = await applications.worker_rows(worker_db, principal.candidate_id)
    async with tenant_session(principal.tenant_id) as db:
        views = await applications.my_views(db, rows)
    return [_worker_out(v) for v in views]


async def worker_withdraw(principal: Any, worker_db: AsyncSession, mobility_application_id: uuid.UUID) -> WorkerApplicationOut:
    """Ownership is proven in the Worker's own org-scoped session first (another Worker's application -> 404)."""
    from app.models.mobility import MobilityApplication

    if await worker_db.get(MobilityApplication, mobility_application_id) is None:
        raise not_found()
    async with tenant_session(principal.tenant_id) as db:
        audit.set_actor(db, "candidate", principal.candidate_id)
        emp = await employees.worker_employee(db, principal.candidate_id)
        row = await applications.withdraw(_worker_ctx(principal), db, emp, mobility_application_id)
        return _worker_out((await applications.my_views(db, [row]))[0])
