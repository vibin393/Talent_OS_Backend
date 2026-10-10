# ruff: noqa: E501
"""Referrals dashboard tiles (Epic 52 A4; Epic 42 data). Row scope is the owner's list rule
(``referrals.common.scoped``: the Demand's owner / BU; general referrals need tenant scope), so counts equal
``GET /referrals`` totals. Referrer-safe columns only (no feedback, no compensation besides masked reward amounts)."""

from typing import Any

from sqlalchemy import func, select

from app.models.demands import Demand
from app.models.referrals import Referral, ReferralReward
from app.services.reporting.metrics import TileContext, TileData, pct
from app.services.reporting.metrics.p3common import comp, label, money, need, range_link

FUNNEL = ("submitted", "accepted", "in_process", "hired", "not_selected", "duplicate")


def scoped_ids(t: TileContext, *conds: Any) -> Any:
    from app.services.referrals.common import scoped

    stmt = select(Referral.id).outerjoin(Demand, Demand.id == Referral.demand_id).where(*conds)
    return scoped(t.ctx, stmt)


def _in_range(t: TileContext) -> list[Any]:
    f = t.filters
    return [Referral.created_at >= f.start, Referral.created_at < f.end]


async def referral_funnel(t: TileContext) -> TileData:
    need(t, "referrals")
    ids = scoped_ids(t, *_in_range(t))
    counts = dict(
        (
            await t.db.execute(
                select(Referral.status, func.count()).where(Referral.id.in_(ids)).group_by(Referral.status)
            )
        )
        .tuples()
        .all()
    )
    rows = [{"key": k, "label": label(k), "value": int(counts.get(k, 0))} for k in FUNNEL]
    total = sum(int(v) for v in counts.values())
    return {
        "value": total,
        "rows": rows,
        "row_links": [range_link(t, "/referrals", status=k) for k in FUNNEL],
        "link": range_link(t, "/referrals"),
    }


async def referral_share_of_hires(t: TileContext) -> TileData:
    """SM-23: hires from referrals (referral ``hired``, decided in range) / all hires in range in the caller's scope."""
    need(t, "referrals")
    from app.models.pipeline import ApplicationStageHistory, PipelineStage
    from app.services.reporting.common import scoped_demand_ids

    f = t.filters
    referred = int(
        (
            await t.db.execute(
                select(func.count()).where(
                    Referral.id.in_(
                        scoped_ids(
                            t,
                            Referral.status == "hired",
                            Referral.decided_at >= f.start,
                            Referral.decided_at < f.end,
                        )
                    )
                )
            )
        ).scalar_one()
    )
    from app.models.pipeline import Application

    hires = int(
        (await t.db.execute(
            select(func.count(func.distinct(ApplicationStageHistory.application_id)))
            .join(PipelineStage, PipelineStage.id == ApplicationStageHistory.to_stage_id)
            .join(Application, Application.id == ApplicationStageHistory.application_id)
            .where(PipelineStage.key == "hired", ApplicationStageHistory.at >= f.start, ApplicationStageHistory.at < f.end,
                   Application.demand_id.in_(scoped_demand_ids(t.principal, "application:read")))
        )).scalar_one()
    )  # fmt: skip
    return {"value": pct(referred, hires), "unit": "%", "n": hires, "rows": [
        {"key": "referral_hires", "label": "Referral hires", "value": referred},
        {"key": "all_hires", "label": "All hires", "value": hires},
    ]}  # fmt: skip


async def rewards_by_status(t: TileContext) -> TileData:
    need(t, "referral_rewards", "referrals")
    rows_db = (
        await t.db.execute(
            select(
                ReferralReward.status, ReferralReward.currency, func.count(), func.sum(ReferralReward.amount)
            )
            .where(ReferralReward.referral_id.in_(scoped_ids(t)))
            .group_by(ReferralReward.status, ReferralReward.currency)
        )
    ).all()
    show = comp(t)
    rows = []
    for status, currency, n, total in sorted(rows_db, key=lambda r: (r[0], r[1] or "")):
        row: dict[str, Any] = {"key": status, "label": label(status), "value": int(n)}
        if show:
            row["money"] = money(total, currency)
        rows.append(row)
    return {
        "value": sum(r["value"] for r in rows),
        "rows": rows,
        "masked": not show,
        "link": {"path": "/referrals/rewards", "query": {}},
    }


async def top_referrers(t: TileContext) -> TileData:
    need(t, "referrals")
    from app.models.candidates import Candidate
    from app.services.reporting.common import users

    who = func.coalesce(Referral.referrer_user_id, Referral.referrer_candidate_id)
    rows_db = (
        await t.db.execute(
            select(Referral.referrer_kind, who, func.count(), func.count().filter(Referral.status == "hired"))
            .where(Referral.id.in_(scoped_ids(t, *_in_range(t))))
            .group_by(Referral.referrer_kind, who)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()
    user_ids = [r[1] for r in rows_db if r[0] == "user"]
    cand_ids = [r[1] for r in rows_db if r[0] != "user"]
    names: dict[Any, str] = {}
    if user_ids:
        names |= dict(
            (await t.db.execute(select(users.c.id, users.c.name).where(users.c.id.in_(user_ids))))
            .tuples()
            .all()
        )
    if cand_ids:
        names |= dict(
            (await t.db.execute(select(Candidate.id, Candidate.full_name).where(Candidate.id.in_(cand_ids))))
            .tuples()
            .all()
        )
    rows = [
        {
            "referrer": names.get(rid) or "-",
            "referrer_kind": "Employee" if kind == "user" else "Worker",
            "referrals": int(n),
            "hired": int(h),
        }
        for kind, rid, n, h in rows_db
    ]
    return {"value": len(rows), "rows": rows}
