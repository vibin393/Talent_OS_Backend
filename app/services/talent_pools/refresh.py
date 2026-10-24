# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Dynamic Pool refresh, diff and triggers (Epic 38 story 38.3, FR-192, FR-194, NFR-39).

* ``request_refresh`` records a ``running`` refresh row and emits ``PoolRefreshRequested``; the after-commit
  handler (``hooks.py``) enqueues ``talent_pools.refresh`` on queue ``maintenance``. A Pool with a running refresh
  returns that refresh (one refresh per Pool at a time); a manual refresh within 5 minutes of the last one is 429.
* ``run_refresh`` (task body) evaluates the Segment with ``SemanticContext.for_system(tenant, db, "refresh")``
  (tenant-wide, RLS on; field permissions were enforced against the author at save time) inside the foundation's
  read-only savepoint, then diffs set-based in the writable outer transaction: removed members get
  ``removed_at`` / ``removed_refresh_id``, new ids are inserted with ``added_refresh_id`` and the shared reason;
  unchanged rows are not rewritten. A Redis lock ``pool_refresh:<pool_id>`` guards concurrent workers.
* Segments that no longer validate (``semantic_unknown_*``) are flagged ``needs_repair``; their Pools skip
  (``skipped_needs_repair``) and the owners are notified. A ``query_timeout`` fails the refresh (owner notified).
