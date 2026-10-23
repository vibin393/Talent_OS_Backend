# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Phase 4 AEDT coverage inside ``fairness`` (Epic 65 story 65.6, FR-323, NFR-52, AD-47, AD-71).

* ``SEED_P4`` / ``ensure_seed_p4`` -- inventory entries for the agents that assist selection, ``inactive`` until
  Compliance confirms them and ``used_to_filter=False`` (agents assist; a person decides). ``version`` /
  ``first_used_on`` come from ``agent_principals`` / ``agent_runs`` projections; a version change is audited.
* three selection points (``SELECTORS_P4`` / ``DEFINITIONS_P4``), each offered only when its source projection exists.
* ``notice_lines`` -- the LL144 sentence listing the active agent entries.
* ``entry_status`` / ``request_confirmation`` / ``request_application`` -- read helpers for ``ai_governance``.

``fairness`` may not import Phase 4 code (``p4-below``): Phase 4 tables are read only through ``sqlalchemy.table()``
projections. Ids stay in the run task's memory only (P3 rule); snapshots stay aggregate.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Integer, String, Uuid, and_, column, func, select, table, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.models.fairness import AedtAlternativeRequest, AedtInventory

# (seed_key == feature, agent key, name, data used)
SEED_P4: tuple[tuple[str, str, str, str], ...] = (
    ("agent_sourcing", "sourcing", "Sourcing Agent shortlist ranking",
     "Deterministic AI Match scores, skills, experience, location and availability; engagement history. No protected or self-ID data."),
    ("agent_screening", "screening", "Screening Agent summaries and flags",
     "Candidate answers in screening conversations and the resume text."),
    ("agent_voice_screening", "voice_screening", "Voice Screening summaries and flags",
     "Text transcripts of screening calls. No audio or voice features are analysed."),
    ("agent_interview_assist", "interview_assist", "Interview Assistant draft ratings",
     "Interview transcripts and the interview kit questions and rating scales."),
)  # fmt: skip
FEATURE_BY_AGENT = {agent: feature for feature, agent, _n, _d in SEED_P4}
NAME_BY_AGENT = {agent: name for _f, agent, name, _d in SEED_P4}

# ---------------------------------------------------------------- projections (read-only)
_principals = table("agent_principals", column("agent_key", String), column("spec_version", Integer))
_runs = table("agent_runs", column("agent_key", String), column("created_at"))
_entries = table(
    "agent_shortlist_entries", column("id", Uuid), column("shortlist_id", Uuid), column("demand_id", Uuid),
    column("candidate_id", Uuid), column("status", String), column("created_at"),
)  # fmt: skip
_shortlists = table(
    "agent_shortlists", column("id", Uuid), column("demand_id", Uuid), column("version", Integer)
)
_conversations = table(
    "screening_conversations", column("application_id", Uuid), column("candidate_id", Uuid), column("demand_id", Uuid),
    column("status", String), column("completed_at"), column("flags", JSONB), column("gaps", JSONB),
)  # fmt: skip
_voice = table(
    "voice_sessions", column("application_id", Uuid), column("candidate_id", Uuid), column("status", String),
    column("reviewed_at"), column("gaps", JSONB),
)  # fmt: skip
_drafts = table(
    "interview_scorecard_drafts", column("id", Uuid), column("interview_id", Uuid), column("status", String)
)
_decisions = table(
    "scorecard_suggestion_decisions", column("draft_id", Uuid), column("decision", String), column("decided_at"),
)  # fmt: skip

