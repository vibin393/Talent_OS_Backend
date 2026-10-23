# ruff: file-ignore[line-too-long]
"""Test taking (Epic 40 A6, FR-204, NFR-35), shared by the Candidate Portal (``/portal/assessments``) and the Secure
Link page (``/public/links/{token}/assessment``).

* **The server clock is authoritative.** ``start`` computes ``deadline_at = now + time_limit x accommodation``; the
  invitation's deadline only bounds when a start is allowed. Every response carries ``server_now`` /
  ``remaining_seconds``. Writes after ``deadline_at + 30 s`` -> 409 ``attempt_deadline_passed``; overdue attempts
  are auto-submitted (beat ``assessments.auto_submit``, or lazily on the next read).
* **One active attempt per invitation** (partial UNIQUE). ``start`` is idempotent: a repeat returns the active
  attempt. Each start / resume issues a session id (returned once; sent back as ``X-Attempt-Session``); a resume from
  another session issues a new one, logs ``concurrent_session`` and the old session's writes get 409
  ``attempt_session_replaced``. Only the SHA-256 of the session id is stored.
* **Sections are sequential**: only the current section accepts answers; entering section k sets its deadline
  ``min(now + limit_k x factor, attempt deadline)``; ``finish`` closes it early (409 ``section_closed`` otherwise).
* **Autosave** upserts with a per-answer monotonic ``client_seq`` (stale writes are ignored), validates each response
  per kind and caps it at 20 KB.
* Candidate code is never executed; delivery never carries keys (``delivery.py``).
"""

import dataclasses
import hashlib
import math
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Integer, column, select, update, values
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import NotFound
from app.models.assessments import (
    SIGNAL_KINDS,
    Assessment,
    AssessmentInvitation,
    AssessmentItemVersion,
    AssessmentVersion,
    Attempt,
    AttemptAnswer,
)
from app.schemas.assessments_delivery import (
    AttemptView,
    NoticeOut,
    PortalInvitationOut,
    PreStartOut,
    PreStartSectionOut,
    SaveIn,
    SaveOut,
    SectionStateOut,
    SignalsAcceptedOut,
    SignalsIn,
    StartIn,
    SubmitOut,
)
from app.services.assessments import delivery, lifecycle, proctoring, results
from app.services.assessments import draw as draw_mod
from app.services.assessments.common import (
    CONSENT_PURPOSE,
    LINK_PURPOSE,
    SUBMIT_GRACE_SECONDS,
    conflict,
    invalid,
    now,
)

GRACE = timedelta(seconds=SUBMIT_GRACE_SECONDS)


@dataclasses.dataclass(frozen=True, slots=True)
class Subject:
    tenant_id: uuid.UUID
    invitation_id: uuid.UUID
    actor_kind: str  # candidate | secure_link
    actor_id: uuid.UUID | None
    candidate_id: uuid.UUID | None = None  # portal: the signed-in Candidate (must own the invitation)
    ip: str | None = None


def portal_subject(
    tenant_id: uuid.UUID, candidate_id: uuid.UUID, invitation_id: uuid.UUID, ip: str | None
) -> Subject:
    return Subject(tenant_id, invitation_id, "candidate", candidate_id, candidate_id, ip)


async def link_subject(token: str, ip: str | None) -> Subject:
    link = await secure_links.resolve(token, (LINK_PURPOSE,))
    return Subject(link.tenant_id, link.subject_id, "secure_link", link.id, None, ip)


@asynccontextmanager
async def _db(subject: Subject) -> AsyncIterator[AsyncSession]:
    async with tenant_session(subject.tenant_id) as db:
        yield db


def _ctx(subject: Subject) -> RequestContext:
    return RequestContext(tenant_id=subject.tenant_id, ip=subject.ip)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _ip_hash(ip: str | None) -> str | None:
    return hashlib.sha256(ip.encode()).hexdigest()[:64] if ip else None


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(raw: str | None) -> datetime | None:
    return datetime.fromisoformat(raw) if raw else None


