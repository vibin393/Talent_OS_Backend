"""Platform operations (Epic 12 story 12.6, FR-70..FR-72): tenant overview + AI budget, sub-processors,
platform notices and incident notifications.

* ``is_accepting_applications(tenant_id)`` -- Epic 9 career applications call it (suspended/offboarding ->
  409 ``tenant_not_accepting``).
* Sub-processors: a new entry must be effective at least 30 days out (422 ``notice_period_too_short``) unless
  ``emergency`` with a reason (audited). Creating one posts a notice and emails the admins of every active
  tenant.
* Incidents: ``notify`` renders ``app/templates/incident_notice.md.j2``, posts an admin-audience notice per
  tenant and emails its admins; a notification is ``late`` when sent more than 24 h after confirmation.
"""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jinja2
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import ConflictError, NotFound, Unauthorized, ValidationFailed
from app.core.pagination import Page, PageParams, paginate
from app.models.identity import Tenant
from app.models.platform import (
    Incident,
    IncidentNotification,
    PlatformNotice,
    PlatformNoticeAck,
    SubProcessor,
)
from app.schemas.platform import (
    IncidentCreate,
    IncidentNotificationOut,
    IncidentOut,
    NoticeCreate,
    PlatformNoticeOut,
    SubProcessorIn,
    SubProcessorOut,
    SubProcessorUpdate,
    TenantLifecycleOut,
)
from app.services import ai, identity_bridge
from app.services.privacy.common import email_users

SUB_PROCESSOR_NOTICE = timedelta(days=30)
INCIDENT_DEADLINE = timedelta(hours=24)
TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
_jinja = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(TEMPLATES)),
    autoescape=jinja2.select_autoescape(),
    undefined=jinja2.ChainableUndefined,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _operator(ctx: RequestContext) -> uuid.UUID:
    if getattr(ctx.principal, "type", None) != "platform":
        raise Unauthorized()
    return ctx.principal.id  # type: ignore[no-any-return]


# ================================================================ tenants
async def is_accepting_applications(tenant_id: uuid.UUID) -> bool:
    async with platform_session() as pdb:
        return await identity_bridge.tenant_status(pdb, tenant_id) == "active"


async def tenant_detail(ctx: RequestContext, tenant_id: uuid.UUID) -> TenantLifecycleOut:
    _operator(ctx)
    from app.services import tenant_lifecycle_service

    async with platform_session() as pdb:
        tenant = await pdb.get(Tenant, tenant_id)
        if tenant is None:
            raise NotFound("Tenant not found")
        out = await tenant_lifecycle_service.lifecycle_out(pdb, tenant)
    if tenant.status != "deleted":
        async with tenant_session(tenant_id) as db:
            budget = await ai.monthly_budget(db)
            used = await ai.budget.usage(db, tenant_id, fresh=True)
        out.ai_budget = budget
        out.ai_usage_percent = round(used * 100.0 / budget, 1) if budget else None
    return out


async def set_ai_budget(ctx: RequestContext, tenant_id: uuid.UUID, budget: int | None) -> TenantLifecycleOut:
    operator_id = _operator(ctx)
    async with platform_session() as pdb:
        status = await identity_bridge.tenant_status(pdb, tenant_id)
    if status is None or status == "deleted":
        raise NotFound("Tenant not found")
    async with tenant_session(tenant_id) as db:
        audit.set_actor(db, "platform", operator_id)
        old = await ai.monthly_budget(db)
        await ai.set_monthly_budget(db, tenant_id, budget)
        audit.record("ai.budget_set", "tenant", tenant_id, session=db, old=old, new=budget)
    ai.budget.clear_cache()
    if budget is None or old is None or budget > old:
        from app.tasks.platform import enqueue_resume_budget_paused

        enqueue_resume_budget_paused(tenant_id)
    return await tenant_detail(ctx, tenant_id)


# ================================================================ notices
async def create_notice(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    audience: str,
    kind: str,
    severity: str,
    title: str,
    body_md: str,
    expires_at: datetime | None = None,
    created_by: uuid.UUID | None = None,
) -> PlatformNotice:
    notice = PlatformNotice(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        audience=audience,
        kind=kind,
        severity=severity,
        title=title,
        body_md=body_md,
        expires_at=expires_at,
        created_by=created_by,
    )
    db.add(notice)
    await db.flush()
    return notice


