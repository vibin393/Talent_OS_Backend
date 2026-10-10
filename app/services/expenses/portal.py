"""Worker ("My Work") and Client Portal projections and actions for Expense claims (AD-26/27).

Access is checked first in the caller's org-scoped session (worker: ``candidate_id``; client:
``client_id`` plus
``visible_placement_ids`` for restricted Client Users) -- another org's id is 404. The work then runs in an
internal tenant session on behalf of the portal principal (approval requests, ledgers, policies and files are
internal-only tables). Client decisions always go through ``approvals_service.decide`` with the Client
``ActorRef``. Worker schemas carry no bill amount / markup; Client schemas carry billable lines only.
"""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef
from app.core.client_scope import visible_placement_ids
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.models.approvals import ApprovalRequest
from app.models.expenses import ExpenseClaim, ExpenseLine
from app.models.placements import Placement
from app.schemas.expenses import LineIn, LinePreviewOut
from app.schemas.expenses_portal import (
    ClientClaimDetail,
    ClientClaimLine,
    ClientClaimSummary,
    ReceiptOut,
    ReceiptReadOut,
    WorkerClaimablePlacement,
    WorkerClaimOut,
    WorkerClaimSummary,
    WorkerExpenseSettings,
    WorkerLine,
)
from app.services.expenses import amounts, approval, claims, receipts
from app.services.expenses.common import claim_lines, claim_not_found, get_claim, settings_row
from app.services.expenses.lines import placement_window


