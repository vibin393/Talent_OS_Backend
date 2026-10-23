# ruff: file-ignore[line-too-long]
"""Payout export and mark-paid for rewards not paid through payroll (Epic 42 A6): User referrers and Workers
without a payroll Placement. ``referral:manage`` + ``report:export``; the P1 export writer (``XlsxWriter`` writes
string cells, ``csv_cell`` prefixes formula-like values with ``'``)."""

import uuid
from datetime import date
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.events import emit
from app.models.referrals import Referral, ReferralReward
from app.schemas.referrals import MarkPaidOut
from app.services.referrals.common import REF_MANAGE, conflict, now, require
from app.services.referrals.events import ReferralRewardPaid

HEADER = [
    "Reward ID",
    "Referral",
    "Referrer type",
    "Referrer",
    "Referrer email",
    "Referred person",
    "Amount",
    "Currency",
    "Approved on",
]


async def _rows(db: AsyncSession, status: str, channel: str) -> list[list[object]]:
    from app.models.candidates import Candidate
    from app.models.identity import User
    from app.services.candidates import portal_port

    rows = (
        await db.execute(
            select(ReferralReward, Referral)
            .join(Referral, Referral.id == ReferralReward.referral_id)
            .where(ReferralReward.status == status, ReferralReward.payout_channel == channel)
            .order_by(ReferralReward.approved_at, ReferralReward.id)
        )
    ).tuples()
    out: list[list[object]] = []
    for reward, referral in rows:
        if reward.referrer_kind == "user":
            u = await db.get(User, reward.referrer_user_id)
            name, email = (u.name, str(u.email)) if u else (None, None)
        else:
            c = await db.get(Candidate, reward.referrer_candidate_id)
            name = c.full_name if c else None
            email = await portal_port.primary_email(db, reward.referrer_candidate_id) if c else None  # type: ignore[arg-type]
        out.append(
            [
                str(reward.id),
                referral.human_id,
                "Employee" if reward.referrer_kind == "user" else "Worker",
                name,
                email,
                referral.referee_name,
                reward.amount,
                reward.currency,
                reward.approved_at.date().isoformat() if reward.approved_at else None,
            ]
        )
    return out


async def payout_export(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    status: str = "approved",
    channel: str = "payout_export",
    fmt: Literal["xlsx", "csv"] = "xlsx",
) -> tuple[bytes, str, str]:
    """``(content, media type, filename)``."""
    from app.services.reporting.export import XlsxWriter, csv_line

    require(ctx, REF_MANAGE)
    require(ctx, "report:export")
    rows = await _rows(db, status, channel)
    stamp = date.today().isoformat()
    audit.record("referral.payout_exported", "referral_reward", None, session=db, rows=len(rows), format=fmt)
    if fmt == "csv":
        body = "".join([csv_line(HEADER), *(csv_line(r) for r in rows)]).encode("utf-8")
        return body, "text/csv; charset=utf-8", f"referral-payouts-{stamp}.csv"
    writer = XlsxWriter("Referral payouts", HEADER)
    for r in rows:
        writer.append(r)
    return (
        writer.close(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        f"referral-payouts-{stamp}.xlsx",
    )


async def mark_paid(
    ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID], paid_on: date, payout_ref: str
) -> MarkPaidOut:
    """``approved`` payout-export rewards -> ``paid`` (user action). Payroll rewards are paid by the Payroll
    Export only (409 ``paid_through_payroll``)."""
    from datetime import UTC, datetime, time

    require(ctx, REF_MANAGE)
    rows = list(
        (
            await db.execute(select(ReferralReward).where(ReferralReward.id.in_(ids)).with_for_update())
        ).scalars()
    )
    if len(rows) != len(set(ids)):
        raise conflict("reward_not_found", "Some rewards were not found")
    for r in rows:
        if r.payout_channel != "payout_export":
            raise conflict("paid_through_payroll", "This reward is paid through payroll", reward_id=str(r.id))
        if r.status != "approved":
            raise conflict(
                "reward_not_approved", "Only approved rewards can be marked paid", reward_id=str(r.id)
            )
    paid_at = datetime.combine(paid_on, time(12, 0), tzinfo=UTC) if paid_on else now()
    for r in rows:
        r.status = "paid"
        r.paid_at = paid_at
        r.payout_ref = payout_ref.strip()[:100]
        emit(
            db,
            ReferralRewardPaid(
                tenant_id=r.tenant_id,
                reward_id=r.id,
                referral_id=r.referral_id,
                referrer_kind=r.referrer_kind,
                referrer_id=r.referrer_user_id or r.referrer_candidate_id,  # type: ignore[arg-type]
            ),
        )
        audit.record("referral.reward_paid", "referral_reward", r.id, session=db, payout_ref=r.payout_ref)
    await db.flush()
    return MarkPaidOut(updated=len(rows))
