# ruff: file-ignore[line-too-long]
"""Registrations of the ``campus`` domain (autodiscovered by ``core.hooks``), Epic 43 A8.

* Semantic source ``campus.registration`` and the campus fields on ``candidates.candidate`` (``source.py``).
* Human-only (star, FR-257) transitions ``campus_registration: shortlisted, eligibility_override``.
* Consent notice ``campus`` (foundation mechanism; lazily seeded per tenant).
* After-commit handlers (tasks on ``maintenance``): ``RegistrationReceived`` / ``EligibilityDefinitionChanged`` ->
  eligibility; ``AssessmentScored`` / ``AssessmentInvitationDecided`` (campus drives); ``OfferAccepted`` /
  ``OfferDeclined`` / ``OfferExpired``; ``PlacementActivated`` / ``PlacementDropped``; ``InterviewCompleted`` /
  ``InterviewStageOutcomeRecorded`` / ``ApplicationStageChanged`` (-> selected) -> ``interviewed``.
* Notification types ``campus_registration_received`` (drive owner digest), ``campus_slot_assigned`` (interviewers,
  in-app), ``queue_turn`` (the Candidate's portal; SMS / WhatsApp go through P2 messaging), domain-local
  ``campus_offers_created`` (drive owner).
* Tenant settings entry, file access (registration answer files), audit action descriptions.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

NOTICE_TITLE = "How we use your details when you register for a campus drive or hiring event"
NOTICE_BODY = """You are registering for a campus recruitment drive or a hiring event.

**What we collect:** your name, email and phone number, and the answers you give in this form (for example your
programme, branch, CGPA or percentage, backlogs and graduation year, plus any document you upload).

**Why:** to check your eligibility for the roles in this drive or event, invite you to assessments and interviews,
contact you about your application and, if you are selected, make you an offer. We create a candidate profile for you,
or update the one we already have.

**Who sees it:** our recruitment team and the interviewers on your panel. Your institution's placement office may see
whether you registered and how far you progressed. We never ask about caste, religion, gender, age, disability or
marital status.

**How long:** we keep your registration as long as your candidate profile, under our candidate retention policy.

