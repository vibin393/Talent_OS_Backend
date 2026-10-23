# ruff: file-ignore[line-too-long]
"""Registrations of the ``assessments`` domain (autodiscovered by ``core.hooks``), Epic 40 A11.

* Consent notice ``assessment`` (lists the logged proctoring signals; lazily seeded per tenant by the foundation).
* Human-only (star) transitions ``assessment_invitation: passed, failed`` (FR-257).
* Notification types ``assessment_invited`` / ``assessment_reminder`` (Candidate, portal), ``assessment_completed`` /
  ``assessment_expired`` (inviter), ``assessment_review_needed`` (inviter).
* Workflow: action ``assessments.invite``; triggers ``assessments.invitation_status_changed`` and
  ``assessments.scored`` with the ``assessment_invitation`` snapshot loader.
* Connector webhook handler (category ``assessment``); after-commit provider invite task.
* Semantic source ``assessments.result``; settings hub entry; interviews stage-plan validator / scheduling guard;
  P1 submission attachment provider (shared results); file access for ``assessment_invitation`` (report uploads).
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext

NOTICE_TITLE = "How we use your online assessment"
NOTICE_BODY = """We use this online assessment **only to evaluate your skills for the role or programme you were invited for**.

**What we record:** your answers, the time you started and submitted, and these test events: switching tabs or
windows, the test window losing focus, copy and paste, leaving full-screen mode, opening the test in a second window,
and a change of network address. We do **not** use your webcam, microphone or screen. These events are shown to the
reviewer as context only; they never change your score.

**How it is scored:** objective questions are scored automatically by comparing answers. Coding questions are
**never run on our servers** -- they are compared with expected answers or reviewed by a person. Free-text answers
are reviewed by a person; AI may suggest a mark, but a person decides.

**Who sees the results:** our recruiters for this role and, only if a recruiter chooses to include them, the hiring
client (a one-page summary of scores, never your answers or test events).