"""

import time
import uuid
from datetime import timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError, RateLimited
from app.core.events import emit
from app.core.semantic import SemanticError, SemanticTimeout, validate
from app.core.skill_refs import SkillKey
from app.events.talent_pools import PoolMembershipChanged
from app.models.talent_pools import Segment, TalentPool, TalentPoolMember, TalentPoolRefresh
from app.services.segments import definitions as segment_definitions
from app.services.segments import repair as segment_repair
from app.services.segments import service as segments
from app.services.talent_pools import common
from app.services.talent_pools.events import PoolRefreshRequested

BATCH = 5_000
MANUAL_COOLDOWN = timedelta(minutes=5)
STALE_RUNNING = timedelta(minutes=30)
LOCK_TTL_S = 1800


# ---------------------------------------------------------------- redis lock (best effort)
_redis_client: Any = None


def _redis() -> Any:
    global _redis_client  # process-wide lazy client
    if _redis_client is None:
        import redis

        from app.core.settings import get_settings

        s = get_settings()
        _redis_client = redis.Redis(
            host=s.redis_host,
            port=s.redis_port,
            password=s.redis_password or None,
            db=2,
            decode_responses=True,
            socket_connect_timeout=1,
            socket_timeout=2,
        )
    return _redis_client


def _lock(pool_id: uuid.UUID, token: str) -> bool | None:
    """True = acquired, False = held elsewhere, None = Redis unavailable (DB status still guards)."""
    try:
        return bool(_redis().set(f"pool_refresh:{pool_id}", token, nx=True, ex=LOCK_TTL_S))
    except Exception as exc:
        logger.warning("pool refresh lock unavailable ({}); continuing", type(exc).__name__)
        return None


def _unlock(pool_id: uuid.UUID, token: str) -> None:
    try:
        client = _redis()
        key = f"pool_refresh:{pool_id}"
        if client.get(key) == token:
            client.delete(key)
    except Exception:  # pragma: no cover - best effort
        logger.debug("pool refresh unlock failed")


# ---------------------------------------------------------------- requests
async def running_refresh(db: AsyncSession, pool_id: uuid.UUID) -> TalentPoolRefresh | None:
    return (
        await db.execute(
            select(TalentPoolRefresh)
            .where(
                TalentPoolRefresh.pool_id == pool_id,
                TalentPoolRefresh.status == "running",
                TalentPoolRefresh.started_at > func.now() - STALE_RUNNING,
            )
            .order_by(TalentPoolRefresh.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def request_refresh(
    db: AsyncSession, pool: TalentPool, trigger: str, *, cooldown: bool = False
) -> TalentPoolRefresh:
    """Start (or join) a refresh of a dynamic Pool."""
    if pool.kind != "dynamic":
        raise ConflictError("Only dynamic pools are refreshed", code="pool_is_static")
    common.require_active(pool)
    running = await running_refresh(db, pool.id)
    if running is not None:
        return running
    if cooldown:
        last = (
            await db.execute(
                select(func.max(TalentPoolRefresh.started_at)).where(
                    TalentPoolRefresh.pool_id == pool.id, TalentPoolRefresh.trigger == "manual"
                )
            )
        ).scalar_one_or_none()
        if last is not None and last > common.now() - MANUAL_COOLDOWN:
            raise RateLimited(
                "This pool was refreshed less than 5 minutes ago",
                code="refresh_too_soon",
                details={"retry_after": 300},
            )
    row = TalentPoolRefresh(
        id=uuid.uuid4(), tenant_id=pool.tenant_id, pool_id=pool.id, trigger=trigger, status="running"
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    emit(
        db,
        PoolRefreshRequested(tenant_id=pool.tenant_id, pool_id=pool.id, refresh_id=row.id, trigger=trigger),
    )
    return row


# ---------------------------------------------------------------- notifications
async def _notify(
    db: AsyncSession, type_key: str, pool: TalentPool, refresh: TalentPoolRefresh, **extra: Any
) -> None:
    from app.services import notifications_service

    payload = {
        "tenant_id": str(pool.tenant_id),
        "pool_id": str(pool.id),
        "refresh_id": str(refresh.id),
        "owner_id": str(pool.owner_id),
        "event_id": f"{type_key}:{refresh.id}",
        **extra,
    }
    try:
        await notifications_service.fan_out(db, type_key, payload)
    except Exception:  # pragma: no cover - a notification never fails a refresh
        logger.exception("talent pool notification {} failed", type_key)


# ---------------------------------------------------------------- runner
def _chunks(items: list[uuid.UUID], size: int = BATCH) -> list[list[uuid.UUID]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


async def _finish(  # NOSONAR
    refresh: TalentPoolRefresh, status: str, started: float, error_code: str | None = None
) -> None:
    refresh.status = status
    refresh.error_code = error_code
    refresh.finished_at = common.now()
    refresh.duration_ms = int((time.monotonic() - started) * 1000)


async def run_refresh(db: AsyncSession, tenant_id: uuid.UUID, refresh_id: uuid.UUID) -> dict[str, Any]:
    """Task body of ``talent_pools.refresh`` (inside a tenant session that commits on success)."""
    started = time.monotonic()
    refresh = await db.get(TalentPoolRefresh, refresh_id)
    if refresh is None or refresh.status != "running":
        return {"status": "skipped"}
    pool = (
        await db.execute(select(TalentPool).where(TalentPool.id == refresh.pool_id).with_for_update())
    ).scalar_one_or_none()
    if pool is None or pool.kind != "dynamic" or pool.segment_id is None or pool.status != "active":
        await _finish(refresh, "failed", started, "pool_unavailable")
        return {"status": "failed"}
    token = uuid.uuid4().hex
    if _lock(pool.id, token) is False:
        return {"status": "locked"}
    try:
        return await _run(db, tenant_id, pool, refresh, started)
    finally:
        _unlock(pool.id, token)


async def _run(
    db: AsyncSession, tenant_id: uuid.UUID, pool: TalentPool, refresh: TalentPoolRefresh, started: float
) -> dict[str, Any]:
    segment = await db.get(Segment, pool.segment_id)
    if segment is None:
        await _finish(refresh, "failed", started, "segment_missing")
        return {"status": "failed"}
    issues = segment_repair.issues_of(segment)
    if issues:
        segment_repair.flag(segment, issues)
    if segment.needs_repair:
        pool.refresh_due_at = None
        await _finish(refresh, "skipped_needs_repair", started, "segment_needs_repair")
        await db.flush()
        await _notify(db, "pool_needs_repair", pool, refresh, segment_id=str(segment.id))
        return {"status": "skipped_needs_repair"}
    defn = validate(segment.definition)
    sys_ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    try:
        evaluation = await segments.evaluate(sys_ctx, db, segment.id, mode="ids")
    except SemanticTimeout:
        pool.refresh_due_at = None
        await _finish(refresh, "failed", started, "query_timeout")
        await db.flush()
        await _notify(db, "pool_refresh_failed", pool, refresh, error_code="query_timeout")
        return {"status": "failed", "error_code": "query_timeout"}
    except SemanticError as exc:
        segment_repair.flag(segment, [segment_repair.issue(exc)])
        await _finish(refresh, "skipped_needs_repair", started, "segment_needs_repair")
        await db.flush()
        await _notify(db, "pool_needs_repair", pool, refresh, segment_id=str(segment.id))
        return {"status": "skipped_needs_repair"}
    reason = await segments.reason_for(sys_ctx, db, defn, segment.id, segment.definition_version)
    new_ids = set(evaluation.subject_ids)
    m = TalentPoolMember
    current = set(
        (
            await db.execute(select(m.candidate_id).where(m.pool_id == pool.id, m.removed_at.is_(None)))
        ).scalars()
    )
    to_remove = sorted(current - new_ids)
    to_add = sorted(new_ids - current)
    at = common.now()
    for batch in _chunks(to_remove):
        await db.execute(
            update(m)
            .where(m.pool_id == pool.id, m.removed_at.is_(None), m.candidate_id.in_(batch))
            .values(removed_at=at, removed_refresh_id=refresh.id)
        )
    for batch in _chunks(to_add):
        await db.execute(
            insert(m),
            [
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "pool_id": pool.id,
                    "candidate_id": cid,
                    "source": "segment",
                    "reason": reason,
                    "added_at": at,
                    "added_refresh_id": refresh.id,
                }
                for cid in batch
            ],
        )
    pool.member_count = len(new_ids)
    pool.last_diff = {"added": len(to_add), "removed": len(to_remove)}
    pool.last_refreshed_at = at
    pool.refresh_due_at = None
    refresh.added = len(to_add)
    refresh.removed = len(to_remove)
    refresh.evaluated = len(new_ids)
    await _finish(refresh, "done", started)
    audit.record(
        "talent_pool.refreshed",
        "talent_pool",
        pool.id,
        session=db,
        refresh_id=str(refresh.id),
        trigger=refresh.trigger,
        added=len(to_add),
        removed=len(to_remove),
        evaluated=len(new_ids),
    )
    await db.flush()
    emit(
        db,
        PoolMembershipChanged(
            tenant_id=tenant_id,
            pool_id=pool.id,
            refresh_id=refresh.id,
            added_count=len(to_add),
            removed_count=len(to_remove),
            changed_at=at,
        ),
    )
    return {"status": "done", "added": len(to_add), "removed": len(to_remove)}


# ---------------------------------------------------------------- triggers
async def request_due(db: AsyncSession) -> int:
    """Beat ``talent_pools.due``: refresh dynamic Pools marked due (skill graph changes)."""
    pools = list(
        (
            await db.execute(
                select(TalentPool).where(
                    TalentPool.kind == "dynamic",
                    TalentPool.status == "active",
                    TalentPool.refresh_due_at.is_not(None),
                    TalentPool.refresh_due_at <= func.now(),
                )
            )
        ).scalars()
    )
    for pool in pools:
        await request_refresh(db, pool, "skill_graph")
    return len(pools)


async def request_nightly(db: AsyncSession) -> int:
    pools = list(
        (
            await db.execute(
                select(TalentPool).where(TalentPool.kind == "dynamic", TalentPool.status == "active")
            )
        ).scalars()
    )
    for pool in pools:
        await request_refresh(db, pool, "nightly")
    return len(pools)


async def mark_due_for_graph_change(
    db: AsyncSession, tenant_id: uuid.UUID, scope: str, skill_keys: list[str]
) -> int:
    """``SkillGraphChanged`` handler body: mark dynamic Pools whose Segment expands an affected skill."""
    rows = (
        await db.execute(
            select(TalentPool, Segment)
            .join(Segment, Segment.id == TalentPool.segment_id)
            .where(TalentPool.kind == "dynamic", TalentPool.status == "active")
        )
    ).all()
    changed = set(skill_keys)
    affected_by_change: set[str] | None = None
    marked = 0
    for pool, segment in rows:
        try:
            defn = validate(segment.definition)
        except SemanticError:
            continue
        refs = segment_definitions.expanding_skill_refs(defn)
        if not refs:
            continue
        hit = scope == "platform" or not changed or bool(changed & set(refs))
        if not hit:
            if affected_by_change is None:
                affected_by_change = await _expanded_keys(db, tenant_id, sorted(changed))
            leaf_expansion = await _expanded_keys(db, tenant_id, refs)
            hit = bool(set(refs) & affected_by_change) or bool(leaf_expansion & changed)
        if hit:
            pool.refresh_due_at = common.now()
            marked += 1
    await db.flush()
    return marked


async def _expanded_keys(db: AsyncSession, tenant_id: uuid.UUID, keys: list[str]) -> set[str]:
    from app.services import skills_service

    refs: list[SkillKey] = []
    for k in keys:
        try:
            refs.append(SkillKey.parse(k))
        except ValueError:
            continue
    if not refs:
        return set()
    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    return {e.key.as_str() for e in await skills_service.expand(ctx, db, refs, depth=1)}


async def list_refreshes(db: AsyncSession, pool_id: uuid.UUID, limit: int = 50) -> list[TalentPoolRefresh]:
    return list(
        (
            await db.execute(
                select(TalentPoolRefresh)
                .where(TalentPoolRefresh.pool_id == pool_id)
                .order_by(TalentPoolRefresh.started_at.desc())
                .limit(limit)
            )
        ).scalars()
    )