# ---------------------------------------------------------------- loading
async def _invitation(
    db: AsyncSession, subject: Subject, *, for_update: bool = False
) -> AssessmentInvitation:
    stmt = select(AssessmentInvitation).where(
        AssessmentInvitation.id == subject.invitation_id, AssessmentInvitation.candidate_id.is_not(None)
    )
    if subject.candidate_id is not None:
        stmt = stmt.where(AssessmentInvitation.candidate_id == subject.candidate_id)
    if for_update:
        stmt = stmt.with_for_update()
    inv = (await db.execute(stmt)).scalar_one_or_none()
    if inv is None:
        raise NotFound(
            "Assessment not found"
        )  # another candidate's invitation is indistinguishable from none
    return inv


async def _latest_attempt(
    db: AsyncSession, inv: AssessmentInvitation, *, for_update: bool = False
) -> Attempt | None:
    stmt = select(Attempt).where(Attempt.invitation_id == inv.id).order_by(Attempt.attempt_no.desc()).limit(1)
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


def _effective_minutes(limit: int | None, factor: Decimal) -> int | None:
    return math.ceil(Decimal(limit) * factor) if limit else None


async def _notice(db: AsyncSession, tenant_id: uuid.UUID) -> NoticeOut:
    from app.services.privacy import consent

    row = None
    try:
        async with db.begin_nested():
            row = await consent.active_notice(db, tenant_id, CONSENT_PURPOSE)
    except Exception:
        row = None
    if row is not None:
        return NoticeOut(notice_id=row.id, title=row.title, body_md=row.body_md)
    title, body = consent.DEFAULT_NOTICES.get(CONSENT_PURPOSE, ("Online assessment", ""))
    return NoticeOut(notice_id=None, title=title, body_md=body)


# ---------------------------------------------------------------- portal list / pre-start
async def portal_list(tenant_id: uuid.UUID, candidate_id: uuid.UUID) -> list[PortalInvitationOut]:
    async with tenant_session(tenant_id) as db:
        rows = (
            await db.execute(
                select(AssessmentInvitation, Assessment.name)
                .join(Assessment, Assessment.id == AssessmentInvitation.assessment_id)
                .where(
                    AssessmentInvitation.candidate_id == candidate_id,
                    AssessmentInvitation.status != "cancelled",
                )
                .order_by(AssessmentInvitation.created_at.desc())
            )
        ).all()
        return [
            PortalInvitationOut(
                invitation_id=i.id, assessment_name=name, status=i.status, deadline_at=i.deadline_at
            )
            for i, name in rows
        ]


async def prestart(subject: Subject) -> PreStartOut:
    async with _db(subject) as db:
        inv = await _invitation(db, subject)
        assessment = await db.get(Assessment, inv.assessment_id)
        version = (
            await db.get(AssessmentVersion, inv.assessment_version_id) if inv.assessment_version_id else None
        )
        factor = Decimal(inv.accommodation_factor)
        sections = [
            PreStartSectionOut(
                key=s["key"], title=s["title"],
                item_count=int(s["draw"]["count"]) if s.get("draw") else len(s.get("items") or []),
                time_limit_minutes=_effective_minutes(s.get("time_limit_minutes"), factor),
            )
            for s in (version.sections if version else [])
        ]  # fmt: skip
        active = (
            await db.execute(
                select(Attempt.id).where(Attempt.invitation_id == inv.id, Attempt.status == "in_progress")
            )
        ).first()
        return PreStartOut(
            invitation_id=inv.id,
            assessment_name=assessment.name if assessment else "",
            instructions=assessment.instructions if assessment else None,
            status=inv.status,
            deadline_at=inv.deadline_at,
            time_limit_minutes=_effective_minutes(version.time_limit_minutes if version else None, factor),
            accommodation_factor=factor,
            screen_reader_mode=inv.screen_reader_mode,
            sections=sections,
            notice=await _notice(db, inv.tenant_id),
            logged_signals=list(SIGNAL_KINDS),
            has_active_attempt=active is not None,
            delivery=assessment.delivery if assessment else "builtin",
            take_url=None,
        )