**Your rights:** you can see, correct or ask us to erase your data, and withdraw this consent, from your privacy
settings. Your answers are deleted with your candidate data; an anonymous score may be kept for statistics."""


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- notice / human-only
def _register_notice() -> None:
    from app.services.privacy.consent import register_default_notice

    register_default_notice("assessment", NOTICE_TITLE, NOTICE_BODY)


def _register_human_only() -> None:
    from app.services.workflow.catalogue import register_human_only_transitions

    register_human_only_transitions("assessment_invitation", ["passed", "failed"])


# ---------------------------------------------------------------- notifications
async def _invitation(db: AsyncSession, payload: Any) -> Any:
    from app.models.assessments import AssessmentInvitation

    iid = _uuid(payload.get("invitation_id"))
    return await db.get(AssessmentInvitation, iid) if iid else None


async def _name(db: AsyncSession, inv: Any) -> str:
    from app.models.assessments import Assessment

    a = await db.get(Assessment, inv.assessment_id) if inv is not None else None
    return a.name if a is not None else "Assessment"


async def _candidate_recipients(db: AsyncSession, payload: Any) -> set[Any]:  # ruff: ignore[unused-function-argument]
    from app.services.notifications_service import Recipient

    cid = _uuid(payload.get("candidate_id"))
    return {Recipient("candidate", cid)} if cid else set()


async def _inviter_recipients(db: AsyncSession, payload: Any) -> set[Any]:
    inv = await _invitation(db, payload)
    if inv is None or inv.invited_by_kind != "user" or inv.invited_by_id is None:
        return set()
    return {inv.invited_by_id}


def _renderer(kind: str) -> Any:
    async def render(db: AsyncSession, payload: Any) -> Any:
        from app.services.notifications_service import Content

        inv = await _invitation(db, payload)
        name = await _name(db, inv)
        iid = inv.id if inv is not None else None
        titles = {
            "invited": f"You are invited to the assessment {name}",
            "reminder": f"Reminder: complete the assessment {name}",
            "completed": f"A candidate completed {name}",
            "expired": f"An invitation to {name} expired",
            "review": f"{name}: answers wait for your review",
        }
        candidate_facing = kind in ("invited", "reminder")
        return Content(
            title=titles[kind][:300],
            entity_type=None if candidate_facing else "assessment_invitation",
            entity_id=None if candidate_facing else iid,
            link_path=(
                f"/portal/assessments/{iid}"
                if candidate_facing
                else "/assessments/reviews"
                if kind == "review"
                else f"/assessments?invitation={iid}"
            )
            if iid
            else None,
        )

    return render


def _status_chooser(payload: Any) -> str | None:
    return {
        "invited": "assessment_invited",
        "completed": "assessment_completed",
        "expired": "assessment_expired",
    }.get(str(payload.get("to_status")))


def _register_notifications() -> None:
    from app.services import notifications_service as ns
    from app.services.assessments.events import (
        AssessmentInvitationStatusChanged,
        AssessmentReminderDue,
        AssessmentReviewNeeded,
    )

    defs: dict[str, Any] = {
        "assessment_invited": ns.NotificationType(
            "Assessment invitation", "Assessments", "both", False, _candidate_recipients, _renderer("invited"),
            external_default_channel="in_app", portal_path="/portal/assessments/{invitation_id}", external=True,
        ),
        "assessment_reminder": ns.NotificationType(
            "Assessment reminder", "Assessments", "both", False, _candidate_recipients, _renderer("reminder"),
            external_default_channel="in_app", portal_path="/portal/assessments/{invitation_id}", external=True,
        ),
        "assessment_completed": ns.NotificationType("Assessment completed", "Assessments", "in_app", False, _inviter_recipients, _renderer("completed")),
        "assessment_expired": ns.NotificationType("Assessment invitation expired", "Assessments", "in_app", False, _inviter_recipients, _renderer("expired")),
        "assessment_review_needed": ns.NotificationType("Assessment answers to review", "Assessments", "both", False, _inviter_recipients, _renderer("review")),
    }  # fmt: skip
    fresh = [k for k in defs if k not in ns.NOTIFICATION_TYPES]
    for key in fresh:
        ns.register_notification_type(key, defs[key])
    if "assessment_invited" in fresh:
        ns.register_event_source(AssessmentInvitationStatusChanged, _status_chooser)
        ns.register_event_source(AssessmentReminderDue, "assessment_reminder")
        ns.register_event_source(AssessmentReviewNeeded, "assessment_review_needed")


# ---------------------------------------------------------------- workflow
async def _snapshot(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID) -> Any:  # ruff: ignore[unused-function-argument]
    from app.models.assessments import AssessmentInvitation

    inv = await db.get(AssessmentInvitation, entity_id)
    if inv is None or inv.candidate_id is None:
        return None
    return {
        "id": str(inv.id), "assessment_id": str(inv.assessment_id), "candidate_id": str(inv.candidate_id),
        "application_id": str(inv.application_id) if inv.application_id else None, "status": inv.status,
        "result": inv.result, "gating": inv.gating, "context_kind": inv.context_kind,
        "score": str(inv.score) if inv.score is not None else None,
    }  # fmt: skip


def _register_workflow() -> None:
    from app.models.assessments import INVITATION_STATUSES
    from app.services.assessments import workflow_actions
    from app.services.workflow import catalogue
    from app.services.workflow.conditions import FieldDef
    from app.services.workflow.registry import TRIGGERS, Trigger, register_trigger

    workflow_actions.register()
    if catalogue.snapshot_loader("assessment_invitation") is None:
        catalogue.register_snapshot_loader(
            "assessment_invitation",
            _snapshot,
            {
                "status": FieldDef("enum", "Status", INVITATION_STATUSES),
                "result": FieldDef("enum", "Result", ("pass", "fail")),
                "gating": FieldDef("bool", "Gating"),
                "context_kind": FieldDef(
                    "enum", "Context", ("application", "campus_drive", "event", "direct")
                ),
                "score": FieldDef("decimal", "Score (%)"),
            },
        )
    module = "app.services.assessments.events"
    if "assessments.invitation_status_changed" not in TRIGGERS:
        register_trigger(
            "assessments.invitation_status_changed",
            Trigger(
                "assessments.invitation_status_changed", "Assessment invitation status changed", module,
                "AssessmentInvitationStatusChanged", "assessment_invitation", "invitation_id",
                {
                    "from_status": ("from_status", FieldDef("str", "From status")),
                    "to_status": ("to_status", FieldDef("enum", "To status", INVITATION_STATUSES)),
                },
            ),
        )  # fmt: skip
    if "assessments.scored" not in TRIGGERS:
        register_trigger(
            "assessments.scored",
            Trigger(
                "assessments.scored", "Assessment scored", module, "AssessmentResultReady", "assessment_invitation", "invitation_id",
                {"source": ("source", FieldDef("enum", "Source", ("builtin", "provider")))},
            ),
        )  # fmt: skip


# ---------------------------------------------------------------- connectors / tasks
def _register_connector() -> None:
    from app.services.assessments import provider
    from app.services.connectors.registry import register_webhook_handler, webhook_handler

    if webhook_handler("assessment") is None:
        register_webhook_handler("assessment", provider.on_webhook)


def _subscribe() -> None:
    from app.core.events import subscribe
    from app.services.assessments.events import AssessmentProviderInviteQueued

    def on_queued(e: AssessmentProviderInviteQueued) -> None:
        from app.tasks import assessments as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.provider_invite, str(e.tenant_id), str(e.job_id))

    on_queued.__name__ = "assessments_on_provider_invite_queued"
    subscribe(AssessmentProviderInviteQueued, on_queued)


# ---------------------------------------------------------------- semantic / settings / P1 hooks / files
def _register_semantic() -> None:
    from app.services.assessments import semantic_source

    semantic_source.register()


def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting",
        "assessments",
        "Assessments",
        "Auto-pass, review confidence, reminders, deadlines, link grace and proctoring signal limits.",
        "/api/v1/assessment-settings",
        "assessment:configure",
        owner_epic="Epic 40",
    )


def _register_p1_hooks() -> None:
    from app.services.assessments import shares, stage_type
    from app.services.pipeline import submissions

    stage_type.register()
    submissions.register_submission_attachment_provider(shares.submission_attachments)


async def _invitation_access(
    ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID, write: bool
) -> bool:
    from app.models.assessments import AssessmentInvitation

    principal = ctx.principal
    if getattr(principal, "type", None) != "user":
        return False
    if not principal.has("assessment:review" if write else "assessment:read"):
        return False
    return await db.get(AssessmentInvitation, entity_id) is not None


def _register_files() -> None:
    from app.services import files_service

    files_service.register_access_checker(
        "assessment_invitation", _invitation_access, plural="assessment-invitations"
    )


for _fn in (
    _register_notice,
    _register_human_only,
    _register_notifications,
    _register_workflow,
    _register_connector,
    _subscribe,
    _register_semantic,
    _register_settings,
    _register_p1_hooks,
    _register_files,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block start-up on an optional registry
        logger.exception("assessments: {} failed", _fn.__name__)