**Your rights:** you can see, correct or ask us to erase your data, and withdraw this consent at any time from the
candidate portal or by writing to us. Marketing messages are optional and need a separate tick."""

AUDIT_ACTIONS = (
    ("campus.link_issued", "A campus registration link and QR code were issued"),
    ("campus.link_revoked", "A campus registration link was revoked"),
    ("campus.registration_opened", "Registration opened for a campus drive"),
    ("campus.registration_closed", "Registration closed for a campus drive"),
    ("campus.drive_cancelled", "A campus drive was cancelled"),
    ("campus.registered", "A student registered for a campus drive"),
    ("campus.registration_withdrawn", "A campus registration was withdrawn"),
    ("campus.eligibility_rule_changed", "A campus drive's eligibility rule changed"),
    ("campus.eligibility_overridden", "A campus registration's eligibility was overridden (star)"),
    ("campus.assessments_invited", "Eligible students were invited to the drive assessment"),
    ("campus.shortlist_confirmed", "A campus shortlist was confirmed (star)"),
    ("campus.batch_auto_slotted", "Shortlisted students were auto-slotted into interviews"),
    ("campus.slot_moved", "A campus interview slot was moved"),
    ("campus.batch_cancelled", "A campus interview batch was cancelled"),
    ("campus.bulk_offers", "Campus offers were created in bulk"),
    ("campus.reneged", "A student reneged on a campus offer"),
    ("campus.invites_sent", "Placement-office registration invitations were sent"),
    ("campus.checked_in", "A candidate checked in at a recruitment event"),
    ("campus.event_registered", "A candidate registered for a recruitment event"),
)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- core registrations
def _register_core() -> None:
    from app.services.campus import source
    from app.services.campus.common import CONSENT_PURPOSE, HUMAN_ONLY_ENTITY
    from app.services.privacy.consent import register_default_notice
    from app.services.workflow.catalogue import register_human_only_transitions

    source.register()
    register_human_only_transitions(HUMAN_ONLY_ENTITY, ["shortlisted", "eligibility_override"])
    register_default_notice(CONSENT_PURPOSE, NOTICE_TITLE, NOTICE_BODY)


# ---------------------------------------------------------------- after-commit handlers
def _subscribe() -> None:
    from app.core.events import subscribe
    from app.events.assessments import AssessmentInvitationDecided, AssessmentScored
    from app.events.interviews import InterviewCompleted, InterviewStageOutcomeRecorded
    from app.events.offers import OfferAccepted, OfferDeclined, OfferExpired
    from app.events.pipeline import ApplicationStageChanged
    from app.events.placements import PlacementActivated, PlacementDropped
    from app.services.campus.domain_events import EligibilityDefinitionChanged, RegistrationReceived

    def _q(task_name: str, *args: Any) -> None:
        from app.tasks import campus as tasks
        from app.tasks.runtime import enqueue

        enqueue(getattr(tasks, task_name), *args)

    def on_registered(e: RegistrationReceived) -> None:
        _q("evaluate_eligibility", str(e.tenant_id), str(e.drive_id), True)

    def on_rule(e: EligibilityDefinitionChanged) -> None:
        _q("evaluate_eligibility", str(e.tenant_id), str(e.drive_id), False)

    def on_scored(e: AssessmentScored) -> None:
        _q("assessment_event", str(e.tenant_id), str(e.invitation_id), "scored", e.score)

    def on_decided(e: AssessmentInvitationDecided) -> None:
        if e.context_kind == "campus_drive" and e.outcome in ("expired", "cancelled"):
            _q("assessment_event", str(e.tenant_id), str(e.invitation_id), "decided", e.outcome)

    def on_accepted(e: OfferAccepted) -> None:
        _q("offer_event", str(e.tenant_id), str(e.offer_id), "accepted")

    def on_declined(e: OfferDeclined) -> None:
        _q("offer_event", str(e.tenant_id), str(e.offer_id), "closed")

    def on_expired(e: OfferExpired) -> None:
        _q("offer_event", str(e.tenant_id), str(e.offer_id), "closed")

    def on_activated(e: PlacementActivated) -> None:
        _q("placement_event", str(e.tenant_id), str(e.placement_id), "activated")

    def on_dropped(e: PlacementDropped) -> None:
        _q("placement_event", str(e.tenant_id), str(e.placement_id), "dropped")

    def on_completed(e: InterviewCompleted) -> None:
        if e.status == "completed":
            _q("interviewed", str(e.tenant_id), str(e.application_id))

    def on_outcome(e: InterviewStageOutcomeRecorded) -> None:
        _q("interviewed", str(e.tenant_id), str(e.application_id))

    def on_stage(e: ApplicationStageChanged) -> None:
        if e.to_key == "selected":
            _q("interviewed", str(e.tenant_id), str(e.application_id))

    handlers: tuple[tuple[Any, type], ...] = (
        (on_registered, RegistrationReceived),
        (on_rule, EligibilityDefinitionChanged),
        (on_scored, AssessmentScored),
        (on_decided, AssessmentInvitationDecided),
        (on_accepted, OfferAccepted),
        (on_declined, OfferDeclined),
        (on_expired, OfferExpired),
        (on_activated, PlacementActivated),
        (on_dropped, PlacementDropped),
        (on_completed, InterviewCompleted),
        (on_outcome, InterviewStageOutcomeRecorded),
        (on_stage, ApplicationStageChanged),
    )
    for fn, cls in handlers:
        fn.__name__ = f"campus_{fn.__name__}"
        subscribe(cls, fn)


# ---------------------------------------------------------------- notifications
async def _owner(db: AsyncSession, payload: Any) -> set[Any]:  # ruff: ignore[unused-function-argument]
    oid = _uuid(payload.get("owner_id"))
    return {oid} if oid else set()


async def _interviewers(db: AsyncSession, payload: Any) -> set[Any]:  # ruff: ignore[unused-function-argument]
    return {u for u in (_uuid(x) for x in payload.get("interviewer_user_ids") or ()) if u}


async def _candidate(db: AsyncSession, payload: Any) -> set[Any]:  # ruff: ignore[unused-function-argument]
    from app.services.notifications_service import Recipient

    cid = _uuid(payload.get("candidate_id"))
    return {Recipient("candidate", cid)} if cid else set()


async def _drive_name(db: AsyncSession, payload: Any) -> str:
    from app.models.campus import CampusDrive

    did = _uuid(payload.get("drive_id"))
    d = await db.get(CampusDrive, did) if did else None
    return d.name if d is not None else "your campus drive"


async def _render_digest(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    did = payload.get("drive_id")
    return Content(
        title=f"{payload.get('count', 0)} new registrations for {await _drive_name(db, payload)}"[:300],
        entity_type="campus_drive",
        entity_id=_uuid(did),
        link_path=f"/campus/drives/{did}",
    )


async def _render_slot(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
    from app.services.notifications_service import Content

    iid = payload.get("interview_id")
    return Content(
        title="A campus interview was added to your calendar",
        body="A shortlisted student was slotted into your interview panel.",
        link_path=f"/interviews?interview={iid}" if iid else "/interviews",
    )


async def _render_turn(db: AsyncSession, payload: Any) -> Any:  # ruff: ignore[unused-function-argument]
    from app.services.notifications_service import Content

    return Content(title="It is your turn: please go to your interview station", link_path="/portal")


async def _render_offers(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    did = payload.get("drive_id")
    return Content(
        title=f"{payload.get('count', 0)} campus offers created for {await _drive_name(db, payload)}"[:300],
        body="Each offer follows its own approval and release.",
        entity_type="campus_drive",
        entity_id=_uuid(did),
        link_path=f"/campus/drives/{did}",
    )


def _register_notifications() -> None:
    from app.services import notifications_service as ns
    from app.services.campus.domain_events import (
        CampusOffersCreated,
        CampusRegistrationsDigest,
        CampusSlotAssigned,
        QueueTurn,
    )

    defs: dict[str, tuple[Any, type]] = {
        "campus_registration_received": (
            ns.NotificationType("Campus registrations received (daily)", "Campus", "in_app", False, _owner, _render_digest, internal_only=True),
            CampusRegistrationsDigest,
        ),
        "campus_slot_assigned": (
            ns.NotificationType("Campus interview slot assigned", "Campus", "in_app", False, _interviewers, _render_slot, internal_only=True),
            CampusSlotAssigned,
        ),
        "queue_turn": (
            ns.NotificationType(
                "Your interview turn", "Campus", "in_app", False, _candidate, _render_turn,
                external_default_channel="in_app", portal_path="/portal/interviews", external=True,
            ),
            QueueTurn,
        ),
        "campus_offers_created": (
            ns.NotificationType("Campus offers created", "Campus", "both", False, _owner, _render_offers, internal_only=True),
            CampusOffersCreated,
        ),
    }
    for key, (definition, cls) in defs.items():
        if key in ns.NOTIFICATION_TYPES:
            continue
        ns.register_notification_type(key, definition)
        ns.register_event_source(cls, key)


# ---------------------------------------------------------------- settings / files / audit
def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting",
        "campus",
        "Campus & events",
        "Registration rate limits, default interview length and the QR poster footer.",
        "/api/v1/campus/settings",
        "campus:manage",
        owner_epic="Epic 43",
    )


async def _file_access(ctx: Any, db: AsyncSession, entity_id: uuid.UUID, write: bool) -> bool:
    from app.services.campus.registration import registration_for

    principal = ctx.principal
    if write or getattr(principal, "type", None) != "user" or not principal.has("campus:read"):
        return False
    try:
        await registration_for(ctx, db, entity_id)
    except Exception:
        return False
    return True


def _register_access() -> None:
    from app.services import files_service

    files_service.register_access_checker("campus_registration", _file_access, plural="campus-registrations")


def _register_audit() -> None:
    from app.core import audit

    for key, desc in AUDIT_ACTIONS:
        audit.register_audit_action(key, desc)


for _fn in (_register_core, _subscribe, _register_notifications, _register_settings, _register_access, _register_audit):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block start-up on an optional registry
        logger.exception("campus: {} failed", _fn.__name__)
