# ruff: file-ignore[line-too-long, unused-function-argument]
"""Registrations of the ``scheduling_agent`` domain (autodiscovered by ``core.hooks``), Epic 59.

* P1 reminder owner (coordinator C-11): ``interviews.invites.register_reminder_owner(reminders.owns_reminders)``.
* Notification types (FR-327): ``interview_slot_picked``, ``interview_booked_by_agent``, ``interview_rescheduled_by_agent``
  (in-app), ``scheduling_handed_off`` (in-app + email), ``scheduling_pick_time`` (Candidate Portal),
  ``scheduling_interview_reminder`` / ``scheduling_scorecard_nudge`` (internal interviewers, in-app + email).
* Event subscriptions (after commit -> ``app.tasks.scheduling_agent``): picks / counter-proposals (``SchedulingRunNeeded``),
  ``InterviewScheduled`` / ``Rescheduled`` / ``Cancelled`` / ``Completed``, ``ScorecardSubmitted``,
  ``ApplicationStageChanged`` (leaving Submitted / Interviewing closes open requests), failed runs.
* Settings hub entry, retention bindings (``agent_logs``), audit actions.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

INTERVIEWABLE = ("submitted", "interviewing")


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


def _register_reminder_owner() -> None:
    from app.services.interviews.invites import register_reminder_owner
    from app.services.scheduling_agent.reminders import owns_reminders

    register_reminder_owner(owns_reminders)


# ---------------------------------------------------------------- notifications
async def _owner(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    from app.models.scheduling_agent import SchedulingRequest

    rid = _uuid(payload.get("request_id"))
    req = await db.get(SchedulingRequest, rid) if rid else None
    return {req.owner_user_id} if req is not None else set()


async def _owner_and_interviewers(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    from app.models.scheduling_agent import SchedulingRequest

    rid = _uuid(payload.get("request_id"))
    req = await db.get(SchedulingRequest, rid) if rid else None
    if req is None:
        return set()
    return {req.owner_user_id, *(req.participant_user_ids or [])}


async def _user(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    uid = _uuid(payload.get("user_id"))
    return {uid} if uid else set()


async def _candidate(db: AsyncSession, payload: Any) -> set[Any]:
    from app.services.notifications_service import Recipient

    cid = _uuid(payload.get("candidate_id"))
    return {Recipient("candidate", cid)} if cid else set()


def _renderer(kind: str) -> Any:
    async def render(db: AsyncSession, payload: Any) -> Any:
        from app.models.interviews import Interview
        from app.models.scheduling_agent import SchedulingRequest
        from app.services.notifications_service import Content

        if kind == "pick_time":
            return Content(title="Pick a time for your interview", link_path="/portal/interviews")
        if kind in ("reminder", "nudge"):
            iid = _uuid(payload.get("interview_id"))
            row = await db.get(Interview, iid) if iid else None
            name = row.stage_name if row is not None else "interview"
            title = (
                f"Reminder: {name} interview {row.human_id if row else ''} is coming up"
                if kind == "reminder"
                else f"Scorecard due: {name} interview {row.human_id if row else ''}"
            )
            return Content(
                title=title.strip()[:300],
                entity_type="interview",
                entity_id=iid,
                link_path=f"/interviews/{iid}" if iid else None,
            )
        rid = _uuid(payload.get("request_id"))
        req = await db.get(SchedulingRequest, rid) if rid else None
        human = req.human_id if req is not None else "Scheduling"
        app_id = req.application_id if req is not None else _uuid(payload.get("application_id"))
        titles = {
            "picked": f"{human}: the candidate picked an interview time",
            "booked": f"{human}: the Scheduling Agent booked the interview",
            "rescheduled": f"{human}: the Scheduling Agent rescheduled the interview",
            "handed_off": f"{human}: scheduling needs you",
        }
        return Content(
            title=titles[kind][:300], entity_type="application", entity_id=app_id,
            link_path=f"/applications/{app_id}?tab=interviews" if app_id else None,
        )  # fmt: skip

    return render


def _chooser(payload: Any) -> str | None:
    status = str(payload.get("status"))
    if status == "picked":
        return "interview_slot_picked"
    if status == "booked":
        return (
            "interview_rescheduled_by_agent"
            if payload.get("kind") == "reschedule"
            else "interview_booked_by_agent"
        )
    if status == "handed_off":
        return "scheduling_handed_off"
    return None


def _register_notifications() -> None:
    from app.events.scheduling_agent import SchedulingRequestChanged
    from app.services import notifications_service as ns

    defs: dict[str, Any] = {
        "interview_slot_picked": ns.NotificationType("Candidate picked an interview time", "Interviews", "in_app", False, _owner, _renderer("picked"), internal_only=True),
        "interview_booked_by_agent": ns.NotificationType("Interview booked by the Scheduling Agent", "Interviews", "in_app", False, _owner_and_interviewers, _renderer("booked"), internal_only=True),
        "interview_rescheduled_by_agent": ns.NotificationType("Interview rescheduled by the Scheduling Agent", "Interviews", "in_app", False, _owner, _renderer("rescheduled"), internal_only=True),
        "scheduling_handed_off": ns.NotificationType("Scheduling handed to you", "Interviews", "both", True, _owner, _renderer("handed_off"), internal_only=True),
        "scheduling_interview_reminder": ns.NotificationType("Interview reminder", "Interviews", "both", False, _user, _renderer("reminder"), internal_only=True),
        "scheduling_scorecard_nudge": ns.NotificationType("Scorecard reminder", "Interviews", "both", False, _user, _renderer("nudge"), internal_only=True),
        "scheduling_pick_time": ns.NotificationType(
            "Pick an interview time", "Interviews", "in_app", False, _candidate, _renderer("pick_time"),
            external_default_channel="in_app", portal_path="/portal/interviews", external=True,
        ),
    }  # fmt: skip
    fresh = [k for k in defs if k not in ns.NOTIFICATION_TYPES]
    for key in fresh:
        ns.register_notification_type(key, defs[key])
    if "interview_slot_picked" in fresh:
        ns.register_event_source(SchedulingRequestChanged, _chooser)


# ---------------------------------------------------------------- events -> tasks
def _subscribe() -> None:
    from app.core.events import subscribe
    from app.events.agents import AgentRunFailed
    from app.events.interviews import (
        InterviewCancelled,
        InterviewCompleted,
        InterviewRescheduled,
        InterviewScheduled,
        ScorecardSubmitted,
    )
    from app.events.pipeline import ApplicationStageChanged
    from app.events.scheduling_agent import SchedulingRunNeeded

    def _enqueue(name: str, *args: Any) -> None:
        from app.tasks import scheduling_agent as tasks
        from app.tasks.runtime import enqueue

        enqueue(getattr(tasks, name), *args)

    def run_needed(e: SchedulingRunNeeded) -> None:
        _enqueue(
            "run_needed",
            str(e.tenant_id),
            str(e.request_id),
            e.trigger_key,
            str(e.offer_id) if e.offer_id else None,
        )

    def scheduled(e: Any) -> None:
        _enqueue("interview_changed", str(e.tenant_id), str(e.interview_id), "scheduled")

    def cancelled(e: InterviewCancelled) -> None:
        _enqueue("interview_changed", str(e.tenant_id), str(e.interview_id), "cancelled")

    def completed(e: InterviewCompleted) -> None:
        _enqueue("interview_changed", str(e.tenant_id), str(e.interview_id), f"completed:{e.status}")

    def scorecard(e: ScorecardSubmitted) -> None:
        _enqueue("scorecard_submitted", str(e.tenant_id), str(e.interview_id), str(e.participant_id))

    def stage_changed(e: ApplicationStageChanged) -> None:
        if e.to_key not in INTERVIEWABLE:
            _enqueue("stage_exit", str(e.tenant_id), str(e.application_id))

    def run_failed(e: AgentRunFailed) -> None:
        if e.agent_key == "scheduling":
            _enqueue("run_failed", str(e.tenant_id), str(e.run_id), e.reason_code)

    for cls, fn in (
        (SchedulingRunNeeded, run_needed),
        (InterviewScheduled, scheduled),
        (InterviewRescheduled, scheduled),
        (InterviewCancelled, cancelled),
        (InterviewCompleted, completed),
        (ScorecardSubmitted, scorecard),
        (ApplicationStageChanged, stage_changed),
        (AgentRunFailed, run_failed),
    ):
        fn.__name__ = f"scheduling_agent_on_{cls.__name__}_{fn.__name__}"
        subscribe(cls, fn)


# ---------------------------------------------------------------- settings / retention / audit
def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting", "scheduling_agent", "Scheduling Agent",
        "Slots to offer, look-ahead, buffers, candidate hours, negotiation rounds, link validity, reminders and scorecard nudges.",
        "/api/v1/settings/agents/scheduling", "ai:agent_configure", owner_epic="Epic 59",
    )  # fmt: skip


def _register_retention() -> None:
    from collections.abc import Sequence

    from sqlalchemy import select, update

    from app.models.scheduling_agent import SchedulingRequest, SchedulingSlotOffer
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    def _subject(model: Any) -> Any:
        async def subject(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> uuid.UUID | None:
            return (
                await db.execute(select(model.candidate_id).where(model.id == row_id))
            ).scalar_one_or_none()

        return subject

    def _anchor(model: Any) -> Any:
        async def anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
            return (await db.execute(select(model.updated_at).where(model.id == row_id))).scalar_one_or_none()

        return anchor

    async def expire_offers(
        db: AsyncSession, ids: Sequence[uuid.UUID], action: str = "anonymize"
    ) -> dict[str, int]:
        result = await db.execute(
            update(SchedulingSlotOffer)
            .where(SchedulingSlotOffer.id.in_(list(ids)))
            .values(candidate_note=None, counter_windows=None)
        )
        return {"redacted": int(getattr(result, "rowcount", 0) or 0)}

    async def expire_requests(
        db: AsyncSession, ids: Sequence[uuid.UUID], action: str = "anonymize"
    ) -> dict[str, int]:
        result = await db.execute(
            update(SchedulingRequest)
            .where(SchedulingRequest.id.in_(list(ids)))
            .values(reschedule_reason=None, external_participants=[])
        )
        return {"redacted": int(getattr(result, "rowcount", 0) or 0)}

    bindings = retention_bindings()
    for table, model, fn in (
        ("scheduling_slot_offers", SchedulingSlotOffer, expire_offers),
        ("scheduling_requests", SchedulingRequest, expire_requests),
    ):
        if table not in bindings:
            register_retention_binding(
                table,
                "agent_logs",
                subject_resolver=_subject(model),
                anchor_resolver=_anchor(model),
                expire_fn=fn,
            )


def _register_audit() -> None:
    from app.core import audit

    for key, text in (
        ("scheduling_request.created", "A scheduling request was opened"),
        ("scheduling_request.cancelled", "A scheduling request was cancelled"),
        ("scheduling_request.booked", "The Scheduling Agent booked an interview"),
        ("scheduling_settings.updated", "Scheduling Agent settings were changed"),
        ("scheduling_prefs.updated", "Interviewer scheduling preferences were changed"),
    ):
        audit.register_audit_action(key, text)


for _fn in (
    _register_reminder_owner,
    _register_notifications,
    _subscribe,
    _register_settings,
    _register_retention,
    _register_audit,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("scheduling_agent hooks: {} failed", _fn.__name__)
