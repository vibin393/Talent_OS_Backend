# ruff: file-ignore[line-too-long]
"""Alternative selection process requests (Epic 49 A8, FR-249).

A Candidate asks for an alternative to the AEDT for an LL144-flagged Demand, from the apply success step (apply
token) or the Candidate Portal (own application only). One request per Application; it creates a P1 Task for the
Demand owner ("Alternative selection process requested", notified through ``TaskAssigned``) and the score suppressor
then hides AEDT outputs for that (candidate, demand) pair. Recruiters see the request on the Application -- never the
self-ID.

Task source: the P1 ``tasks.source`` CHECK has no Phase 3 value and this epic alters no P1 table, so the row is
written with ``source='workflow'`` and ``created_by`` NULL (system task).
"""

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import Gone, NotFound
from app.core.events import emit
from app.events.tasks import TaskAssigned
from app.models.demands import Demand
from app.models.fairness import AedtAlternativeRequest, AedtJurisdictionFlag
from app.models.pipeline import Application
from app.models.portal import ApplySession, ApplySessionLookup
from app.models.tasks import Task
from app.schemas.fairness import AlternativeRequestIn, AlternativeRequestOut, PublicAlternativeRequestIn
from app.services.fairness import settings as settings_mod
from app.services.fairness.common import conflict, now, tenant_of
from app.services.fairness.events import AlternativeRequested

TASK_TITLE = "Alternative selection process requested"


async def _offered(db: AsyncSession, demand_id: uuid.UUID) -> bool:
    s = await settings_mod.current(db)
    return bool(s.ll144_enabled and await db.get(AedtJurisdictionFlag, demand_id) is not None)


async def _create(
    db: AsyncSession, app: Application, channel: str, note: str | None
) -> AedtAlternativeRequest:
    if not await _offered(db, app.demand_id):
        raise NotFound("An alternative process is not offered for this job", code="alternative_not_offered")
    existing = (
        await db.execute(
            select(AedtAlternativeRequest).where(AedtAlternativeRequest.application_id == app.id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    tenant_id = tenant_of(db)
    owner_id = (await db.execute(select(Demand.owner_id).where(Demand.id == app.demand_id))).scalar_one()
    task = Task(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        title=TASK_TITLE,
        notes="The candidate asked for an alternative selection process or accommodation instead of the automated tools (NYC LL144). AEDT scores are hidden for this application.",
        owner_id=owner_id,
        created_by=None,
        entity_type="application",
        entity_id=app.id,
        due_at=None,
        due_all_day=False,
        priority="high",
        status="open",
        source="workflow",
    )
    db.add(task)
    await db.flush()
    req = AedtAlternativeRequest(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        application_id=app.id,
        candidate_id=app.candidate_id,
        demand_id=app.demand_id,
        channel=channel,
        note=(note or "").strip() or None,
        task_id=task.id,
        requested_at=now(),
        status="open",
    )
    db.add(req)
    await db.flush()
    emit(
        db,
        TaskAssigned(
            tenant_id=tenant_id,
            task_id=task.id,
            owner_id=owner_id,
            actor_user_id=None,
            entity_type="application",
            entity_id=app.id,
        ),
    )
    emit(db, AlternativeRequested(tenant_id=tenant_id, request_id=req.id, demand_id=app.demand_id))
    audit.record("fairness.alternative_requested", "application", app.id, session=db, channel=channel)
    return req


async def from_apply(slug: str, data: PublicAlternativeRequestIn) -> None:
    from app.services.portal.careers import resolve_site

    site = await resolve_site(slug)
    digest = hashlib.sha256(data.apply_token.encode("utf-8")).hexdigest()
    async with tenant_session(site.tenant_id) as db:
        audit.set_actor(db, "system", None)
        lookup = await db.get(ApplySessionLookup, digest)
        if lookup is None or lookup.tenant_id != site.tenant_id:
            raise NotFound("Apply session not found", code="apply_session_not_found")
        row = await db.get(ApplySession, lookup.session_id)
        if row is None or row.token_hash != digest or row.submitted_at is None or row.application_id is None:
            raise NotFound("Apply session not found", code="apply_session_not_found")
        if row.expires_at <= now():
            raise Gone("This application session has expired", code="apply_session_expired")
        app = await db.get(Application, row.application_id)
        if app is None:
            raise NotFound("Apply session not found", code="apply_session_not_found")
        await _create(db, app, "apply", data.note)


def _out(r: AedtAlternativeRequest) -> AlternativeRequestOut:
    return AlternativeRequestOut.model_validate(r)


async def from_portal(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, data: AlternativeRequestIn
) -> AlternativeRequestOut:
    """Candidate Portal: own application only (another Candidate's application -> 404)."""
    async with tenant_session(tenant_id) as db:
        app = await db.get(Application, data.application_id)
        if app is None or app.candidate_id != candidate_id:
            raise NotFound("Application not found")
        audit.set_actor(db, "candidate", candidate_id)
        return _out(await _create(db, app, "portal", data.note))


async def list_for_candidate(db: AsyncSession, candidate_id: uuid.UUID) -> list[AlternativeRequestOut]:
    rows = (
        await db.execute(
            select(AedtAlternativeRequest)
            .where(AedtAlternativeRequest.candidate_id == candidate_id)
            .order_by(AedtAlternativeRequest.requested_at.desc())
        )
    ).scalars()
    return [_out(r) for r in rows]


async def for_application(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> AlternativeRequestOut | None:
    from app.services.pipeline.common import READ, application_for

    await application_for(ctx, db, application_id, READ)  # 404 outside the caller's application read scope
    row = (
        await db.execute(
            select(AedtAlternativeRequest).where(AedtAlternativeRequest.application_id == application_id)
        )
    ).scalar_one_or_none()
    return _out(row) if row is not None else None


async def mark_handled(
    ctx: RequestContext, db: AsyncSession, application_id: uuid.UUID
) -> AlternativeRequestOut:
    from app.services.pipeline.common import READ, application_for

    await application_for(ctx, db, application_id, READ)
    row = (
        await db.execute(
            select(AedtAlternativeRequest).where(AedtAlternativeRequest.application_id == application_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise conflict("alternative_not_requested", "No alternative process was requested")
    row.status = "handled"
    await db.flush()
    return _out(row)