async def post_notice(ctx: RequestContext, data: NoticeCreate) -> PlatformNoticeOut:
    operator_id = _operator(ctx)
    async with platform_session() as pdb:
        notice = await create_notice(pdb, created_by=operator_id, **data.model_dump())
        audit.record("platform.notice_posted", "platform_notice", notice.id, session=pdb, kind=notice.kind)
        await pdb.refresh(notice)
        return PlatformNoticeOut.model_validate(notice)


async def list_all_notices(ctx: RequestContext) -> list[PlatformNoticeOut]:
    _operator(ctx)
    async with platform_session() as pdb:
        rows = (
            await pdb.execute(select(PlatformNotice).order_by(PlatformNotice.created_at.desc()).limit(500))
        ).scalars()
        return [PlatformNoticeOut.model_validate(r) for r in rows]


def _is_admin(principal: Any) -> bool:
    return bool(principal is not None and principal.has("user:manage"))


async def notices_for(ctx: RequestContext, db: AsyncSession) -> list[PlatformNoticeOut]:  # ruff: ignore[unused-function-argument]
    """Unexpired notices for the caller's tenant, filtered by audience (admins see admin notices)."""
    principal = ctx.principal
    tenant_id = ctx.tenant_id
    user_id = getattr(principal, "id", None)
    audiences = ["all_users", "admins"] if _is_admin(principal) else ["all_users"]
    async with platform_session() as pdb:
        rows = (
            await pdb.execute(
                select(PlatformNotice)
                .where(
                    or_(PlatformNotice.tenant_id.is_(None), PlatformNotice.tenant_id == tenant_id),
                    PlatformNotice.audience.in_(audiences),
                    or_(PlatformNotice.expires_at.is_(None), PlatformNotice.expires_at > _now()),
                )
                .order_by(PlatformNotice.created_at.desc())
                .limit(200)
            )
        ).scalars()
        rows = list(rows)  # type: ignore[assignment]
        acked = set(
            (
                await pdb.execute(
                    select(PlatformNoticeAck.notice_id).where(
                        PlatformNoticeAck.user_id == user_id,
                        PlatformNoticeAck.notice_id.in_([r.id for r in rows]),
                    )
                )
            ).scalars()
        )
    out = []
    for r in rows:
        item = PlatformNoticeOut.model_validate(r)
        item.acknowledged = r.id in acked
        out.append(item)
    return out


async def ack_notice(ctx: RequestContext, db: AsyncSession, notice_id: uuid.UUID) -> None:
    visible = {n.id for n in await notices_for(ctx, db)}
    if notice_id not in visible:
        raise NotFound("Notice not found")
    user_id = getattr(ctx.principal, "id", None)
    assert ctx.tenant_id is not None and user_id is not None
    async with platform_session() as pdb:
        if await pdb.get(PlatformNoticeAck, (notice_id, user_id)) is None:
            pdb.add(PlatformNoticeAck(notice_id=notice_id, user_id=user_id, tenant_id=ctx.tenant_id))
    audit.record("platform.notice_acknowledged", "platform_notice", notice_id, session=db)


# ================================================================ sub-processors
async def list_sub_processors(*, include_planned: bool = True) -> list[SubProcessorOut]:
    async with platform_session() as pdb:
        stmt = select(SubProcessor).where(SubProcessor.status != "retired")
        if not include_planned:
            stmt = stmt.where(SubProcessor.status == "active")
        rows = (await pdb.execute(stmt.order_by(SubProcessor.name))).scalars()
        return [SubProcessorOut.model_validate(r) for r in rows]


async def list_all_sub_processors(ctx: RequestContext) -> list[SubProcessorOut]:
    _operator(ctx)
    async with platform_session() as pdb:
        rows = (await pdb.execute(select(SubProcessor).order_by(SubProcessor.name))).scalars()
        return [SubProcessorOut.model_validate(r) for r in rows]