# ---------------------------------------------------------------- plan / view
async def _build_plan(
    db: AsyncSession,
    inv: AssessmentInvitation,
    version: AssessmentVersion,
    assessment: Assessment,
    attempt_no: int,
) -> tuple[list[dict[str, Any]], dict[str, AssessmentItemVersion]]:
    rng = draw_mod.rng_for(inv.id, f"attempt:{attempt_no}")
    chosen: list[tuple[dict[str, Any], list[tuple[str, Decimal]]]] = []
    for s in version.sections or []:
        if s.get("draw"):
            rule = s["draw"]
            pool = s.get("pool") or []
            points = {p["item_version_id"]: Decimal(str(p["points"])) for p in pool}
            picked = draw_mod.draw(
                [(p["item_version_id"], int(p["difficulty"])) for p in pool],
                int(rule["count"]),
                tuple(rule.get("difficulty") or (1, 5)),
                rng,
            )  # type: ignore[arg-type]
            entries = [(vid, points[vid]) for vid in picked]
        else:
            entries = [(e["item_version_id"], Decimal(str(e["points"]))) for e in s.get("items") or []]
        if assessment.shuffle_items:
            entries = draw_mod.shuffled(entries, rng)
        chosen.append((s, entries))
    ids = [uuid.UUID(vid) for _, entries in chosen for vid, _ in entries]
    versions = {
        str(v.id): v
        for v in (
            await db.execute(select(AssessmentItemVersion).where(AssessmentItemVersion.id.in_(ids)))
        ).scalars()
    }
    kinds = await _kinds(db, list(versions.values()))
    plan: list[dict[str, Any]] = []
    position = 0
    for s, entries in chosen:
        items = []
        for vid, pts in entries:
            position += 1
            v = versions[vid]
            items.append(delivery.plan_entry(position, kinds[v.id], v, pts, assessment.shuffle_options, rng))
        plan.append(
            {
                "key": s["key"],
                "title": s["title"],
                "time_limit_minutes": s.get("time_limit_minutes"),
                "items": items,
            }
        )
    return plan, versions


async def _kinds(db: AsyncSession, versions: list[AssessmentItemVersion]) -> dict[uuid.UUID, str]:
    from app.models.assessments import AssessmentItem

    if not versions:
        return {}
    rows = await db.execute(
        select(AssessmentItem.id, AssessmentItem.kind).where(
            AssessmentItem.id.in_({v.item_id for v in versions})
        )
    )
    by_item = dict(rows.tuples().all())
    return {v.id: by_item[v.item_id] for v in versions}


def _enter(att: Attempt, key: str, factor: Decimal) -> None:
    section = next((s for s in att.section_plan or [] if s["key"] == key), None)
    limit = _effective_minutes(section.get("time_limit_minutes") if section else None, factor)
    deadline = min(now() + timedelta(minutes=limit), att.deadline_at) if limit else None
    states = dict(att.section_deadlines or {})
    states[key] = {
        "deadline": _iso(deadline) if deadline else None,
        "closed": False,
        "entered_at": _iso(now()),
    }
    att.section_deadlines = states
    att.current_section = key


def _section_closed(att: Attempt, key: str) -> bool:
    state = (att.section_deadlines or {}).get(key)
    if state is None:
        return True  # not entered yet (sections are sequential)
    if state.get("closed"):
        return True
    deadline = _parse(state.get("deadline"))
    return deadline is not None and now() > deadline + GRACE


def _shown_closed(att: Attempt, key: str) -> bool:
    """Display state: submitted attempts and already-left sections are closed; future sections are not yet."""
    if att.status != "in_progress":
        return True
    if key not in (att.section_deadlines or {}):
        return False
    return key != att.current_section or _section_closed(att, key)


def _next_key(att: Attempt, key: str | None) -> str | None:
    keys = [s["key"] for s in att.section_plan or []]
    if key not in keys:
        return keys[0] if keys else None
    i = keys.index(key)
    return keys[i + 1] if i + 1 < len(keys) else None


def _close_and_advance(att: Attempt, factor: Decimal) -> None:
    cur = att.current_section
    if cur is None:
        return
    states = dict(att.section_deadlines or {})
    states[cur] = {**states.get(cur, {}), "closed": True}
    att.section_deadlines = states
    nxt = _next_key(att, cur)
    if nxt is not None:
        _enter(att, nxt, factor)


