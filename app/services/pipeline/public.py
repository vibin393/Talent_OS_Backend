"""Public secure-link pages of the pipeline (NFR-17): AI screening (candidate) and client feedback (contact).

GETs never change state (mail scanners prefetch links); only the one-shot POST consumes the link. Each
handler resolves the token itself (``core.secure_links.resolve`` opens the right tenant) and runs in that
tenant's session. View models carry no ids or contact data; AI screening pages never show the client.
"""

import uuid
from typing import Any

from app.core import audit, secure_links
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.models.pipeline import Application, Screening, Submission
from app.schemas.pipeline import PublicActionOut, PublicLinkView, PublicQuestion
from app.schemas.privacy import NoticeOut
from app.services.pipeline import feedback as feedback_service
from app.services.pipeline import screening as screening_service

AI_SCREENING = "ai_screening"
CLIENT_FEEDBACK = "client_feedback"
PURPOSES: frozenset[str] = frozenset({AI_SCREENING, CLIENT_FEEDBACK})


async def _tenant_name(db: Any, tenant_id: uuid.UUID) -> str | None:
    from app.services import identity_bridge

    tenant = await identity_bridge.get_tenant(db, tenant_id)
    return tenant.name if tenant is not None else None


async def view(link: secure_links.ResolvedLink) -> PublicLinkView:
    from app.services import privacy
    from app.services.candidates import pipeline_port as candidates
    from app.services.demands import pipeline_port as demands_port

    async with tenant_session(link.tenant_id) as db:
        if link.purpose == AI_SCREENING:
            row = await db.get(Screening, link.subject_id)
            if row is None:
                raise NotFound("Link not found", code="link_invalid")
            app = await db.get(Application, row.application_id)
            demand = await demands_port.get_row(db, app.demand_id) if app else None
            notice = await privacy.consent.active_notice(db, link.tenant_id, "ai_processing")
            return PublicLinkView(
                purpose=link.purpose,
                tenant_name=await _tenant_name(db, link.tenant_id),
                expires_at=link.expires_at,
                notice=NoticeOut.model_validate(notice) if notice is not None else None,
                status=row.status,
                job_title=demand.title if demand else None,
                consent_required=not await privacy.has_active_consent(db, row.candidate_id, "ai_processing"),
                questions=[
                    PublicQuestion(
                        key=q["key"],
                        label=q["label"],
                        type=q["type"],
                        options=q.get("options"),
                        required=bool(q.get("required")),
                    )
                    for q in row.questions
                ],
            )
        sub = await db.get(Submission, link.subject_id)
        if sub is None:
            raise NotFound("Link not found", code="link_invalid")
        demand = await demands_port.get_row(db, sub.demand_id)
        cand = (await candidates.summaries(db, [sub.candidate_id])).get(sub.candidate_id, {})
        decision = link.meta.get("decision")
        return PublicLinkView(
            purpose=link.purpose,
            tenant_name=await _tenant_name(db, link.tenant_id),
            expires_at=link.expires_at,
            status=sub.feedback_status,
            job_title=demand.title,
            candidate_first_name=cand.get("first_name"),
            decision=decision if decision in feedback_service.STATUS_FOR else None,
            decisions=["interested", "on_hold", "rejected"],
        )


async def consent(
    token: str, granted: bool, notice_id: uuid.UUID | None, *, ip_hash: str | None, user_agent: str | None
) -> PublicActionOut:
    link = await secure_links.resolve(token, (AI_SCREENING,))
    async with tenant_session(link.tenant_id) as db:
        row = await db.get(Screening, link.subject_id, with_for_update=True)
        if row is None:
            raise NotFound("Link not found", code="link_invalid")
        audit.set_actor(db, "candidate", row.candidate_id)
        status = await screening_service.public_consent(
            db, row, link.id, granted, notice_id, {"ip_hash": ip_hash, "user_agent": (user_agent or "")[:300]}
        )
    return PublicActionOut(status=status)


async def submit(token: str, answers: dict[str, Any]) -> PublicActionOut:
    link = await secure_links.resolve(token, (AI_SCREENING,))
    async with tenant_session(link.tenant_id) as db:
        row = await db.get(Screening, link.subject_id, with_for_update=True)
        if row is None:
            raise NotFound("Link not found", code="link_invalid")
        audit.set_actor(db, "candidate", row.candidate_id)
        status = await screening_service.public_submit(db, row, link.id, answers)
    return PublicActionOut(status=status)


async def feedback(token: str, decision: str, comment: str | None) -> PublicActionOut:
    """Contact confirms a decision: consume this link, revoke its two siblings, record the feedback."""
    link = await secure_links.resolve(token, (CLIENT_FEEDBACK,))
    async with tenant_session(link.tenant_id) as db:
        audit.set_actor(db, "system", None)
        await secure_links.consume(db, link.id)
        contact = link.meta.get("contact_id")
        for sibling in await secure_links.links_for_subject(db, "submission", link.subject_id):
            if sibling.id != link.id and (sibling.meta or {}).get("contact_id") == contact:
                await secure_links.revoke(db, sibling.id)
        ctx = RequestContext(tenant_id=link.tenant_id)
        await feedback_service.record_feedback(
            ctx,
            db,
            link.subject_id,
            decision,
            comment,
            source="link",
            contact_id=uuid.UUID(contact) if contact else None,
        )
    return PublicActionOut(status="recorded")
