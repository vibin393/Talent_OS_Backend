# ruff: noqa: E501
"""Registrations of the ``talent_pools`` domain (autodiscovered by ``core.hooks``), Epic 38 A8.

* Rediscovery section ``pools`` (``matching.rediscovery.register_rediscovery_section``; matching never imports us).
* Workflow: action ``talent_pools.add_to_pool`` (static Pools only), trigger ``talent_pools.membership_changed``
  (entity ``talent_pool``, event ``PoolMembershipChanged``) and its snapshot loader.
* Notification types ``pool_new_members`` (the daily digest, section 0.7) and the domain-local
  ``pool_needs_repair`` / ``pool_refresh_failed`` (sent by the refresh task through ``fan_out``).
* Events: ``PoolRefreshRequested`` -> ``talent_pools.refresh``; ``SkillGraphChanged`` -> ``talent_pools.graph_changed``
  (marks dynamic Pools with expanding ``skill_match`` leaves due; the 15-minute beat refreshes them).
* Activity / task entity ``talent_pool``, audit action, settings hub entry. Erasure: ``talent_pools_erasure``.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- rediscovery
def _register_rediscovery() -> None:
    from app.services.matching.rediscovery import register_rediscovery_section
    from app.services.talent_pools.rediscovery import pools_section

    register_rediscovery_section("pools", pools_section)


# ---------------------------------------------------------------- workflow
async def _snapshot(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID) -> dict[str, Any] | None:  # noqa: ARG001
    from app.models.talent_pools import TalentPool

    pool = await db.get(TalentPool, entity_id)
    if pool is None:
        return None
    return {
        "kind": pool.kind,
        "visibility": pool.visibility,
        "status": pool.status,
        "owner_id": str(pool.owner_id),
        "member_count": pool.member_count,
        "template_key": pool.template_key,
    }


def _register_workflow() -> None:
    from app.models.talent_pools import POOL_KINDS, POOL_STATUSES, POOL_VISIBILITIES
    from app.services.talent_pools import workflow_actions
    from app.services.workflow import catalogue, registry
    from app.services.workflow.conditions import FieldDef

    workflow_actions.register()
    if "talent_pool" not in catalogue.snapshot_loaders():
        catalogue.register_snapshot_loader(
            "talent_pool",
            _snapshot,
            {
                "kind": FieldDef("enum", "Pool kind", POOL_KINDS),
                "visibility": FieldDef("enum", "Visibility", POOL_VISIBILITIES),
                "status": FieldDef("enum", "Status", POOL_STATUSES),
                "owner_id": FieldDef("uuid", "Owner"),
                "member_count": FieldDef("int", "Members"),
                "template_key": FieldDef("str", "Template"),
            },
        )
    key = "talent_pools.membership_changed"
    if key not in registry.TRIGGERS:
        registry.register_trigger(
            key,
            registry.Trigger(
                key=key,
                label="Talent pool membership changed",
                event_module="app.events.talent_pools",
                event_class="PoolMembershipChanged",
                entity_type="talent_pool",
                entity_attr="pool_id",
                extras={
                    "added_count": ("added_count", FieldDef("int", "Members added")),
                    "removed_count": ("removed_count", FieldDef("int", "Members removed")),
                },
            ),
        )


# ---------------------------------------------------------------- notifications
async def _pool_name(db: AsyncSession, payload: Any) -> tuple[uuid.UUID | None, str]:
    from app.models.talent_pools import TalentPool

    pid = _uuid(payload.get("pool_id"))
    pool = await db.get(TalentPool, pid) if pid else None
    return pid, (pool.name if pool is not None else "Talent pool")


async def _owner(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # noqa: ARG001
    uid = _uuid(payload.get("owner_id"))
    return {uid} if uid else set()


async def _subscriber(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # noqa: ARG001
    uid = _uuid(payload.get("user_id"))
    return {uid} if uid else set()


async def _render_digest(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
    from app.services.notifications_service import Content

    pools = list(payload.get("pools") or [])
    total = sum(int(p.get("added") or 0) for p in pools)
    plural = "" if len(pools) == 1 else "s"
    lines = [f"{p.get('name')}: +{p.get('added')}" for p in pools[:20]]
    first = _uuid(pools[0].get("pool_id")) if len(pools) == 1 else None
    return Content(
        title=f"{total} new member{'s' if total != 1 else ''} in {len(pools)} talent pool{plural}"[:300],
        body="\n".join(lines),
        entity_type="talent_pool" if first else None,
        entity_id=first,
        link_path=f"/talent-pools/{first}" if first else "/talent-pools",
    )


async def _render_repair(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    pid, name = await _pool_name(db, payload)
    return Content(
        title=f"{name} needs repair: its segment uses a field that no longer exists"[:300],
        body="Edit the pool definition; refreshes are skipped until then.",
        entity_type="talent_pool" if pid else None,
        entity_id=pid,
        link_path=f"/talent-pools/{pid}" if pid else "/talent-pools",
    )


async def _render_failed(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    pid, name = await _pool_name(db, payload)
    reason = "it took too long" if payload.get("error_code") == "query_timeout" else "of an error"
    return Content(
        title=f"{name} could not refresh because {reason}"[:300],
        body="Narrow the segment or try again later.",
        entity_type="talent_pool" if pid else None,
        entity_id=pid,
        link_path=f"/talent-pools/{pid}" if pid else "/talent-pools",
    )


def _register_notifications() -> None:
    from app.services import notifications_service as ns

    defs = {
        "pool_new_members": ns.NotificationType(
            "New talent pool members (daily digest)",
            "Talent pools",
            "both",
            False,
            _subscriber,
            _render_digest,
            internal_only=True,
        ),
        "pool_needs_repair": ns.NotificationType(
            "Talent pool needs repair",
            "Talent pools",
            "in_app",
            False,
            _owner,
            _render_repair,
            internal_only=True,
        ),
        "pool_refresh_failed": ns.NotificationType(
            "Talent pool refresh failed",
            "Talent pools",
            "in_app",
            False,
            _owner,
            _render_failed,
            internal_only=True,
        ),
    }
    for key, definition in defs.items():
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, definition)


# ---------------------------------------------------------------- events
def _subscribe() -> None:
    from app.core.events import subscribe
    from app.events.skills import SkillGraphChanged
    from app.services.talent_pools.events import PoolRefreshRequested

    def on_refresh_requested(e: PoolRefreshRequested) -> None:
        from app.tasks import talent_pools as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.refresh, str(e.tenant_id), str(e.refresh_id))

    def on_graph_changed(e: SkillGraphChanged) -> None:
        from app.tasks import talent_pools as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.graph_changed, str(e.tenant_id), e.scope, list(e.skill_keys))

    on_refresh_requested.__name__ = "talent_pools_on_refresh_requested"
    on_graph_changed.__name__ = "talent_pools_on_skill_graph_changed"
    subscribe(PoolRefreshRequested, on_refresh_requested)
    subscribe(SkillGraphChanged, on_graph_changed)


# ---------------------------------------------------------------- access / audit / settings
async def _pool_access(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID, write: bool) -> bool:
    from app.core.errors import NotFound
    from app.services.talent_pools import common

    principal = ctx.principal
    if getattr(principal, "type", None) != "user" or not principal.has(common.READ):
        return False
    try:
        pool = await common.get_pool(ctx, db, entity_id)
    except NotFound:
        return False
    return common.can_edit(principal, pool) if write else True


def _register_access() -> None:
    from app.services import activity_service

    activity_service.register_entity("talent_pool", _pool_access, plural="talent-pools")


def _register_audit() -> None:
    from app.core import audit

    audit.register_audit_action("talent_pool.refreshed", "A dynamic talent pool was refreshed (counts only)")


def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting",
        "talent_pools",
        "Talent pools",
        "Nightly pool refresh time, daily digest time and the dynamic pool limit.",
        "/api/v1/talent-pools/settings",
        "pool:manage",
        owner_epic="Epic 38",
    )


for _fn in (
    _register_rediscovery,
    _register_workflow,
    _register_notifications,
    _subscribe,
    _register_access,
    _register_audit,
    _register_settings,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block start-up on an optional registry
        logger.exception("talent_pools: {} failed", _fn.__name__)
