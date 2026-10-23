# ruff: file-ignore[line-too-long]
"""AEDT score suppressor (Epic 49 A8, coordinator decision 15).

Registered into P1 ``matching.ports`` from ``hooks.py``. For ``(candidate_id, demand_id)`` pairs it returns:

* ``"alternative_process"`` -- the Candidate requested an alternative selection process for that Demand;
* ``"notice_period"`` -- the Demand is LL144-flagged (tenant ``ll144_enabled``) and still inside the notice period.

Reads only the fairness-owned flag / request tables and settings (never self-ID data).
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fairness import AedtAlternativeRequest, FairnessSettings
from app.services.fairness import ll144
from app.services.fairness.common import now

Pair = tuple[uuid.UUID, uuid.UUID]


async def suppressor(db: AsyncSession, pairs: Sequence[Pair]) -> dict[Pair, str]:
    wanted = [(c, d) for c, d in pairs if c is not None and d is not None]
    if not wanted:
        return {}
    demand_ids = list({d for _, d in wanted})
    out: dict[Pair, str] = {}
    requested = set(
        (
            await db.execute(
                select(AedtAlternativeRequest.candidate_id, AedtAlternativeRequest.demand_id).where(
                    AedtAlternativeRequest.demand_id.in_(demand_ids),
                    AedtAlternativeRequest.candidate_id.in_(list({c for c, _ in wanted})),
                )
            )
        ).tuples()
    )
    for pair in wanted:
        if pair in requested:
            out[pair] = "alternative_process"
    flags = await ll144.flags_for(db, demand_ids)
    if flags:
        s = await db.get(FairnessSettings, uuid.UUID(str(db.sync_session.info["tenant_id"])))
        if s is not None and s.ll144_enabled:
            at = now()
            in_period = {
                d
                for d, f in flags.items()
                if at < await ll144.period_end(db, f.notice_published_at, s.notice_lead_business_days)
            }
            for pair in wanted:
                if pair[1] in in_period:
                    out.setdefault(pair, "notice_period")
    return out
