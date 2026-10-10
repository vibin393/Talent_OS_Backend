"""Client Portal functions of the interviews domain (Epic 18, FR-101 / P1 FR-41). Append-only ``client_``.

* ``client_my_scorecards``: interviews of this client where an ``interviewer_external`` participant's email is the
  Client User's email and the scorecard is still open.
* Viewing / submitting reuses the P1 external scorecard service behind ``public_scorecard_view`` /
  ``public_scorecard_submit`` with the participant's open scorecard link, which the submission consumes (the
  link is then unusable). No open link -> 404.
* ``client_upcoming_interviews``: dates only (home tile).
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.models.candidates import Candidate
from app.models.demands import Demand
from app.models.interviews import Interview, InterviewParticipant, Scorecard
from app.schemas.interviews_portal import ClientScorecardItem, ClientUpcomingInterview
from app.services.client_portal_base import client_of, demand_in_scope, display_name
from app.services.interviews.scheduling import PARTICIPANT_SUBJECT, SCORECARD_PURPOSE


async def _my_email(db: AsyncSession, client_user_id: uuid.UUID) -> str | None:
    from app.models.external_auth import ClientUser

    raw = (
        await db.execute(select(ClientUser.email).where(ClientUser.id == client_user_id))
    ).scalar_one_or_none()
    return str(raw).lower() if raw else None


async def _participants(
    ctx: RequestContext, db: AsyncSession
) -> list[tuple[InterviewParticipant, Interview]]:
    _, client_id, me = client_of(ctx)
    email = await _my_email(db, me)
    if not email:
        return []
    rows = (
        await db.execute(
            select(InterviewParticipant, Interview)
            .join(Interview, Interview.id == InterviewParticipant.interview_id)
            .join(Demand, Demand.id == Interview.demand_id)
            .where(
                Demand.client_id == client_id,
                InterviewParticipant.kind == "interviewer_external",
                InterviewParticipant.removed_at.is_(None),
                InterviewParticipant.email == email,
                Interview.status != "cancelled",
            )
            .order_by(Interview.starts_at.desc())
        )
    ).all()
    return [(p, i) for p, i in rows if await demand_in_scope(ctx, db, i.demand_id)]


async def client_my_scorecards(ctx: RequestContext, db: AsyncSession) -> list[ClientScorecardItem]:
    pairs = await _participants(ctx, db)
    if not pairs:
        return []
    cards = {
        (c.interview_id, c.participant_id): c
        for c in (
            await db.execute(select(Scorecard).where(Scorecard.participant_id.in_([p.id for p, _ in pairs])))
        ).scalars()
    }
    cands = {
        c.id: c.full_name
        for c in (
            await db.execute(select(Candidate).where(Candidate.id.in_({i.candidate_id for _, i in pairs})))
        ).scalars()
    }
    titles = dict(
        (
            await db.execute(
                select(Demand.id, Demand.title).where(Demand.id.in_({i.demand_id for _, i in pairs}))
            )
        ).all()  # type: ignore[arg-type]
    )
    now = datetime.now(UTC)
    out: list[ClientScorecardItem] = []
    for p, i in pairs:
        card = cards.get((i.id, p.id))
        state = (
            "submitted"
            if card is not None and card.status == "submitted"
            else ("not_started" if i.starts_at > now else "open")
        )
        out.append(
            ClientScorecardItem(
                participant_id=p.id,
                interview_id=i.id,
                candidate_display_name=display_name(cands.get(i.candidate_id)),
                demand_title=titles.get(i.demand_id),
                stage_name=i.stage_name,
                starts_at=i.starts_at,
                state=state,
            )
        )
    return out


async def _open_link(
    ctx: RequestContext, db: AsyncSession, participant_id: uuid.UUID
) -> secure_links.ResolvedLink:
    tenant_id, _, _ = client_of(ctx)
    if participant_id not in {p.id for p, _ in await _participants(ctx, db)}:
        raise NotFound("Scorecard not found")
    async with tenant_session(tenant_id) as idb:
        links = await secure_links.links_for_subject(idb, PARTICIPANT_SUBJECT, participant_id)
        now = datetime.now(UTC)
        live = [
            link
            for link in links
            if link.purpose == SCORECARD_PURPOSE
            and link.revoked_at is None
            and link.use_count < link.max_uses
            and link.expires_at > now
        ]
        if not live:
            raise NotFound("Scorecard not found")
        link = max(live, key=lambda x: x.expires_at)
        return secure_links.ResolvedLink(
            id=link.id,
            tenant_id=link.tenant_id,
            purpose=link.purpose,
            subject_type=link.subject_type,
            subject_id=link.subject_id,
            expires_at=link.expires_at,
            max_uses=link.max_uses,
            use_count=link.use_count,
            meta=dict(link.meta or {}),
        )


async def client_scorecard_view(ctx: RequestContext, db: AsyncSession, participant_id: uuid.UUID) -> object:
    from app.services import interviews_service

    return await interviews_service.public_scorecard_view(await _open_link(ctx, db, participant_id))


async def client_submit_scorecard(
    ctx: RequestContext, db: AsyncSession, participant_id: uuid.UUID, data: object
) -> str:
    """Reuses the P1 external submit (actor recorded as the secure link of this participant), consuming the
    participant's scorecard link."""
    from app.services import interviews_service

    return str(
        await interviews_service.public_scorecard_submit(await _open_link(ctx, db, participant_id), data)
    )


async def client_upcoming_interviews(
    ctx: RequestContext, db: AsyncSession, limit: int = 20
) -> list[ClientUpcomingInterview]:
    _, client_id, _ = client_of(ctx)
    from app.core.client_scope import visible_demand_ids

    stmt = (
        select(Interview, Demand.title, Candidate.full_name)
        .join(Demand, Demand.id == Interview.demand_id)
        .join(Candidate, Candidate.id == Interview.candidate_id)
        .where(
            Demand.client_id == client_id,
            Interview.status == "scheduled",
            Interview.starts_at > datetime.now(UTC),
        )
        .order_by(Interview.starts_at)
        .limit(limit)
    )
    scope = await visible_demand_ids(ctx, db)
    if scope is not None:
        stmt = stmt.where(Interview.demand_id.in_(scope or [uuid.uuid4()]))
    return [
        ClientUpcomingInterview(
            interview_id=i.id, demand_title=t, candidate_display_name=display_name(n), starts_at=i.starts_at
        )
        for i, t, n in (await db.execute(stmt)).all()
    ]
