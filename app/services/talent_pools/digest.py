# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Daily new-member digest and the tenant-local beat schedule (Epic 38 story 38.3, FR-194).

* ``send_digest``: **one** ``pool_new_members`` notification per subscriber listing each subscribed Pool with
  the members added since the subscription's ``last_digest_at``, counted within the subscriber's own candidate
  scope; Pools with zero additions are skipped (a subscriber with none gets nothing).
* ``tick`` (beat ``talent_pools.tick``, every 15 minutes, queue ``maintenance``): at the tenant-local
  ``nightly_refresh_local_time`` (once per local date) the segment repair check and the nightly refresh of every
  dynamic Pool; at ``digest_local_time`` the digest; every tick, Pools marked due by a skills-graph change.
"""

import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.candidates import Candidate
from app.models.talent_pools import TalentPool, TalentPoolMember, TalentPoolSubscription
from app.services.talent_pools import common, refresh
from app.services.talent_pools import settings as pool_settings


async def _principal(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> Any:
    from app.services import entity_refs

    return await entity_refs.principal_for_user(db, tenant_id, user_id)


async def send_digest(db: AsyncSession, tenant_id: uuid.UUID, at: datetime | None = None) -> int:
    """Returns the number of notifications sent."""
    from app.services import notifications_service

    now = at or common.now()
    rows = (
        await db.execute(
            select(TalentPoolSubscription, TalentPool)
            .join(TalentPool, TalentPool.id == TalentPoolSubscription.pool_id)
            .where(TalentPool.status == "active")
            .order_by(TalentPoolSubscription.user_id, TalentPool.name)
        )
    ).all()
    by_user: dict[uuid.UUID, list[tuple[TalentPoolSubscription, TalentPool]]] = defaultdict(list)
    for sub, pool in rows:
        by_user[sub.user_id].append((sub, pool))
    sent = 0
    for user_id, subs in by_user.items():
        principal = await _principal(db, tenant_id, user_id)
        if principal is None:
            continue
        viewer = RequestContext(tenant_id=tenant_id, principal=principal)
        visible_ids = set(
            (
                await db.execute(
                    select(TalentPool.id).where(
                        TalentPool.id.in_([p.id for _, p in subs]), common.visible(principal)
                    )
                )
            ).scalars()
        )
        listed: list[dict[str, Any]] = []
        for sub, pool in subs:
            if pool.id not in visible_ids:
                continue
            m = TalentPoolMember
            since = sub.last_digest_at or sub.created_at
            added = int(
                (
                    await db.execute(
                        select(func.count())
                        .select_from(m)
                        .join(Candidate, Candidate.id == m.candidate_id)
                        .where(
                            m.pool_id == pool.id,
                            m.removed_at.is_(None),
                            m.added_at > since,
                            m.added_at <= now,
                            common.member_scope(viewer),
                        )
                    )
                ).scalar_one()
            )
            sub.last_digest_at = now
            if added > 0:
                listed.append({"pool_id": str(pool.id), "name": pool.name, "added": added})
        if not listed:
            continue
        payload = {
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "pools": listed,
            "event_id": f"pool_new_members:{user_id}:{now.date().isoformat()}",
        }
        try:
            sent += 1 if await notifications_service.fan_out(db, "pool_new_members", payload) else 0
        except Exception:  # pragma: no cover - never break the digest for one user
            logger.exception("pool digest failed for a subscriber")
    await db.flush()
    return sent


def _due(local: datetime, hhmm: str, last_on: str | None) -> bool:
    hour, minute = (int(x) for x in hhmm.split(":"))
    return (local.hour, local.minute) >= (hour, minute) and last_on != local.date().isoformat()


async def tick(db: AsyncSession, tenant_id: uuid.UUID, at: datetime | None = None) -> dict[str, int]:
    from app.services import directory_service
    from app.services.segments import repair

    now = at or common.now()
    tz = ZoneInfo(await directory_service.tenant_timezone(db, tenant_id) or "Asia/Kolkata")
    local = now.astimezone(tz)
    cfg = await pool_settings.get_row(db, tenant_id)
    out = {"nightly": 0, "digest": 0, "due": 0}
    if _due(local, cfg.nightly_refresh_local_time, cfg.last_nightly_on):
        cfg.last_nightly_on = local.date().isoformat()
        await repair.check_all(db)
        out["nightly"] = await refresh.request_nightly(db)
    if _due(local, cfg.digest_local_time, cfg.last_digest_on):
        cfg.last_digest_on = local.date().isoformat()
        out["digest"] = await send_digest(db, tenant_id, now)
    out["due"] = await refresh.request_due(db)
    await db.flush()
    return out