POINT_SOURCES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "agent_shortlist_added": (
        ("agent_shortlist_entries", ("shortlist_id", "demand_id", "candidate_id", "status", "created_at")),
        ("agent_shortlists", ("id", "demand_id", "version")),
    ),
    "agent_screening_flag_advanced": (
        (
            "screening_conversations",
            ("application_id", "candidate_id", "status", "completed_at", "flags", "gaps"),
        ),
    ),
    "agent_draft_rating_accepted": (
        ("interview_scorecard_drafts", ("id", "interview_id", "status")),
        ("scorecard_suggestion_decisions", ("draft_id", "decision", "decided_at")),
    ),
}
DEFINITIONS_P4 = {
    "agent_shortlist_added": "Applicants are candidates on the latest Sourcing Agent shortlist of a Demand created in the date range; selected applicants were added to the pipeline.",
    "agent_screening_flag_advanced": "Applicants are AI screenings (text conversations completed or voice sessions reviewed) in the date range with at least one flag or gap; selected applicants reached a later pipeline stage within 30 days.",
    "agent_draft_rating_accepted": "Applicants are interviews with an Interview Assistant draft decided in the date range; selected applicants had at least one suggested rating accepted.",
}
LABELS_P4 = {
    "agent_shortlist_added": "Agent shortlist → added to pipeline",
    "agent_screening_flag_advanced": "Agent screening flag → advanced",
    "agent_draft_rating_accepted": "Interview Assistant draft rating accepted",
}


async def _present(db: AsyncSession, name: str, cols: tuple[str, ...]) -> bool:
    rows = await db.execute(
        text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = :t"
        ),
        {"t": name},
    )
    have = {str(r[0]) for r in rows}
    return set(cols) <= have


async def point_available(db: AsyncSession, key: str) -> bool:
    for name, cols in POINT_SOURCES.get(key, ()):
        if not await _present(db, name, cols):
            return False
    return True


# ================================================================ inventory seeds
async def ensure_seed_p4(db: AsyncSession) -> None:
    from app.services.fairness.common import tenant_of

    have = {
        r.seed_key: r
        for r in (
            await db.execute(select(AedtInventory).where(AedtInventory.seed_key.in_([s[0] for s in SEED_P4])))
        ).scalars()
    }
    tenant_id = tenant_of(db)
    principals_ok = await _present(db, "agent_principals", ("agent_key", "spec_version"))
    runs_ok = await _present(db, "agent_runs", ("agent_key", "created_at"))
    for key, agent_key, name, data_used in SEED_P4:
        row = have.get(key)
        if row is None:
            row = AedtInventory(
                id=uuid.uuid4(), tenant_id=tenant_id, seed_key=key, feature=key, name=name, data_used=data_used,
                used_to_filter=False, status="inactive", job_categories=[],
            )  # fmt: skip
            db.add(row)
        if principals_ok:
            version = (
                await db.execute(
                    select(_principals.c.spec_version).where(_principals.c.agent_key == agent_key)
                )
            ).scalar_one_or_none()
            if version is not None and row.version != str(version):
                if row.version is not None:
                    audit.record(
                        "aedt.version_changed", "aedt_inventory", row.id, session=db, feature=key,
                        before=row.version, after=str(version),
                    )  # fmt: skip
                row.version = str(version)
        if runs_ok and row.first_used_on is None:
            first = (
                await db.execute(select(func.min(_runs.c.created_at)).where(_runs.c.agent_key == agent_key))
            ).scalar_one_or_none()
            if first is not None:
                row.first_used_on = first.date()
    await db.flush()


async def entry_status(db: AsyncSession, agent_key: str) -> str | None:
    feature = FEATURE_BY_AGENT.get(agent_key)
    if feature is None:
        return None
    await ensure_seed_p4(db)
    return (
        await db.execute(select(AedtInventory.status).where(AedtInventory.seed_key == feature))
    ).scalar_one_or_none()


async def request_confirmation(db: AsyncSession, agent_key: str) -> int:
    """A Task "Confirm the AEDT inventory entry for <Agent>" for each Compliance user (idempotent per agent and user)."""
    from app.models.tasks import Task
    from app.services import directory_service
    from app.services.fairness.common import tenant_of

    name = NAME_BY_AGENT.get(agent_key)
    if name is None:
        return 0
    role_id = await directory_service.role_id_by_key(db, "compliance")
    users = await directory_service.user_ids_with_role(db, role_id) if role_id else set()
    title = f"Confirm the AEDT inventory entry for {name}"[:200]
    existing = set(
        (await db.execute(select(Task.owner_id).where(Task.title == title, Task.status == "open"))).scalars()
    )
    created = 0
    for uid in sorted(users - existing):
        db.add(Task(
            id=uuid.uuid4(), tenant_id=tenant_of(db), title=title,
            notes="An AI agent that assists selection was turned on. Review its AEDT inventory entry (NYC LL144) and set it to active.",
            owner_id=uid, created_by=None, entity_type=None, entity_id=None, due_at=None, due_all_day=False,
            priority="normal", status="open", source="workflow",
        ))  # fmt: skip
        created += 1
    if created:
        await db.flush()
    return created