def _advance_if_expired(att: Attempt, factor: Decimal) -> None:
    for _ in range(len(att.section_plan or [])):
        cur = att.current_section
        if cur is None or not _section_closed(att, cur) or _next_key(att, cur) is None:
            return
        _close_and_advance(att, factor)


async def _view(
    db: AsyncSession, inv: AssessmentInvitation, att: Attempt, session_token: str | None
) -> AttemptView:
    entries = [(s["key"], e) for s in att.section_plan or [] for e in s.get("items") or []]
    ids = [uuid.UUID(e["item_version_id"]) for _, e in entries]
    versions = {
        v.id: v
        for v in (
            await db.execute(select(AssessmentItemVersion).where(AssessmentItemVersion.id.in_(ids)))
        ).scalars()
    }
    answers = {
        pos: resp
        for pos, resp in (
            await db.execute(
                select(AttemptAnswer.position, AttemptAnswer.response).where(
                    AttemptAnswer.attempt_id == att.id
                )
            )
        ).tuples()
        if resp is not None
    }
    states = att.section_deadlines or {}
    t = now()
    return AttemptView(
        session_id=session_token,
        server_now=t,
        deadline_at=att.deadline_at,
        remaining_seconds=max(0, int((att.deadline_at - t).total_seconds()))
        if att.status == "in_progress"
        else 0,
        status=att.status,
        current_section=att.current_section,
        screen_reader_mode=inv.screen_reader_mode,
        sections=[
            SectionStateOut(
                key=s["key"],
                title=s["title"],
                deadline_at=_parse((states.get(s["key"]) or {}).get("deadline")),
                closed=_shown_closed(att, s["key"]),
                positions=[int(e["position"]) for e in s.get("items") or []],
            )
            for s in att.section_plan or []
        ],
        items=[
            delivery.delivery_item(e, key, versions[uuid.UUID(e["item_version_id"])]) for key, e in entries
        ],
        answers=answers,
    )


def _new_session(att: Attempt) -> str:
    token = secrets.token_urlsafe(24)
    att.session_id = _hash(token)
    return token


def _check_session(att: Attempt, header: str | None) -> None:
    if not header or att.session_id is None or _hash(header) != att.session_id:
        raise conflict("attempt_session_replaced", "This test was opened in another window or device")


def _check_ip(db: AsyncSession, att: Attempt, ip: str | None) -> None:
    h = _ip_hash(ip)
    if h and att.last_ip_hash and h != att.last_ip_hash:
        proctoring.server_signal(db, att, "ip_change")
    if h:
        att.last_ip_hash = h


# ---------------------------------------------------------------- submit (shared with the beat)
async def finish_attempt(
    db: AsyncSession, inv: AssessmentInvitation, att: Attempt, status: str, actor_kind: str
) -> None:
    att.status = status
    att.submitted_at = now()
    await db.flush()
    if inv.status == "started":
        await lifecycle.move(db, inv, "completed", actor_kind)
    pending = await results.score_attempt(db, inv, att)
    if not pending:
        await results.complete_review_if_done(db, inv, att)


async def _resume(
    db: AsyncSession, inv: AssessmentInvitation, att: Attempt, subject: Subject, header: str | None
) -> AttemptView:
    factor = Decimal(inv.accommodation_factor)
    if att.status == "in_progress" and now() > att.deadline_at + GRACE:
        await finish_attempt(db, inv, att, "auto_submitted", "system")
        return await _view(db, inv, att, None)
    token: str | None = None
    if att.status == "in_progress":
        if not header or att.session_id is None or _hash(header) != att.session_id:
            if att.session_id is not None:
                proctoring.server_signal(db, att, "concurrent_session")
            token = _new_session(att)
        _check_ip(db, att, subject.ip)
        _advance_if_expired(att, factor)
        await db.flush()
    return await _view(db, inv, att, token)