async def create_sub_processor(ctx: RequestContext, data: SubProcessorIn) -> SubProcessorOut:
    operator_id = _operator(ctx)
    now = _now()
    if data.effective_from < now + SUB_PROCESSOR_NOTICE:
        if not data.emergency:
            raise ValidationFailed(
                "Sub-processors must be announced 30 days before they take effect",
                code="notice_period_too_short",
                details={
                    "fields": {"effective_from": "At least 30 days from now (or use the emergency override)"}
                },
            )
        if not data.emergency_reason or len(data.emergency_reason.strip()) < 10:
            raise ValidationFailed(
                "An emergency override needs a reason",
                details={"fields": {"emergency_reason": "At least 10 characters"}},
            )
    async with platform_session() as pdb:
        row = SubProcessor(
            id=uuid.uuid4(),
            name=data.name,
            purpose=data.purpose,
            data_categories=data.data_categories,
            processing_location=data.processing_location,
            country=data.country,
            website=data.website,
            status="planned",
            effective_from=data.effective_from,
            notice_sent_at=now,
            emergency_reason=data.emergency_reason if data.emergency else None,
        )
        pdb.add(row)
        await pdb.flush()
        await create_notice(
            pdb,
            tenant_id=None,
            audience="admins",
            kind="sub_processor",
            severity="info",
            title=f"New sub-processor: {data.name}",
            body_md=(
                f"**{data.name}** ({data.country}) will process {', '.join(data.data_categories) or 'data'} for: "
                f"{data.purpose}. Effective from {data.effective_from:%Y-%m-%d}."
            ),
            created_by=operator_id,
        )
        audit.record(
            "platform.sub_processor_added",
            "sub_processor",
            row.id,
            session=pdb,
            emergency=data.emergency,
            emergency_reason=data.emergency_reason if data.emergency else None,
        )
        await pdb.refresh(row)
        out = SubProcessorOut.model_validate(row)
    from app.tasks.platform import enqueue_sub_processor_notice

    enqueue_sub_processor_notice(row.id)
    return out


async def update_sub_processor(
    ctx: RequestContext, sub_id: uuid.UUID, data: SubProcessorUpdate
) -> SubProcessorOut:
    _operator(ctx)
    async with platform_session() as pdb:
        row = await pdb.get(SubProcessor, sub_id)
        if row is None:
            raise NotFound("Sub-processor not found")
        changes = data.model_dump(exclude_unset=True)
        for key, value in changes.items():
            setattr(row, key, value)
        await pdb.flush()
        audit.record(
            "platform.sub_processor_updated", "sub_processor", row.id, session=pdb, fields=sorted(changes)
        )
        await pdb.refresh(row)
        return SubProcessorOut.model_validate(row)


# ================================================================ incidents
async def create_incident(ctx: RequestContext, data: IncidentCreate) -> IncidentOut:
    operator_id = _operator(ctx)
    async with platform_session() as pdb:
        row = Incident(id=uuid.uuid4(), created_by=operator_id, status="draft", **data.model_dump())
        pdb.add(row)
        await pdb.flush()
        audit.record("platform.incident_created", "incident", row.id, session=pdb, severity=row.severity)
        await pdb.refresh(row)
        return IncidentOut.model_validate(row)


async def list_incidents(ctx: RequestContext, params: PageParams) -> Page[IncidentOut]:
    _operator(ctx)
    async with platform_session() as pdb:
        page = await paginate(
            pdb, select(Incident).order_by(Incident.confirmed_at.desc(), Incident.id), params
        )
        return Page[IncidentOut](
            items=[IncidentOut.model_validate(i) for i in page.items],
            total=page.total,
            page=page.page,
            page_size=page.page_size,
        )


def render_incident(incident: Incident, tenant_name: str) -> str:
    return _jinja.get_template("incident_notice.md.j2").render(
        incident=incident,
        tenant_name=tenant_name,
        confirmed_at=f"{incident.confirmed_at:%Y-%m-%d %H:%M} UTC",
        details=incident.details or {},
    )