def notice_lines(entries: list[Any]) -> list[str]:
    """The LL144 sentence listing the active agent entries (empty when none is active)."""
    names = [e.name for e in entries if (e.feature or "").startswith("agent_") and e.status == "active"]
    if not names:
        return []
    return [f"AI assistants used in this process: {', '.join(names)}. A person makes every decision."]


async def request_application(db: AsyncSession, request_id: uuid.UUID) -> tuple[uuid.UUID, str] | None:
    """(application id, status) of an alternative-process request (public reader for ``ai_governance``)."""
    row = await db.get(AedtAlternativeRequest, request_id)
    return (row.application_id, row.status) if row is not None else None


async def open_request_for(db: AsyncSession, application_id: uuid.UUID) -> bool:
    """Whether an ``open`` or ``handled`` alternative-process request exists for the Application."""
    return (
        await db.execute(
            select(func.count())
            .select_from(AedtAlternativeRequest)
            .where(
                AedtAlternativeRequest.application_id == application_id,
                AedtAlternativeRequest.status.in_(("open", "handled")),
            )
        )
    ).scalar_one() > 0


# ================================================================ selection points
Point = Any  # selection_points.Point (imported lazily to avoid a cycle)
Selector = Callable[[AsyncSession, dict[str, Any], datetime, datetime, dict[str, Any]], Awaitable[list[Any]]]


def _point(*args: Any) -> Any:
    from app.services.fairness.selection_points import Point

    return Point(*args)


def _demand_conds(filters: dict[str, Any], demand_col: Any) -> list[Any]:
    from app.services.fairness.selection_points import _demand_filter

    return _demand_filter(filters, demand_col)


async def agent_shortlist_added(
    db: AsyncSession,
    params: dict[str, Any],
    lo: datetime,
    hi: datetime,
    filters: dict[str, Any],
) -> list[Any]:
    latest = (
        select(_shortlists.c.demand_id, func.max(_shortlists.c.version).label("v")).group_by(
            _shortlists.c.demand_id
        )
    ).subquery()
    current = select(_shortlists.c.id).join(
        latest, and_(latest.c.demand_id == _shortlists.c.demand_id, latest.c.v == _shortlists.c.version)
    )
    stmt = select(_entries.c.candidate_id, _entries.c.demand_id, _entries.c.status).where(
        _entries.c.shortlist_id.in_(current), _entries.c.created_at >= lo, _entries.c.created_at < hi
    )
    for cond in _demand_conds(filters, _entries.c.demand_id):
        stmt = stmt.where(cond)
    seen: dict[tuple[Any, Any], Any] = {}
    for cid, did, status in (await db.execute(stmt)).tuples():
        key = (cid, did)
        selected = status == "added_to_pipeline"
        if key not in seen or selected:
            seen[key] = _point(cid, did, selected)
    return list(seen.values())


def _has_items(col: Any) -> Any:
    return func.jsonb_array_length(func.coalesce(col, text("'[]'::jsonb"))) > 0