# ---------------------------------------------------------------- API
async def start(subject: Subject, data: StartIn, header: str | None = None) -> AttemptView:
    from app.services.privacy import consent

    if not data.agreed:
        raise invalid("consent_required", "Read and accept the notice before starting", "agreed")
    async with _db(subject) as db:
        inv = await _invitation(db, subject, for_update=True)
        assessment = await db.get(Assessment, inv.assessment_id)
        if assessment is None or inv.assessment_version_id is None:
            raise NotFound("Assessment not found")
        if assessment.delivery == "provider":
            raise conflict("provider_delivery", "This assessment is taken on the provider's site")
        if inv.status == "started":
            att = await _latest_attempt(db, inv, for_update=True)
            if att is not None:
                return await _resume(db, inv, att, subject, header)
        lifecycle.require(inv.status, "started", subject.actor_kind)
        if inv.deadline_at <= now():
            raise conflict("invitation_expired", "The deadline to start this assessment has passed")
        previous = await _latest_attempt(db, inv)
        attempt_no = (previous.attempt_no if previous else 0) + 1
        if attempt_no > assessment.attempts_limit:
            raise conflict("attempts_exhausted", "No attempts left")
        notice_id = data.notice_id
        row = await consent.grant_consent(
            _ctx(subject), db, inv.candidate_id,  # type: ignore[arg-type]
            CONSENT_PURPOSE, source="candidate_portal" if subject.actor_kind == "candidate" else "candidate_link",
            notice_id=notice_id, evidence={"invitation_id": str(inv.id), "via": subject.actor_kind},
        )  # fmt: skip
        inv.consent_id = row.id
        await lifecycle.move(db, inv, "started", subject.actor_kind, actor_id=subject.actor_id)
        version = await db.get(AssessmentVersion, inv.assessment_version_id)
        assert version is not None
        plan, versions = await _build_plan(db, inv, version, assessment, attempt_no)
        factor = Decimal(inv.accommodation_factor)
        limit = _effective_minutes(version.time_limit_minutes, factor)
        started = now()
        deadline = (
            started + timedelta(minutes=limit)
            if limit
            else max(inv.deadline_at, started + timedelta(minutes=5))
        )
        att = Attempt(
            id=uuid.uuid4(), tenant_id=inv.tenant_id, invitation_id=inv.id, attempt_no=attempt_no, status="in_progress",
            started_at=started, deadline_at=deadline, section_plan=plan, section_deadlines={}, last_ip_hash=_ip_hash(subject.ip),
        )  # fmt: skip
        token = _new_session(att)
        db.add(att)
        await db.flush()
        for s in plan:
            for e in s["items"]:
                db.add(
                    AttemptAnswer(
                        id=uuid.uuid4(), tenant_id=inv.tenant_id, attempt_id=att.id, item_version_id=versions[e["item_version_id"]].id,
                        position=int(e["position"]), section_key=s["key"], max_points=Decimal(str(e["points"])), client_seq=0,
                    )
                )  # fmt: skip
        if plan:
            _enter(att, plan[0]["key"], factor)
        await db.flush()
        return await _view(db, inv, att, token)


async def attempt(subject: Subject, header: str | None) -> AttemptView:
    async with _db(subject) as db:
        inv = await _invitation(db, subject, for_update=True)
        att = await _latest_attempt(db, inv, for_update=True)
        if att is None:
            raise conflict("attempt_not_started", "The assessment has not been started")
        return await _resume(db, inv, att, subject, header)


async def _active(
    db: AsyncSession, subject: Subject, header: str | None, *, for_update: bool = False
) -> tuple[AssessmentInvitation, Attempt]:
    inv = await _invitation(db, subject, for_update=for_update)
    att = await _latest_attempt(db, inv, for_update=for_update)
    if att is None:
        raise conflict("attempt_not_started", "The assessment has not been started")
    if att.status != "in_progress":
        raise conflict("attempt_already_submitted", "This attempt was already submitted")
    _check_session(att, header)
    if inv.status != "started":
        raise conflict("transition_not_allowed", "This invitation is no longer open", status=inv.status)
    if now() > att.deadline_at + GRACE:
        raise conflict("attempt_deadline_passed", "Time is up -- your answers were submitted")
    return inv, att


