# ruff: file-ignore[line-too-long]
"""Pull-only posting source ``referral_reward`` (Epic 42 A6, AD-23), ledger ``payable`` only.

The loader returns ``approved`` rewards with ``payout_channel='payroll'``; ``postings.load_unposted`` drops those
already active in the ledger. Each item is one fixed amount on the Worker referrer's own payroll Placement
(``payee_placement_id``), dated the approval's local date. P2 payroll prices ``referral_reward`` as a one-off
(``earnings.ONE_OFF_KINDS``): the amount lands in the adjustments column, never as hours.

``PayrollExportFinalized`` -> ``mark_paid_for_export`` marks the rewards posted to that export version ``paid``.
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import emit
from app.core.fiscal import today_for
from app.models.placements import Placement
from app.models.referrals import ReferralReward
from app.services.postings import SourceItem, SourceQuery
from app.services.referrals.common import POSTING_SOURCE, TZ, now
from app.services.referrals.events import ReferralRewardPaid


def _local_date(reward: ReferralReward) -> date:
    from zoneinfo import ZoneInfo

    if reward.approved_at is None:  # pragma: no cover - approved rewards carry a timestamp
        return today_for(TZ)
    return reward.approved_at.astimezone(ZoneInfo(TZ)).date()


async def load_rewards(ctx: RequestContext, db: AsyncSession, query: SourceQuery) -> list[SourceItem]:  # ruff: ignore[unused-function-argument]
    if query.ledger != "payable":
        return []
    stmt = (
        select(ReferralReward, Placement)
        .join(Placement, Placement.id == ReferralReward.payee_placement_id)
        .where(ReferralReward.status == "approved", ReferralReward.payout_channel == "payroll")
        .order_by(ReferralReward.approved_at, ReferralReward.id)
    )
    if query.placement_ids is not None:
        stmt = stmt.where(ReferralReward.payee_placement_id.in_(list(query.placement_ids)))
    if query.legal_entity_id is not None:
        stmt = stmt.where(Placement.legal_entity_id == query.legal_entity_id)
    if query.payee_kind is not None:
        stmt = stmt.where(Placement.payee_kind == query.payee_kind)
    out: list[SourceItem] = []
    for reward, placement in (await db.execute(stmt)).tuples():
        item_date = _local_date(reward)
        if item_date > query.period_end or (
            query.period_start is not None and item_date < query.period_start
        ):  # type: ignore[operator]
            continue
        out.append(
            SourceItem(
                POSTING_SOURCE,
                reward.id,
                placement.id,
                item_date,
                Decimal(1),
                Decimal(reward.amount),
                reward.currency,
                {
                    "candidate_id": reward.referrer_candidate_id,
                    "approved_at": reward.approved_at,
                    "category": POSTING_SOURCE,
                    "payable": True,
                },
            )
        )
    return out


def register() -> None:
    from app.services.postings import posting_sources, register_posting_source

    if POSTING_SOURCE not in posting_sources():
        register_posting_source(POSTING_SOURCE, load_rewards, ledgers=frozenset({"payable"}))


async def mark_paid_for_export(db: AsyncSession, export_version_id: uuid.UUID) -> int:
    """Rewards posted to ``export_version_id`` (``postings.targets_for``) -> ``paid``."""
    from app.services import postings

    rows = list(
        (
            await db.execute(
                select(ReferralReward)
                .where(ReferralReward.status == "approved", ReferralReward.payout_channel == "payroll")
                .with_for_update()
            )
        ).scalars()
    )
    n = 0
    for reward in rows:
        targets = await postings.targets_for(db, "payable", POSTING_SOURCE, reward.id)
        if not any(t.document_id == export_version_id and t.state == "posted" for t in targets):
            continue
        reward.status = "paid"
        reward.paid_at = now()
        reward.payout_ref = str(export_version_id)
        emit(
            db,
            ReferralRewardPaid(
                tenant_id=reward.tenant_id,
                reward_id=reward.id,
                referral_id=reward.referral_id,
                referrer_kind=reward.referrer_kind,
                referrer_id=reward.referrer_user_id or reward.referrer_candidate_id,  # type: ignore[arg-type]
            ),
        )
        n += 1
    await db.flush()
    return n