async def agent_screening_flag_advanced(
    db: AsyncSession, params: dict[str, Any], lo: datetime, hi: datetime, filters: dict[str, Any]
) -> list[Any]:
    from app.models.pipeline import Application, ApplicationStageHistory, PipelineStage

    source = str(params.get("source") or "both")
    rows: list[tuple[Any, Any, Any, datetime]] = []  # (application, candidate, demand, completed)
    if source in ("text", "both"):
        stmt = (
            select(_conversations.c.application_id, _conversations.c.candidate_id, Application.demand_id, _conversations.c.completed_at)
            .join(Application, Application.id == _conversations.c.application_id)
            .where(_conversations.c.status == "completed", _conversations.c.completed_at >= lo,
                   _conversations.c.completed_at < hi,
                   (_has_items(_conversations.c.flags)) | (_has_items(_conversations.c.gaps)))
        )  # fmt: skip
        for cond in _demand_conds(filters, Application.demand_id):
            stmt = stmt.where(cond)
        rows.extend((await db.execute(stmt)).tuples())
    if source in ("voice", "both") and await _present(
        db, "voice_sessions", ("application_id", "status", "reviewed_at", "gaps")
    ):
        stmt = (
            select(_voice.c.application_id, _voice.c.candidate_id, Application.demand_id, _voice.c.reviewed_at)
            .join(Application, Application.id == _voice.c.application_id)
            .where(_voice.c.status == "reviewed", _voice.c.reviewed_at >= lo, _voice.c.reviewed_at < hi,
                   _has_items(_voice.c.gaps))
        )  # fmt: skip
        for cond in _demand_conds(filters, Application.demand_id):
            stmt = stmt.where(cond)
        rows.extend((await db.execute(stmt)).tuples())
    if not rows:
        return []
    app_ids = list({r[0] for r in rows})
    frm = PipelineStage.__table__.alias("frm")
    to = PipelineStage.__table__.alias("to_stage")
    moves = (
        await db.execute(
            select(ApplicationStageHistory.application_id, ApplicationStageHistory.at)
            .join(to, to.c.id == ApplicationStageHistory.to_stage_id)
            .outerjoin(frm, frm.c.id == ApplicationStageHistory.from_stage_id)
            .where(ApplicationStageHistory.application_id.in_(app_ids),
                   (frm.c.position.is_(None)) | (to.c.position > frm.c.position))
        )
    ).tuples()  # fmt: skip
    advanced: dict[Any, list[datetime]] = {}
    for aid, at in moves:
        advanced.setdefault(aid, []).append(at)
    limit = hi + timedelta(days=30)
    seen: dict[tuple[Any, Any], Any] = {}
    for aid, cid, did, done in sorted(rows, key=lambda r: r[3]):
        selected = any(done < at <= limit for at in advanced.get(aid, []))
        seen.setdefault((cid, did), _point(cid, did, selected))
    return list(seen.values())


async def agent_draft_rating_accepted(
    db: AsyncSession, params: dict[str, Any], lo: datetime, hi: datetime, filters: dict[str, Any]
) -> list[Any]:
    from app.models.interviews import Interview

    share = float(params.get("min_accepted_share") or 0)
    stmt = (
        select(_drafts.c.id, Interview.candidate_id, Interview.demand_id, _decisions.c.decision)
        .join(Interview, Interview.id == _drafts.c.interview_id)
        .join(_decisions, _decisions.c.draft_id == _drafts.c.id)
        .where(_decisions.c.decided_at >= lo, _decisions.c.decided_at < hi)
    )  # fmt: skip
    for cond in _demand_conds(filters, Interview.demand_id):
        stmt = stmt.where(cond)
    per_draft: dict[Any, list[Any]] = {}
    for draft_id, cid, did, decision in (await db.execute(stmt)).tuples():
        per_draft.setdefault(draft_id, [cid, did, 0, 0])
        per_draft[draft_id][3] += 1
        if decision == "accepted":
            per_draft[draft_id][2] += 1
    seen: dict[tuple[Any, Any], Any] = {}
    for cid, did, accepted, total in per_draft.values():
        selected = accepted >= 1 and (accepted / total if total else 0) >= share
        key = (cid, did)
        if key not in seen or selected:
            seen[key] = _point(cid, did, selected)
    return list(seen.values())


SELECTORS_P4: dict[str, Selector] = {
    "agent_shortlist_added": agent_shortlist_added,
    "agent_screening_flag_advanced": agent_screening_flag_advanced,
    "agent_draft_rating_accepted": agent_draft_rating_accepted,
}

__all__ = [
    "DEFINITIONS_P4",
    "FEATURE_BY_AGENT",
    "LABELS_P4",
    "POINT_SOURCES",
    "SEED_P4",
    "SELECTORS_P4",
    "ensure_seed_p4",
    "entry_status",
    "notice_lines",
    "open_request_for",
    "point_available",
    "request_application",
    "request_confirmation",
]
