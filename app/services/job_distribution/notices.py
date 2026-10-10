# ruff: noqa: E501, D401
"""Applicant notice email and the daily applicant digest (Epic 70, spec A4 steps 7 / 9)."""

import uuid
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import func, select

from app.core import audit
from app.core.database import tenant_session
from app.models.demands import Demand
from app.models.job_distribution import BoardApplicantIngest, BoardPosting
from app.services.job_distribution.common import board_label, board_settings, now

NOTICE_TITLE = "Your application from a job board"
NOTICE_BODY = (
    "You applied to {tenant} for {job} through {board}. {tenant} now holds your application and CV to consider you "
    "for this and similar roles. You can see, correct or delete your data at {privacy_link}."
)


async def send_notice(tenant_id: uuid.UUID, application_id: uuid.UUID, board_key: str) -> str:
    from app.core.mailer import EmailMessage, send_email
    from app.core.settings import get_settings
    from app.services.candidates import portal_port as candidates
    from app.services.job_distribution.mapping import tenant_slug_and_name
    from app.services.pipeline import portal_port as pipeline

    async with tenant_session(tenant_id) as db:
        audit.set_actor(db, "system", None)
        candidate_id = await pipeline.application_candidate(db, application_id)
        demand_id = await pipeline.application_demand(db, application_id)
        if candidate_id is None:
            return "skipped"
        email = await candidates.primary_email(db, candidate_id)
        demand = await db.get(Demand, demand_id) if demand_id else None
        slug, tenant_name = await tenant_slug_and_name(db)
    if not email:
        return "skipped"
    privacy_link = f"{get_settings().frontend_url.rstrip('/')}/careers/{slug}/privacy" if slug else get_settings().frontend_url
    body = NOTICE_BODY.format(tenant=tenant_name or "We", job=demand.title if demand else "the job", board=board_label(board_key), privacy_link=privacy_link)
    await send_email(
        EmailMessage(
            to=email, subject=f"{tenant_name}: {NOTICE_TITLE.lower()}", text=f"Hello,\n\n{body}\n\n{tenant_name}\n",
            tenant_id=tenant_id, kind="job_board_application_notice", from_name=tenant_name or None,
        )
    )  # fmt: skip
    return "sent"


async def applicant_digest(tenant_id: uuid.UUID) -> int:
    """Counts per posting of the last 24 h to each Demand owner (no personal data)."""
    from app.services.job_distribution.notify import fan_out

    async with tenant_session(tenant_id) as db:
        audit.set_actor(db, "system", None)
        settings = await board_settings(db)
        if not settings.applicant_digest:
            return 0
        rows = (
            await db.execute(
                select(Demand.owner_id, Demand.title, BoardPosting.board_key, func.count(BoardApplicantIngest.id))
                .join(BoardPosting, BoardPosting.id == BoardApplicantIngest.board_posting_id)
                .join(Demand, Demand.id == BoardPosting.demand_id)
                .where(BoardApplicantIngest.status.in_(("created", "linked")), BoardApplicantIngest.received_at >= now() - timedelta(days=1))
                .group_by(Demand.owner_id, Demand.title, BoardPosting.board_key)
            )
        ).all()
    per_owner: dict[uuid.UUID, list[str]] = defaultdict(list)
    for owner, title, key, n in rows:
        per_owner[owner].append(f"{title} on {board_label(key)}: {int(n)}")
    day = now().date().isoformat()
    for owner, lines in per_owner.items():
        fan_out(tenant_id, "job_board_applicants_digest", {"event_id": f"job_board_applicants_digest:{owner}:{day}", "user_id": str(owner), "lines": lines[:20]})
    return len(per_owner)
