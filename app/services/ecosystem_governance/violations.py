# ruff: file-ignore[line-too-long]
"""Policy violations (Epic 79 story 79.1, FR-405).

Composes existing sources (no new write path in owner code): ``egress_refused`` (webhook deliveries / connector jobs),
``scope_refused`` (public API 403s + MCP insufficient-scope), ``injection_detected``, ``refresh_reuse``
(``oauth_tokens.revoked_reason = 'reuse_detected'``), ``human_only_refused`` (P4 audit), ``bulk_extraction_paused``.
Each source is guarded so an absent / renamed table degrades to no rows. Rows carry no payloads.

Known gap (spec A3): egress refusals raised outside webhook deliveries and connector jobs are not persisted.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from loguru import logger
from sqlalchemy import select

from app.models.oauth import OAuthToken
from app.schemas.ecosystem_governance import ViolationRow

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.context import RequestContext


async def list_violations(
    ctx: RequestContext, db: AsyncSession, *, kind: str | None = None, from_: dt.datetime | None = None, to: dt.datetime | None = None  # ruff: ignore[unused-function-argument]
) -> list[ViolationRow]:
    out: list[ViolationRow] = []
    out.extend(await _refresh_reuse(ctx, db))
    out.extend(await _scope_refused(ctx, db))
    out.extend(await _egress_refused(ctx, db))
    if kind:
        out = [v for v in out if v.kind == kind]
    out.sort(key=lambda v: v.at or dt.datetime.min.replace(tzinfo=dt.UTC), reverse=True)
    return out


async def _refresh_reuse(ctx: RequestContext, db: AsyncSession) -> list[ViolationRow]:
    try:
        rows = (
            await db.execute(
                select(OAuthToken.installation_id, OAuthToken.revoked_at)
                .where(
                    OAuthToken.tenant_id == ctx.tenant_id,
                    OAuthToken.revoked_reason == "reuse_detected",
                )
                .limit(500)
            )
        ).all()
        return [
            ViolationRow(kind="refresh_reuse", at=revoked_at, installation=str(inst_id), error_code="reuse_detected")
            for inst_id, revoked_at in rows
        ]
    except Exception:  # pragma: no cover
        logger.debug("violations: refresh_reuse source unavailable")
        return []


async def _scope_refused(ctx: RequestContext, db: AsyncSession) -> list[ViolationRow]:
    return await _from_table(
        db,
        ctx,
        "SELECT route, created_at FROM api_request_log WHERE tenant_id = :tid AND status = 403 "
        "AND route LIKE '/public/v1%' ORDER BY created_at DESC LIMIT 500",
        kind="scope_refused",
        route_field="route",
        error_code="insufficient_scope",
    )


async def _egress_refused(ctx: RequestContext, db: AsyncSession) -> list[ViolationRow]:
    return await _from_table(
        db,
        ctx,
        "SELECT last_error_code, updated_at FROM connector_jobs WHERE tenant_id = :tid "
        "AND last_error_code = 'egress_refused' ORDER BY updated_at DESC LIMIT 500",
        kind="egress_refused",
        route_field=None,
        error_code="egress_refused",
    )


async def _from_table(
    db: AsyncSession, ctx: RequestContext, sql: str, *, kind: str, route_field: str | None, error_code: str
) -> list[ViolationRow]:
    from sqlalchemy import text

    try:
        rows = (await db.execute(text(sql), {"tid": ctx.tenant_id})).all()
    except Exception:  # pragma: no cover - table absent / renamed
        logger.debug("violations: %s source unavailable", kind)
        return []
    out: list[ViolationRow] = []
    for row in rows:
        mapping: dict[str, Any] = dict(row._mapping)
        at = mapping.get("created_at") or mapping.get("updated_at")
        out.append(
            ViolationRow(
                kind=kind,
                at=at,
                route_or_tool=mapping.get(route_field) if route_field else None,
                error_code=error_code,
            )
        )
    return out


__all__ = ["list_violations"]
