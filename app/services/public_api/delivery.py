# ruff: noqa: D400, D415, E501
"""Public delivery reads (Epic 67, spec A4). Scope predicates (recorded per resource):

* Submissions: applications visible under ``submission:read`` (``pipeline.common.visible_applications``).
* Interviews: applications visible under ``interview:read``; times, stage, participant user ids and status only
  (no feedback, scorecards or recordings).
* Offers: ``offers.common.visible_offers`` -- status only.
* Placements: the recruiter / account manager under ``placement:read`` (mirrors ``placements.lists.scoped``).
* Workers: Worker profiles of placements visible under ``worker:read`` (recruiter / account manager / manager).
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import scope_condition
from app.models.interviews import Interview, InterviewParticipant
from app.models.offers import Offer
from app.models.pipeline import Application, Submission
from app.models.placements import Placement, WorkerProfile
from app.services.offers.common import visible_offers
from app.services.pipeline.common import visible_applications
from app.services.public_api._core import Order, base, ctx_principal, expose, finish_all, money, one, page


def _apps(permission: str) -> Any:
    _, principal = ctx_principal()
    return visible_applications(principal, permission).with_only_columns(Application.id)


# ================================================================ submissions
def _submissions() -> Any:
    return select(Submission).where(Submission.application_id.in_(_apps("submission:read")))


async def _build_submissions(rows: Sequence[Submission]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    await expose(principal, "candidate", [r.candidate_id for r in rows])
    return finish_all(principal, [
        {**base(s, "submission", human_id=s.human_id), "application_id": s.application_id, "candidate_id": s.candidate_id,
         "demand_id": s.demand_id, "client_id": s.client_id, "status": s.status, "sent_at": s.sent_at,
         "bill_rate": money(s.bill_rate, s.currency), "pay_rate": money(s.pay_rate, s.currency)}
        for s in rows
    ])  # fmt: skip


async def list_submissions(db: AsyncSession, *, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    return await page(db, _submissions(), sort_col=Submission.updated_at, id_col=Submission.id, limit=limit, cursor=cursor, order=order, build=_build_submissions)


async def get_submission(db: AsyncSession, submission_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_submissions([await one(db, _submissions().where(Submission.id == submission_id))]))[0]


# ================================================================ interviews
def _interviews() -> Any:
    return select(Interview).where(Interview.application_id.in_(_apps("interview:read")))


async def _build_interviews(db: AsyncSession, rows: Sequence[Interview]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    users: dict[uuid.UUID, list[uuid.UUID]] = {}
    if rows:
        q = select(InterviewParticipant.interview_id, InterviewParticipant.user_id).where(
            InterviewParticipant.interview_id.in_([r.id for r in rows]), InterviewParticipant.user_id.is_not(None),
            InterviewParticipant.removed_at.is_(None),
        )  # fmt: skip
        for iid, uid in (await db.execute(q)).all():
            users.setdefault(iid, []).append(uid)
    await expose(principal, "candidate", [r.candidate_id for r in rows])
    return finish_all(principal, [
        {**base(i, "interview", human_id=i.human_id), "application_id": i.application_id, "candidate_id": i.candidate_id,
         "demand_id": i.demand_id, "stage_name": i.stage_name, "round": i.round, "starts_at": i.starts_at, "ends_at": i.ends_at,
         "timezone": i.timezone, "mode": i.mode, "status": i.status, "participant_user_ids": users.get(i.id, [])}
        for i in rows
    ])  # fmt: skip


async def list_interviews(db: AsyncSession, *, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    return await page(db, _interviews(), sort_col=Interview.updated_at, id_col=Interview.id, limit=limit, cursor=cursor, order=order,
                      build=lambda r: _build_interviews(db, r))  # fmt: skip


async def get_interview(db: AsyncSession, interview_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_interviews(db, [await one(db, _interviews().where(Interview.id == interview_id))]))[0]


# ================================================================ offers (status only)
def _offers() -> Any:
    _, principal = ctx_principal()
    return visible_offers(principal)


async def _build_offers(rows: Sequence[Offer]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    return finish_all(principal, [
        {**base(o, "offer", human_id=o.human_id), "application_id": o.application_id, "status": o.status,
         "status_changed_at": o.responded_at or o.released_at or o.updated_at, "expires_at": o.expires_at}
        for o in rows
    ])  # fmt: skip


async def list_offers(db: AsyncSession, *, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    return await page(db, _offers(), sort_col=Offer.updated_at, id_col=Offer.id, limit=limit, cursor=cursor, order=order, build=_build_offers)


async def get_offer(db: AsyncSession, offer_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_offers([await one(db, _offers().where(Offer.id == offer_id))]))[0]


# ================================================================ placements / workers
def placement_condition(permission: str) -> Any:
    _, principal = ctx_principal()
    a = scope_condition(principal, permission, Placement.recruiter_user_id)
    if a is None:
        return None
    b = scope_condition(principal, permission, Placement.account_manager_user_id)
    return or_(a, b) if b is not None else a


def _placements(permission: str = "placement:read") -> Any:
    cond = placement_condition(permission)
    stmt = select(Placement)
    return stmt if cond is None else stmt.where(cond)


async def _build_placements(rows: Sequence[Placement]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    await expose(principal, "candidate", [r.candidate_id for r in rows])
    return finish_all(principal, [
        {**base(p, "placement", human_id=p.human_id), "candidate_id": p.candidate_id, "client_id": p.client_id,
         "legal_entity_id": p.legal_entity_id, "status": p.status, "role_title": p.role_title, "worker_type": p.worker_type,
         "planned_start_date": p.planned_start_date, "planned_end_date": p.planned_end_date,
         "actual_start_date": p.actual_start_date, "end_date": p.end_date}
        for p in rows
    ])  # fmt: skip


async def list_placements(db: AsyncSession, *, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    return await page(db, _placements(), sort_col=Placement.updated_at, id_col=Placement.id, limit=limit, cursor=cursor, order=order, build=_build_placements)


async def get_placement(db: AsyncSession, placement_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_placements([await one(db, _placements().where(Placement.id == placement_id))]))[0]


def _workers() -> Any:
    _, principal = ctx_principal()
    cond = placement_condition("worker:read")
    if cond is None:
        return select(WorkerProfile)
    managed = scope_condition(principal, "worker:read", WorkerProfile.manager_user_id)
    via = WorkerProfile.candidate_id.in_(select(Placement.candidate_id).where(cond))
    return select(WorkerProfile).where(or_(via, managed) if managed is not None else via)


async def _build_workers(rows: Sequence[WorkerProfile]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    await expose(principal, "candidate", [r.candidate_id for r in rows])
    return finish_all(principal, [
        {**base(w, "worker", human_id=w.worker_human_id), "candidate_id": w.candidate_id, "worker_type": w.default_worker_type,
         "manager_user_id": w.manager_user_id, "pan": w.pan_last4 and f"XXXXXX{w.pan_last4}"}
        for w in rows
    ])  # fmt: skip


async def list_workers(db: AsyncSession, *, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    return await page(db, _workers(), sort_col=WorkerProfile.updated_at, id_col=WorkerProfile.id, limit=limit, cursor=cursor, order=order, build=_build_workers)


async def get_worker(db: AsyncSession, worker_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_workers([await one(db, _workers().where(WorkerProfile.id == worker_id))]))[0]


__all__ = [
    "get_interview", "get_offer", "get_placement", "get_submission", "get_worker", "list_interviews", "list_offers",
    "list_placements", "list_submissions", "list_workers", "placement_condition",
]  # fmt: skip