async def notify_incident(
    ctx: RequestContext, incident_id: uuid.UUID, tenant_ids: list[uuid.UUID]
) -> list[IncidentNotificationOut]:
    operator_id = _operator(ctx)
    now = _now()
    out: list[IncidentNotificationOut] = []
    async with platform_session() as pdb:
        incident = await pdb.get(Incident, incident_id)
        if incident is None:
            raise NotFound("Incident not found")
        if incident.status == "closed":
            raise ConflictError("The incident is closed", code="incident_closed")
        tenants = [await pdb.get(Tenant, tid) for tid in dict.fromkeys(tenant_ids)]
        if any(t is None for t in tenants):
            raise NotFound("Tenant not found")
    for tenant in tenants:
        assert tenant is not None
        body = render_incident(incident, tenant.name)
        subject = f"Personal data breach notification: {incident.title}"
        async with tenant_session(tenant.id) as db:
            admins = await identity_bridge.tenant_admins(db)
            audit.record(
                "platform.incident_notified",
                "incident",
                incident.id,
                session=db,
                actor_type="platform",
                actor_id=operator_id,
            )
        async with platform_session() as pdb:
            existing = (
                await pdb.execute(
                    select(IncidentNotification).where(
                        IncidentNotification.incident_id == incident.id,
                        IncidentNotification.tenant_id == tenant.id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                out.append(IncidentNotificationOut.model_validate(existing))
                continue
            notice = await create_notice(
                pdb,
                tenant_id=tenant.id,
                audience="admins",
                kind="incident",
                severity=incident.severity,
                title=subject,
                body_md=body,
                created_by=operator_id,
            )
            row = IncidentNotification(
                id=uuid.uuid4(),
                incident_id=incident.id,
                tenant_id=tenant.id,
                notice_id=notice.id,
                subject=subject,
                body_md=body,
                sent_at=now,
                sent_by=operator_id,
                recipient_count=len(admins),
                late=now > incident.confirmed_at + INCIDENT_DEADLINE,
            )
            pdb.add(row)
            inc = await pdb.get(Incident, incident.id)
            if inc is not None and inc.status == "draft":
                inc.status = "notified"
            await pdb.flush()
            out.append(IncidentNotificationOut.model_validate(row))
        from app.tasks.platform import enqueue_incident_emails

        enqueue_incident_emails(tenant.id, [a["email"] for a in admins if a.get("email")], subject, body)
    return out


async def incident_notifications(
    ctx: RequestContext, incident_id: uuid.UUID
) -> list[IncidentNotificationOut]:
    _operator(ctx)
    async with platform_session() as pdb:
        rows = list(
            (
                await pdb.execute(
                    select(IncidentNotification).where(IncidentNotification.incident_id == incident_id)
                )
            ).scalars()
        )
        counts = dict(
            (
                await pdb.execute(
                    select(PlatformNoticeAck.notice_id, func.count())
                    .where(PlatformNoticeAck.notice_id.in_([r.notice_id for r in rows if r.notice_id]))
                    .group_by(PlatformNoticeAck.notice_id)
                )
            )
            .tuples()
            .all()
        )
    out = []
    for r in rows:
        item = IncidentNotificationOut.model_validate(r)
        item.acknowledged_count = int(counts.get(r.notice_id, 0))  # type: ignore[arg-type]
        out.append(item)
    return out


# ================================================================ tenant list (operator console)
async def list_tenants(ctx: RequestContext, params: PageParams) -> Page[TenantLifecycleOut]:
    _operator(ctx)
    from app.services import tenant_lifecycle_service

    async with platform_session() as pdb:
        page = await paginate(pdb, select(Tenant).order_by(Tenant.created_at.desc(), Tenant.id), params)
        items = [await tenant_lifecycle_service.lifecycle_out(pdb, t) for t in page.items]
    return Page[TenantLifecycleOut](items=items, total=page.total, page=page.page, page_size=page.page_size)


async def email_sub_processor_notice(tenant_id: uuid.UUID, sub_id: uuid.UUID) -> int:
    """Email one tenant's admins about a new sub-processor (fanned out per active tenant by a task)."""
    async with platform_session() as pdb:
        sub = await pdb.get(SubProcessor, sub_id)
    if sub is None:
        return 0
    async with tenant_session(tenant_id) as db:
        admins = await identity_bridge.tenant_admins(db)
    return await email_users(
        admins,
        f"Notice: new sub-processor {sub.name}",
        f"{sub.name} ({sub.processing_location}, {sub.country}) will act as a sub-processor from "
        f"{sub.effective_from:%Y-%m-%d} for: {sub.purpose}.\nData categories: {', '.join(sub.data_categories)}.\n",
        tenant_id=tenant_id,
        kind="sub_processor_notice",
    )


# ================================================================ Epic 33 (FR-174, AD-38): custom Career Portal domains
from app.services.custom_domains import (  # ruff: ignore[module-import-not-at-top-of-file, unused-import] - re-exported
    add_custom_domain,  # noqa: F401
    check_domains,  # noqa: F401
    get_custom_domain,  # noqa: F401
    remove_custom_domain,  # noqa: F401
    slug_for_host,  # noqa: F401
    tenant_for_host,  # noqa: F401
    verify_custom_domain,  # noqa: F401
)
