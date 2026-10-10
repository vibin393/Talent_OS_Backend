# ruff: noqa: E501
"""Adjudication (Epic 20 A6, PRD §8.3, FR-113 / FR-181), the ``bgv`` start-gate resolver and the client status.

* ★ ``adjudicate`` is human-only (``bgv:adjudicate``): ``clear`` only from ``under_review``; ``failed`` from
  ``under_review | discrepancy`` and refused (409 ``discrepancy_awaiting_response``) while any discrepancy awaits
  the candidate (responded / lapsed are fine). Reasons >= 20 characters. Sets ``evidence_purge_after``
  (``bgv_evidence`` retention, default 12 months) and emits ``BgvCaseAdjudicated``. ``failed`` never drops a
  Placement -- the UI offers the placements Drop action (reason ``bgv_failed``).
* No AI anywhere in ``bgv`` (import-graph test).
"""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError
from app.core.events import emit
from app.events.bgv import BgvCaseAdjudicated
from app.models.bgv import BgvCase, BgvDiscrepancy
from app.schemas.bgv import AdjudicateIn, CaseOut
from app.services.bgv import status as st
from app.services.bgv.common import actor, get_case, now, record_event, user_id

DEFAULT_RETENTION_MONTHS = 12


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date(year, month, 28)  # pragma: no cover


async def retention_months(db: AsyncSession) -> int:
    from app.models.privacy import RetentionClass

    row = (
        await db.execute(select(RetentionClass).where(RetentionClass.key == "bgv_evidence"))
    ).scalar_one_or_none()
    months = getattr(row, "delete_after_months", None) if row is not None else None
    return int(months) if months else DEFAULT_RETENTION_MONTHS


async def adjudicate(
    ctx: RequestContext, db: AsyncSession, case_id: uuid.UUID, data: AdjudicateIn
) -> CaseOut:
    from app.services.bgv.cases import detail

    who = actor(ctx)
    case = await get_case(db, case_id, lock=True)
    st.check_transition(case.status, data.outcome, who.kind)  # ★ human-only, clear only from under_review
    if data.outcome == "failed":
        awaiting = (
            await db.execute(
                select(BgvDiscrepancy.id).where(
                    BgvDiscrepancy.case_id == case.id, BgvDiscrepancy.status == "awaiting_response"
                )
            )
        ).first()
        if awaiting:
            raise ConflictError(
                "A discrepancy is still awaiting the candidate's response",
                code="discrepancy_awaiting_response",
            )
    adjudicated_at = now()
    case.adjudication_reasons = data.reasons
    case.adjudicated_by = user_id(ctx)
    case.adjudicated_at = adjudicated_at
    case.outcome_qualifier = "with_exceptions" if data.with_exceptions and data.outcome == "clear" else None
    case.evidence_purge_after = add_months(adjudicated_at.date(), await retention_months(db))
    await st.transition_case(db, case, data.outcome, who, reason="adjudicated")
    record_event(db, case, "adjudicated", who, outcome=data.outcome, with_exceptions=data.with_exceptions)
    audit.record("bgv.case_adjudicated", "bgv_case", case.id, session=db, outcome=data.outcome)
    emit(
        db,
        BgvCaseAdjudicated(
            tenant_id=case.tenant_id,
            case_id=case.id,
            candidate_id=case.candidate_id,
            placement_id=case.placement_id,
            outcome=data.outcome,
            with_exceptions=case.outcome_qualifier == "with_exceptions",
        ),
    )
    return await detail(ctx, db, case.id)


# ================================================================ start gate (registered in hooks.py)
GATE_STATUS = {"clear": "met", "failed": "failed", "cancelled": "not_required"}


async def gate_resolver(ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID) -> list:  # type: ignore[type-arg]  # noqa: ARG001
    from app.services.placements.registry import GateState

    rows = (await db.execute(select(BgvCase).where(BgvCase.placement_id == placement_id))).scalars()
    return [
        GateState(
            str(c.id),
            GATE_STATUS.get(c.status, "pending"),  # type: ignore[arg-type]
            c.required,
            {
                "case": c.human_id,
                "status": c.status,
                "with_exceptions": c.outcome_qualifier == "with_exceptions",
            },
        )
        for c in rows
        if c.required or c.status in GATE_STATUS
    ]


# ================================================================ client portal
async def client_status(ctx: RequestContext, client_db: AsyncSession, placement_id: uuid.UUID) -> str | None:
    """``clear`` | ``not_cleared`` | ``None``. The placement must be visible to the client session (org RLS);
    the outcome is shared only when the contract says so (``commercials_service.shares_bgv_outcome``)."""
    from app.core.errors import NotFound
    from app.models.placements import Placement

    placement = (
        await client_db.execute(select(Placement).where(Placement.id == placement_id))
    ).scalar_one_or_none()
    if placement is None:
        raise NotFound("Placement not found")
    if placement.contract_id is None:
        return None
    try:
        from app.services import commercials_service

        shares = await commercials_service.shares_bgv_outcome(ctx, client_db, placement.contract_id)
    except (ImportError, AttributeError):  # pragma: no cover - commercials without the function
        return None
    if not shares:
        return None
    # Case rows are internal (org RLS): read the statuses with an internal session after the authorization above.
    async with tenant_session(placement.tenant_id) as db:
        statuses = [
            s
            for (s,) in (
                await db.execute(
                    select(BgvCase.status).where(
                        BgvCase.placement_id == placement_id,
                        BgvCase.required.is_(True),
                        BgvCase.status != "cancelled",
                    )
                )
            ).all()
        ]
    if not statuses:
        return None
    if all(s == "clear" for s in statuses):
        return "clear"
    return "not_cleared"