# ================================================================ Worker
async def _own(wdb: AsyncSession, candidate_id: uuid.UUID, claim_id: uuid.UUID) -> None:
    found = (
        await wdb.execute(
            select(ExpenseClaim.id).where(
                ExpenseClaim.id == claim_id, ExpenseClaim.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise claim_not_found()


async def _placement_meta(
    db: AsyncSession, placement_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[str | None, str | None]]:
    from app.models.clients import Client

    if not placement_ids:
        return {}
    rows = await db.execute(
        select(Placement.id, Client.name, Placement.role_title)
        .join(Client, Client.id == Placement.client_id, isouter=True)
        .where(Placement.id.in_(placement_ids))
    )
    return {r[0]: (r[1], r[2]) for r in rows.all()}


async def _worker_summaries(db: AsyncSession, rows: list[ExpenseClaim]) -> list[WorkerClaimSummary]:
    meta = await _placement_meta(db, {r.placement_id for r in rows})
    counts: dict[uuid.UUID, int] = {}
    if rows:
        for cid in (
            await db.execute(
                select(ExpenseLine.claim_id).where(ExpenseLine.claim_id.in_([r.id for r in rows]))
            )
        ).scalars():
            counts[cid] = counts.get(cid, 0) + 1
    return [
        WorkerClaimSummary(
            id=r.id,
            human_id=r.human_id,
            placement_id=r.placement_id,
            client_name=meta.get(r.placement_id, (None, None))[0],
            role_title=meta.get(r.placement_id, (None, None))[1],
            currency=r.currency,
            title=r.title,
            status=r.status,
            submitted_at=r.submitted_at,
            total_claimed=r.total_claimed,
            total_approved=r.total_approved,
            line_count=counts.get(r.id, 0),
        )
        for r in rows
    ]


async def worker_out(db: AsyncSession, claim: ExpenseClaim) -> WorkerClaimOut:
    summary = (await _worker_summaries(db, [claim]))[0]
    lines = await claim_lines(db, claim.id)
    names = await claims.receipt_names(db, lines)
    placement = (await db.execute(select(Placement).where(Placement.id == claim.placement_id))).scalar_one()
    start, last = placement_window(placement)
    return WorkerClaimOut(
        **summary.model_dump(),
        editable=claims.editable(claim),
        can_recall=await claims.can_recall(db, claim),
        rejection_comment=claim.rejection_comment,
        placement_start=start,
        placement_end=last,
        lines=[
            WorkerLine(
                id=ln.id,
                expense_date=ln.expense_date,
                category=ln.category,
                description=ln.description,
                amount=ln.amount,
                currency=ln.currency,
                km=ln.km,
                fx_rate=ln.fx_rate,
                fx_rate_date=ln.fx_rate_date,
                amount_claim_ccy=ln.amount_claim_ccy,
                receipt_file_id=ln.receipt_file_id,
                receipt_name=names.get(ln.receipt_file_id) if ln.receipt_file_id else None,
                billable=ln.billable,
                reimbursable=ln.reimbursable,
                flags=list(ln.flags or []),
                justification=ln.justification,
                line_status=ln.line_status,
                decision_comment=ln.decision_comment,
            )
            for ln in lines
        ],
    )


async def worker_settings(
    ctx: RequestContext,  # noqa: ARG001
    wdb: AsyncSession,  # noqa: ARG001
    tenant_id: uuid.UUID,
) -> WorkerExpenseSettings:
    async with tenant_session(tenant_id) as db:
        row = await settings_row(db)
        return WorkerExpenseSettings(
            ai_receipt_read_enabled=row.ai_receipt_read_enabled,
            max_receipt_mb=row.max_receipt_mb,
            allowed_receipt_types=list(row.allowed_receipt_types or []),
        )


async def my_claims(
    ctx: RequestContext,  # noqa: ARG001
    wdb: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
) -> list[WorkerClaimSummary]:
    ids = list(
        (
            await wdb.execute(select(ExpenseClaim.id).where(ExpenseClaim.candidate_id == candidate_id))
        ).scalars()
    )
    if not ids:
        return []
    async with tenant_session(tenant_id) as db:
        rows = list(
            (
                await db.execute(
                    select(ExpenseClaim)
                    .where(ExpenseClaim.id.in_(ids), ExpenseClaim.candidate_id == candidate_id)
                    .order_by(ExpenseClaim.created_at.desc())
                )
            ).scalars()
        )
        return await _worker_summaries(db, rows)


async def claimable_placements(
    ctx: RequestContext,  # noqa: ARG001
    wdb: AsyncSession,  # noqa: ARG001
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
) -> list[WorkerClaimablePlacement]:
    async with tenant_session(tenant_id) as db:
        rows = [
            p
            for p in (
                await db.execute(select(Placement).where(Placement.candidate_id == candidate_id))
            ).scalars()
            if claims.claimable(p)
        ]
        meta = await _placement_meta(db, {p.id for p in rows})
        out = []
        for p in rows:
            start, last = placement_window(p)
            out.append(
                WorkerClaimablePlacement(
                    placement_id=p.id,
                    client_name=meta.get(p.id, (None, None))[0],
                    role_title=meta.get(p.id, (None, None))[1],
                    currency=p.currency,
                    start_date=start,
                    end_date=last,
                )
            )
        return out


async def create(
    ctx: RequestContext,
    wdb: AsyncSession,  # noqa: ARG001
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    placement_id: uuid.UUID,
    title: str | None,
) -> WorkerClaimOut:
    async with tenant_session(tenant_id) as db:
        claim = await claims.create_claim(ctx, db, placement_id, title=title, candidate_id=candidate_id)
        return await worker_out(db, claim)


async def my_claim(
    ctx: RequestContext,  # noqa: ARG001
    wdb: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    claim_id: uuid.UUID,
) -> WorkerClaimOut:
    await _own(wdb, candidate_id, claim_id)
    async with tenant_session(tenant_id) as db:
        return await worker_out(db, await get_claim(db, claim_id))


async def worker_action(
    ctx: RequestContext,
    wdb: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    claim_id: uuid.UUID,
    action: str,
    **kwargs: Any,
) -> WorkerClaimOut:
    await _own(wdb, candidate_id, claim_id)
    actor = ActorRef("candidate", candidate_id)
    async with tenant_session(tenant_id) as db:
        claim = await get_claim(db, claim_id, lock=True)
        if action == "title":
            if claim.status not in ("draft", "rejected"):
                from app.services.expenses.common import not_editable

                raise not_editable(claim.status)
            claim.title = kwargs["title"]
        elif action == "upsert":
            await claims.upsert_line(ctx, db, claim, kwargs["data"], kwargs.get("line_id"), actor)
        elif action == "delete":
            await claims.delete_line(ctx, db, claim, kwargs["line_id"], actor)
        elif action == "submit":
            await claims.submit(ctx, db, claim, actor=actor)
        elif action == "recall":
            await claims.recall(ctx, db, claim, actor)
        await db.flush()
        await db.refresh(claim)
        return await worker_out(db, claim)


async def preview(
    ctx: RequestContext,
    wdb: AsyncSession,
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    claim_id: uuid.UUID,
    data: LineIn,
) -> LinePreviewOut:
    await _own(wdb, candidate_id, claim_id)
    async with tenant_session(tenant_id) as db:
        return await claims.preview_line(ctx, db, await get_claim(db, claim_id), data)


async def upload_receipt(
    ctx: RequestContext,
    wdb: AsyncSession,  # noqa: ARG001
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    data: bytes,
    filename: str | None,
) -> ReceiptOut:
    async with tenant_session(tenant_id) as db:
        return await receipts.upload(ctx, db, candidate_id, data, filename)


async def read_receipt(
    ctx: RequestContext,
    wdb: AsyncSession,  # noqa: ARG001
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    file_id: uuid.UUID,
) -> ReceiptReadOut:
    async with tenant_session(tenant_id) as db:
        return await receipts.read_receipt(ctx, db, candidate_id, file_id)


async def download_receipt(
    ctx: RequestContext,  # noqa: ARG001
    wdb: AsyncSession,  # noqa: ARG001
    tenant_id: uuid.UUID,
    candidate_id: uuid.UUID,
    file_id: uuid.UUID,
) -> tuple[bytes, str, str]:
    async with tenant_session(tenant_id) as db:
        row = await receipts.own_receipt(db, candidate_id, file_id)
        return await receipts.read_bytes(row), row.original_name, row.content_type


# ================================================================ Client
async def _visible_ids(
    ctx: RequestContext, cdb: AsyncSession, ids: list[uuid.UUID] | None = None
) -> list[uuid.UUID]:
    """Claim ids this Client User may see: org RLS (client) + restricted placement scope; drafts never."""
    client_id = getattr(ctx.principal, "org_id", None)
    stmt = select(ExpenseClaim.id, ExpenseClaim.placement_id).where(
        ExpenseClaim.client_id == client_id, ExpenseClaim.status != "draft"
    )
    if ids is not None:
        stmt = stmt.where(ExpenseClaim.id.in_(ids))
    rows = (await cdb.execute(stmt)).all()
    scope = await visible_placement_ids(ctx, cdb)
    return [r[0] for r in rows if scope is None or r[1] in scope]


def _client_bill(line: ExpenseLine, currency: str) -> Decimal | None:
    if line.bill_amount is not None:
        return line.bill_amount
    if not line.billable or line.line_status == "rejected":
        return None
    return amounts.bill_amount(
        line.amount_claim_ccy, line.rebill_method or "at_cost", line.markup_pct, currency
    )


async def _client_summaries(
    ctx: RequestContext, db: AsyncSession, rows: list[ExpenseClaim]
) -> list[tuple[ClientClaimSummary, list[ExpenseLine]]]:
    from app.models.candidates import Candidate

    if not rows:
        return []
    meta = await _placement_meta(db, {r.placement_id for r in rows})
    workers = {
        r[0]: r[1]
        for r in (
            await db.execute(
                select(Candidate.id, Candidate.full_name).where(
                    Candidate.id.in_({r.candidate_id for r in rows})
                )
            )
        ).all()
    }
    req_ids = [r.approval_request_id for r in rows if r.approval_request_id]
    requests = (
        {
            q.id: q
            for q in (
                await db.execute(select(ApprovalRequest).where(ApprovalRequest.id.in_(req_ids)))
            ).scalars()
        }
        if req_ids
        else {}
    )
    lines: dict[uuid.UUID, list[ExpenseLine]] = {r.id: [] for r in rows}
    for ln in (
        await db.execute(
            select(ExpenseLine)
            .where(ExpenseLine.claim_id.in_([r.id for r in rows]), ExpenseLine.billable.is_(True))
            .order_by(ExpenseLine.expense_date, ExpenseLine.id)
        )
    ).scalars():
        lines[ln.claim_id].append(ln)
    out = []
    for r in rows:
        billable = lines[r.id]
        if not billable:
            continue
        total = amounts.total([b for b in (_client_bill(ln, r.currency) for ln in billable) if b is not None])
        out.append(
            (
                ClientClaimSummary(
                    id=r.id,
                    human_id=r.human_id,
                    placement_id=r.placement_id,
                    worker_name=workers.get(r.candidate_id),
                    role_title=meta.get(r.placement_id, (None, None))[1],
                    currency=r.currency,
                    title=r.title,
                    status=r.status,
                    submitted_at=r.submitted_at,
                    billable_line_count=len(billable),
                    billable_total=total,
                    approval_request_id=r.approval_request_id,
                    awaiting_client=await approval.awaiting_client(
                        ctx.principal, requests.get(r.approval_request_id) if r.approval_request_id else None
                    ),
                ),
                billable,
            )
        )
    return out


def _client_detail(summary: ClientClaimSummary, billable: list[ExpenseLine]) -> ClientClaimDetail:
    return ClientClaimDetail(
        **summary.model_dump(),
        lines=[
            ClientClaimLine(
                id=ln.id,
                expense_date=ln.expense_date,
                category=ln.category,
                description=ln.description,
                amount_claim_ccy=ln.amount_claim_ccy,
                bill_amount=_client_bill(ln, summary.currency),
                has_receipt=ln.receipt_file_id is not None,
                line_status=ln.line_status,
                decision_comment=ln.decision_comment,
            )
            for ln in billable
        ],
        line_decisions_supported=True,
    )


async def client_claim_summaries(
    ctx: RequestContext, db: AsyncSession, request_ids: list[uuid.UUID]
) -> list[ClientClaimSummary]:
    """Projection for the Client Portal approvals inbox (Epic 18), by approval request id."""
    rows = list(
        (
            await db.execute(select(ExpenseClaim).where(ExpenseClaim.approval_request_id.in_(request_ids)))
        ).scalars()
    )
    visible = set(await _visible_ids(ctx, db, [r.id for r in rows])) if rows else set()
    return [s for s, _ in await _client_summaries(ctx, db, [r for r in rows if r.id in visible])]


async def client_claim_detail(
    ctx: RequestContext, db: AsyncSession, claim_id: uuid.UUID
) -> ClientClaimDetail:
    """Billable lines only. ``db`` must already be access-checked or org scoped."""
    claim = await get_claim(db, claim_id)
    found = await _client_summaries(ctx, db, [claim])
    if not found:
        raise claim_not_found()
    return _client_detail(*found[0])


async def client_list(
    ctx: RequestContext, cdb: AsyncSession, tenant_id: uuid.UUID, *, status: str = "pending"
) -> list[ClientClaimSummary]:
    ids = await _visible_ids(ctx, cdb)
    if not ids:
        return []
    async with tenant_session(tenant_id) as db:
        stmt = select(ExpenseClaim).where(ExpenseClaim.id.in_(ids)).order_by(ExpenseClaim.submitted_at.desc())
        if status == "pending":
            stmt = stmt.where(ExpenseClaim.status.in_(("submitted", "in_approval")))
        rows = list((await db.execute(stmt.limit(500))).scalars())
        out = [s for s, _ in await _client_summaries(ctx, db, rows)]
    return [s for s in out if s.awaiting_client] if status == "pending" else out


async def client_detail(
    ctx: RequestContext, cdb: AsyncSession, tenant_id: uuid.UUID, claim_id: uuid.UUID
) -> ClientClaimDetail:
    if claim_id not in await _visible_ids(ctx, cdb, [claim_id]):
        raise claim_not_found()
    async with tenant_session(tenant_id) as db:
        return await client_claim_detail(ctx, db, claim_id)


async def client_decide(
    ctx: RequestContext,
    cdb: AsyncSession,
    tenant_id: uuid.UUID,
    claim_id: uuid.UUID,
    action: str,
    comment: str | None,
    line_decisions: list[dict[str, Any]],
) -> ClientClaimDetail:
    if claim_id not in await _visible_ids(ctx, cdb, [claim_id]):
        raise claim_not_found()
    actor = ActorRef("client_user", getattr(ctx.principal, "id", None))
    async with tenant_session(tenant_id) as db:
        claim = await get_claim(db, claim_id, lock=True)
        await approval.decide_as(ctx, db, claim, action, comment, line_decisions, actor)
        await db.refresh(claim)
        return await client_claim_detail(ctx, db, claim_id)


async def client_receipt(
    ctx: RequestContext, cdb: AsyncSession, tenant_id: uuid.UUID, claim_id: uuid.UUID, line_id: uuid.UUID
) -> tuple[bytes, str, str]:
    if claim_id not in await _visible_ids(ctx, cdb, [claim_id]):
        raise claim_not_found()
    async with tenant_session(tenant_id) as db:
        line = (
            await db.execute(
                select(ExpenseLine).where(
                    ExpenseLine.id == line_id,
                    ExpenseLine.claim_id == claim_id,
                    ExpenseLine.billable.is_(True),
                )
            )
        ).scalar_one_or_none()
        if line is None or line.receipt_file_id is None:
            raise NotFound("Receipt not found")
        from app.models.files import StoredFile

        row = (await db.execute(select(StoredFile).where(StoredFile.id == line.receipt_file_id))).scalar_one()
        return await receipts.read_bytes(row), row.original_name, row.content_type
