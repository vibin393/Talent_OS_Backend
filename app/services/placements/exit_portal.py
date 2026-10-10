# ruff: noqa: E501
"""Client Portal and Secure Link side of Epic 31 (reconciliation decision 3: these endpoints are owned here; Epic 18's
hub links to them through ``client_extension_summary`` / ``client_offboarding_summary``).

Access is checked in the caller's org-scoped session (``client_id`` org RLS + ``visible_placement_ids`` for
restricted Client Users; another client's id is 404). The work then runs in an internal tenant session with the
Client ``ActorRef`` (``client_user`` or ``secure_link``). Schemas carry no rates or margin (frozen Never).
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.actors import ActorRef
from app.core.client_scope import visible_placement_ids
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.placements import Placement
from app.models.placements_exit import CASE_OPEN, Extension, OffboardingCase
from app.schemas.placements_exit_portal import (
    ClientExtensionSummary,
    ClientOffboardingSummary,
    ExtensionRespondIn,
    OffboardingConfirmIn,
    PublicActionResult,
    PublicExtensionView,
    PublicOffboardingView,
)
from app.services.placements import extensions, offboarding
from app.services.placements.common import names


async def _scope(ctx: RequestContext, cdb: AsyncSession) -> frozenset[uuid.UUID] | None:
    return await visible_placement_ids(ctx, cdb)


async def _meta(
    db: AsyncSession, placement_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[Placement, str | None]]:
    rows = (
        list((await db.execute(select(Placement).where(Placement.id.in_(placement_ids)))).scalars())
        if placement_ids
        else []
    )
    cands, _ = await names(db, candidate_ids={p.candidate_id for p in rows})
    return {p.id: (p, cands.get(p.candidate_id)) for p in rows}


async def client_extension_summary(ctx: RequestContext, cdb: AsyncSession) -> list[ClientExtensionSummary]:
    client_id = getattr(ctx.principal, "org_id", None)
    rows = list(
        (
            await cdb.execute(
                select(Extension)
                .where(
                    Extension.client_id == client_id,
                    Extension.status.in_(("pending_client_confirmation", "confirmed", "declined")),
                )
                .order_by(Extension.created_at.desc())
                .limit(100)
            )
        ).scalars()
    )
    scope = await _scope(ctx, cdb)
    rows = [r for r in rows if scope is None or r.placement_id in scope]
    meta = await _meta(cdb, {r.placement_id for r in rows})
    out = []
    for r in rows:
        p, worker = meta.get(r.placement_id, (None, None))
        if p is None:
            continue
        out.append(
            ClientExtensionSummary(
                id=r.id,
                human_id=r.human_id,
                placement_id=r.placement_id,
                placement_human_id=p.human_id,
                worker_name=worker,
                role_title=p.role_title,
                current_end_date=r.current_end_date,
                new_end_date=r.new_end_date,
                status=r.status,
                client_response_at=r.client_response_at,
                client_comment=r.client_comment,
            )
        )
    return out


async def client_offboarding_summary(
    ctx: RequestContext, cdb: AsyncSession
) -> list[ClientOffboardingSummary]:
    client_id = getattr(ctx.principal, "org_id", None)
    rows = list(
        (
            await cdb.execute(
                select(OffboardingCase)
                .where(OffboardingCase.client_id == client_id, OffboardingCase.status.in_(CASE_OPEN))
                .order_by(OffboardingCase.last_working_day)
            )
        ).scalars()
    )
    scope = await _scope(ctx, cdb)
    rows = [r for r in rows if scope is None or r.placement_id in scope]
    meta = await _meta(cdb, {r.placement_id for r in rows})
    out = []
    for r in rows:
        p, worker = meta.get(r.placement_id, (None, None))
        if p is None:
            continue
        out.append(
            ClientOffboardingSummary(
                id=r.id,
                human_id=r.human_id,
                placement_id=r.placement_id,
                placement_human_id=p.human_id,
                worker_name=worker,
                role_title=p.role_title,
                reason=r.reason,
                last_working_day=r.last_working_day,
                status=r.status,
                client_confirmed_at=r.client_confirmed_at,
            )
        )
    return out


async def _visible_placement(ctx: RequestContext, cdb: AsyncSession, placement_id: uuid.UUID) -> None:
    client_id = getattr(ctx.principal, "org_id", None)
    found = (
        await cdb.execute(
            select(Placement.id).where(Placement.id == placement_id, Placement.client_id == client_id)
        )
    ).scalar_one_or_none()
    scope = await _scope(ctx, cdb)
    if found is None or (scope is not None and placement_id not in scope):
        raise NotFound("Placement not found")


def _internal_ctx(ctx: RequestContext, tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, principal=ctx.principal, actor_kind="system")


async def respond_extension(
    ctx: RequestContext,
    cdb: AsyncSession,
    tenant_id: uuid.UUID,
    placement_id: uuid.UUID,
    extension_id: uuid.UUID,
    data: ExtensionRespondIn,
) -> ClientExtensionSummary:
    await _visible_placement(ctx, cdb, placement_id)
    ext = (
        await cdb.execute(
            select(Extension).where(Extension.id == extension_id, Extension.placement_id == placement_id)
        )
    ).scalar_one_or_none()
    if ext is None:
        raise NotFound("Extension not found")
    actor = ActorRef("client_user", getattr(ctx.principal, "id", None))
    async with tenant_session(tenant_id) as db:
        ictx = _internal_ctx(ctx, tenant_id)
        if data.decision == "confirm":
            await extensions.confirm(ictx, db, extension_id, actor, comment=data.comment)
        else:
            await extensions.decline(ictx, db, extension_id, actor, comment=data.comment)
    await cdb.refresh(ext)
    summaries = {s.id: s for s in await client_extension_summary(ctx, cdb)}
    return summaries[extension_id]


async def confirm_offboarding(
    ctx: RequestContext,
    cdb: AsyncSession,
    tenant_id: uuid.UUID,
    placement_id: uuid.UUID,
    data: OffboardingConfirmIn,
) -> ClientOffboardingSummary:
    if not data.last_working_day_ack:
        raise ValidationFailed("Acknowledge the last working day", code="ack_required")
    await _visible_placement(ctx, cdb, placement_id)
    case = (
        await cdb.execute(
            select(OffboardingCase).where(
                OffboardingCase.placement_id == placement_id, OffboardingCase.status.in_(CASE_OPEN)
            )
        )
    ).scalar_one_or_none()
    if case is None:
        raise NotFound("Offboarding case not found")
    actor = ActorRef("client_user", getattr(ctx.principal, "id", None))
    async with tenant_session(tenant_id) as db:
        await offboarding.client_confirm(_internal_ctx(ctx, tenant_id), db, case.id, actor, data.comment)
    summaries = {s.id: s for s in await client_offboarding_summary(ctx, cdb)}
    if case.id not in summaries:
        raise ConflictError("The offboarding case is not open", code="case_not_open")
    return summaries[case.id]


# ---------------------------------------------------------------- Secure Links (GET never mutates)
async def _tenant_name(db: AsyncSession, tenant_id: uuid.UUID) -> str | None:
    from app.services import identity_bridge

    tenant = await identity_bridge.get_tenant(db, tenant_id)
    return tenant.name if tenant is not None else None


async def extension_view(token: str) -> PublicExtensionView:
    link = await secure_links.resolve(token, (extensions.LINK_PURPOSE,))
    async with tenant_session(link.tenant_id) as db:
        ext = await extensions.get_row(db, link.subject_id)
        meta = await _meta(db, {ext.placement_id})
        p, worker = meta[ext.placement_id]
        return PublicExtensionView(
            tenant_name=await _tenant_name(db, link.tenant_id),
            worker_name=worker,
            role_title=p.role_title,
            current_end_date=ext.current_end_date,
            new_end_date=ext.new_end_date,
            status=ext.status,
            expires_at=link.expires_at,
        )


async def extension_respond(token: str, data: ExtensionRespondIn) -> PublicActionResult:
    link = await secure_links.resolve(token, (extensions.LINK_PURPOSE,))
    actor = ActorRef("secure_link", link.id)
    async with tenant_session(link.tenant_id) as db:
        await secure_links.consume(db, link.id)
        ctx = RequestContext(tenant_id=link.tenant_id, actor_kind="system")
        if data.decision == "confirm":
            ext = await extensions.confirm(ctx, db, link.subject_id, actor, comment=data.comment)
        else:
            ext = await extensions.decline(ctx, db, link.subject_id, actor, comment=data.comment)
        return PublicActionResult(status=ext.status)


async def offboarding_view(token: str) -> PublicOffboardingView:
    link = await secure_links.resolve(token, (offboarding.LINK_PURPOSE,))
    async with tenant_session(link.tenant_id) as db:
        case = await offboarding.get_case(db, link.subject_id)
        meta = await _meta(db, {case.placement_id})
        p, worker = meta[case.placement_id]
        return PublicOffboardingView(
            tenant_name=await _tenant_name(db, link.tenant_id),
            worker_name=worker,
            role_title=p.role_title,
            last_working_day=case.last_working_day,
            status=case.status,
            client_confirmed=case.client_confirmed_at is not None,
            expires_at=link.expires_at,
        )


async def offboarding_respond(token: str, data: OffboardingConfirmIn) -> PublicActionResult:
    if not data.last_working_day_ack:
        raise ValidationFailed("Acknowledge the last working day", code="ack_required")
    link = await secure_links.resolve(token, (offboarding.LINK_PURPOSE,))
    async with tenant_session(link.tenant_id) as db:
        await secure_links.consume(db, link.id)
        ctx = RequestContext(tenant_id=link.tenant_id, actor_kind="system")
        case = await offboarding.client_confirm(
            ctx, db, link.subject_id, ActorRef("secure_link", link.id), data.comment
        )
        return PublicActionResult(status=case.status)


def summary_payload(rows: list[Any]) -> list[dict[str, Any]]:
    return [r.model_dump(mode="json") for r in rows]