async def save(subject: Subject, header: str | None, data: SaveIn) -> SaveOut:
    async with _db(subject) as db:
        inv, att = await _active(db, subject, header)
        plan = results.plan_index(att)
        t = now()
        rows: dict[int, Any] = {}
        for ans in data.answers:
            key_entry = plan.get(ans.position)
            if key_entry is None:
                raise invalid("unknown_position", "No such question", f"answers[{ans.position}]")
            section_key, entry = key_entry
            if section_key != att.current_section or _section_closed(att, section_key):
                raise conflict("section_closed", "This section is closed", section=section_key)
            rows[ans.position] = delivery.validate_response(str(entry["kind"]), ans.response, ans.position)
        saved = 0
        if rows:
            # One statement for the whole batch (autosave p95 < 300 ms); stale client_seq rows are skipped.
            batch = values(
                column("position", Integer), column("response", JSONB), name="batch"
            ).data(list(rows.items()))
            result = await db.execute(
                update(AttemptAnswer)
                .where(
                    AttemptAnswer.attempt_id == att.id,
                    AttemptAnswer.position == batch.c.position,
                    AttemptAnswer.client_seq < data.client_seq,
                )
                .values(response=batch.c.response, client_seq=data.client_seq, saved_at=t)
                .returning(AttemptAnswer.id)
                .execution_options(synchronize_session=False)
            )
            saved = len(result.all())
        del inv
        return SaveOut(
            saved=saved,
            server_now=t,
            deadline_at=att.deadline_at,
            remaining_seconds=max(0, int((att.deadline_at - t).total_seconds())),
        )


async def finish_section(subject: Subject, header: str | None, key: str) -> AttemptView:
    async with _db(subject) as db:
        inv, att = await _active(db, subject, header, for_update=True)
        if key != att.current_section or _section_closed(att, key):
            raise conflict("section_closed", "This section is closed", section=key)
        _close_and_advance(att, Decimal(inv.accommodation_factor))
        await db.flush()
        return await _view(db, inv, att, None)


async def signals(subject: Subject, header: str | None, data: SignalsIn) -> SignalsAcceptedOut:
    async with _db(subject) as db:
        _, att = await _active(db, subject, header, for_update=True)
        return await proctoring.record(db, att, data)


async def submit(subject: Subject, header: str | None) -> SubmitOut:
    async with _db(subject) as db:
        inv = await _invitation(db, subject, for_update=True)
        att = await _latest_attempt(db, inv, for_update=True)
        if att is None:
            raise conflict("attempt_not_started", "The assessment has not been started")
        if att.status != "in_progress":
            raise conflict("attempt_already_submitted", "This attempt was already submitted")
        _check_session(att, header)
        if now() > att.deadline_at + GRACE:
            raise conflict("attempt_deadline_passed", "Time is up -- your answers were submitted")
        await finish_attempt(db, inv, att, "submitted", subject.actor_kind)
        return SubmitOut(status=att.status, submitted_at=att.submitted_at)  # type: ignore[arg-type]


async def auto_submit_due(db: AsyncSession) -> int:
    """Beat body: ``in_progress`` attempts past ``deadline_at + 30 s`` -> ``auto_submitted`` (system) and scored."""
    rows = list(
        (
            await db.execute(
                select(Attempt)
                .where(Attempt.status == "in_progress", Attempt.deadline_at < now() - GRACE)
                .with_for_update(skip_locked=True)
                .limit(500)
            )
        ).scalars()
    )
    for att in rows:
        inv = await db.get(AssessmentInvitation, att.invitation_id, with_for_update=True)
        if inv is None:
            continue
        if inv.status == "started":
            await finish_attempt(db, inv, att, "auto_submitted", "system")
        else:
            att.status = "auto_submitted"
            att.submitted_at = now()
    await db.flush()
    return len(rows)


__all__ = [
    "Subject",
    "attempt",
    "auto_submit_due",
    "finish_attempt",
    "finish_section",
    "link_subject",
    "portal_list",
    "portal_subject",
    "prestart",
    "save",
    "signals",
    "start",
    "submit",
]
