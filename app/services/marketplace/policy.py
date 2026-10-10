# ruff: noqa: E501
"""Tenant app policy checks (Epic 69, spec A4 step 2 / A5). Storage is the foundation's ``ecosystem_settings.app_policy``
(written through ``ecosystem_service.set_app_policy``); a policy change never widens or silently removes a grant --
``describe`` lists the Installations the current policy would not allow (shown, never removed)."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.database import platform_session
from app.core.errors import ConflictError
from app.models.ecosystem import DEFAULT_APP_POLICY
from app.models.oauth import App, AppInstallation
from app.services import ecosystem_service

PUBLISHER_NOT_ALLOWED = "Not allowed by your app policy"


async def current(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    row = await ecosystem_service.settings_row(db, tenant_id)
    return {**DEFAULT_APP_POLICY, **(row.app_policy or {})}


def allows(policy: dict[str, Any], publisher_kind: str) -> bool:
    """Whether the policy allows installing a listing of this publisher kind."""
    try:
        check_install(policy, publisher_kind)
    except ConflictError:
        return False
    return True


def _kinds(policy: dict[str, Any]) -> set[str]:
    return set(policy.get("publisher_kinds") or [])


def check_install(policy: dict[str, Any], publisher_kind: str) -> None:
    if publisher_kind == "tenant":
        if not policy.get("private_apps", True):
            raise ConflictError(
                "Private apps are turned off by your app policy", code="private_apps_disabled"
            )
        return
    if publisher_kind not in _kinds(policy):
        raise ConflictError(PUBLISHER_NOT_ALLOWED, code="publisher_not_allowed")


async def affected_installations(db: AsyncSession, policy: dict[str, Any]) -> list[AppInstallation]:
    """Live App Installations the policy would no longer allow (connectors are platform-published: never affected)."""
    rows = list(
        (
            await db.execute(
                select(AppInstallation).where(
                    AppInstallation.kind == "app", AppInstallation.status != "uninstalled"
                )
            )
        ).scalars()
    )
    if not rows:
        return []
    async with platform_session() as pdb:
        kinds = dict(
            (
                await pdb.execute(
                    select(App.id, App.publisher_kind).where(App.id.in_([r.app_id for r in rows]))
                )
            )
            .tuples()
            .all()
        )
    out: list[AppInstallation] = []
    for r in rows:
        kind = kinds.get(r.app_id, "partner")  # type: ignore[arg-type]
        try:
            check_install(policy, kind)
        except ConflictError:
            out.append(r)
    return out


async def describe(ctx: RequestContext, db: AsyncSession) -> dict[str, Any]:
    assert ctx.tenant_id is not None
    policy = await current(db, ctx.tenant_id)
    return {"policy": policy, "affected": await affected_installations(db, policy)}


async def policy_view(ctx: RequestContext, db: AsyncSession) -> Any:
    """``GET /marketplace/policy``: the policy plus ``affected_installations`` (writes use ``PUT /ecosystem/app-policy``)."""
    from app.schemas.marketplace import PolicyOut
    from app.services.marketplace.installs import detail

    described = await describe(ctx, db)
    p = described["policy"]
    return PolicyOut(
        publisher_kinds=list(p.get("publisher_kinds") or []), private_apps=bool(p.get("private_apps", True)),
        user_self_authorize=bool(p.get("user_self_authorize", True)),
        sensitive_approval_chain_id=p.get("sensitive_approval_chain_id"),
        affected_installations=[await detail(ctx, db, i) for i in described["affected"]],
    )  # fmt: skip


__all__ = [
    "PUBLISHER_NOT_ALLOWED",
    "affected_installations",
    "allows",
    "check_install",
    "current",
    "describe",
    "policy_view",
]
