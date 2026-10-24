# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Registrations of the ``referrals`` domain (autodiscovered by ``core.hooks``), Epic 42 A7.

* Consent notice ``referral`` (English default; lazily seeded per tenant by the foundation mechanism).
* Approval subject ``referral_reward`` (permission ``referral_reward:approve``) and the human-only (*) transition
  ``referral_reward: approved`` (FR-257).
* Posting source ``referral_reward`` (ledger ``payable``; AD-23).
* After-commit handlers (tasks on ``maintenance``): ``ApplicationStageChanged``, ``PlacementCreated`` /
  ``Activated`` / ``Ended`` / ``Dropped`` and ``PayrollExportFinalized``.
* Notification types: ``referral_status_changed`` (foundation 0.7 key; the referrer, referrer-safe label only),
  ``referral_received`` / ``referral_duplicate`` (recruiters on the Demand), ``referral_reward_eligible``
  (reward approvers), ``referral_reward_paid`` (the referrer).
* Workflow trigger ``referrals.status_changed`` (entity ``referral``; no action is registered) and its snapshot
  loader; semantic sources ``referrals.referral`` / ``referrals.reward``; the settings entry; file access for
  owner kind ``referral`` (resumes); audit action descriptions.

The ``referral_link`` public page is one branch of ``public_links_service`` (shared file, additive).
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext

NOTICE_TITLE = "How we use your details when someone refers you"
NOTICE_BODY = """Someone you know referred you to us for a job opening. Before we do anything with the details they
gave us, we need your agreement.

**What we do if you accept:** we create your candidate profile from your name, email, phone number and resume,
consider you for the opening you were referred for (or for suitable openings, for a general referral), and contact
you about it. If we already know you, we update your existing profile instead.

**What the person who referred you sees:** only that the referral was received, is in process, led to a hire or was
not taken forward. They never see interview feedback, reasons or any offer details.

**If you decline:** we delete the contact details and resume we received about you, and we do not contact you
again about this referral.

**Your rights:** you can see, correct or ask us to erase your data, and withdraw this consent at any time from the
candidate portal or by writing to us."""

AUDIT_ACTIONS = (
    ("referral.program_created", "A referral program was created"),
    ("referral.submitted", "An employee or worker referred a candidate"),
    ("referral.accepted", "A referred person accepted the referral notice"),
    ("referral.declined", "A referred person declined the referral"),
    ("referral.duplicate", "A referral matched an active candidate (duplicate, no reward)"),
    ("referral.reward_created", "A referral reward was created on a hire"),
    ("referral.reward_approved", "A referral reward was approved"),
    ("referral.reward_voided", "A referral reward was voided"),
    ("referral.reward_void_reason", "The reason given for voiding a referral reward"),
    ("referral.reward_paid", "A referral reward was marked paid"),
    ("referral.payout_exported", "The referral payout export was downloaded"),
)


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- notice / human-only / approvals / postings
def _register_core() -> None:
    from app.services.approvals import register_approval_subject
    from app.services.privacy.consent import register_default_notice
    from app.services.referrals import posting_source, rewards
    from app.services.referrals.common import APPROVAL_SUBJECT, REWARD_APPROVE
    from app.services.workflow.catalogue import register_human_only_transitions

    register_default_notice("referral", NOTICE_TITLE, NOTICE_BODY)
    register_human_only_transitions(APPROVAL_SUBJECT, ["approved"])
    register_approval_subject(
        APPROVAL_SUBJECT,
        on_final_decided=rewards.on_final_decided,
        describe=rewards.describe,
        permission=REWARD_APPROVE,
    )
    posting_source.register()


# ---------------------------------------------------------------- after-commit handlers
def _subscribe() -> None:
    from app.core.events import subscribe
    from app.events.payroll import PayrollExportFinalized
    from app.events.pipeline import ApplicationStageChanged
    from app.events.placements import PlacementActivated, PlacementCreated, PlacementDropped, PlacementEnded

    def on_stage(e: ApplicationStageChanged) -> None:
        from app.tasks import referrals as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.stage_changed, str(e.tenant_id), str(e.application_id), e.to_key)

    def on_created(e: PlacementCreated) -> None:
        from app.tasks import referrals as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.placement_event, str(e.tenant_id), str(e.placement_id), "created", None)

    def on_activated(e: PlacementActivated) -> None:
        from app.tasks import referrals as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.placement_event, str(e.tenant_id), str(e.placement_id), "activated", None)

    def on_ended(e: PlacementEnded) -> None:
        from app.tasks import referrals as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.placement_event, str(e.tenant_id), str(e.placement_id), "ended", e.end_date.isoformat())

    def on_dropped(e: PlacementDropped) -> None:
        from app.tasks import referrals as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.placement_event, str(e.tenant_id), str(e.placement_id), "dropped", None)

    def on_finalized(e: PayrollExportFinalized) -> None:
        from app.tasks import referrals as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.payroll_finalized, str(e.tenant_id), str(e.export_version_id))

    handlers: tuple[tuple[Any, type], ...] = (
        (on_stage, ApplicationStageChanged),
        (on_created, PlacementCreated),
        (on_activated, PlacementActivated),
        (on_ended, PlacementEnded),
        (on_dropped, PlacementDropped),
        (on_finalized, PayrollExportFinalized),
    )
    for fn, cls in handlers:
        fn.__name__ = f"referrals_{fn.__name__}"
        subscribe(cls, fn)


# ---------------------------------------------------------------- notifications
async def _referral(db: AsyncSession, payload: Any) -> Any:
    from app.models.referrals import Referral

    rid = _uuid(payload.get("referral_id"))
    return await db.get(Referral, rid) if rid else None


def _referrer_recipient(kind: str | None, rid: Any) -> set[Any]:
    from app.services.notifications_service import Recipient

    ident = _uuid(rid)
    if ident is None:
        return set()
    return {ident} if kind == "user" else {Recipient("candidate", ident)}


async def _referrer_recipients(db: AsyncSession, payload: Any) -> set[Any]:  # ruff: ignore[unused-function-argument]  # NOSONAR
    return _referrer_recipient(payload.get("referrer_kind"), payload.get("referrer_id"))


async def _recruiters(db: AsyncSession, payload: Any) -> set[Any]:
    """The Demand owner and its active user assignees; general referrals go to the default owner / admins."""
    from app.models.demands import Demand, DemandAssignment
    from app.services.referrals.common import get_settings_row

    did = _uuid(payload.get("demand_id"))
    out: set[Any] = set()
    if did is not None:
        d = await db.get(Demand, did)
        if d is not None:
            out.add(d.owner_id)
        out |= {
            u
            for u in (
                await db.execute(
                    select(DemandAssignment.user_id).where(
                        DemandAssignment.demand_id == did, DemandAssignment.removed_at.is_(None)
                    )
                )
            ).scalars()
            if u
        }
    if not out:
        settings = await get_settings_row(db)
        if settings.default_owner_user_id:
            out.add(settings.default_owner_user_id)
    return out


async def _approvers(db: AsyncSession, payload: Any) -> set[Any]:  # ruff: ignore[unused-function-argument]
    from app.services import identity_bridge

    return {u["id"] for u in await identity_bridge.users_with_permission(db, "referral_reward:approve")}


async def _render_status(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    r = await _referral(db, payload)
    ref = r.human_id if r is not None else "Your referral"
    return Content(
        title=f"{ref}: {payload.get('status_label') or 'updated'}"[:300],
        body="Thank you for referring. We will let you know when the status changes.",
        link_path="/refer",
    )


async def _render_received(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    r = await _referral(db, payload)
    return Content(
        title=f"New referral {r.human_id if r else ''}".strip()[:300],
        entity_type="referral" if r else None,
        entity_id=r.id if r else None,
        link_path=f"/referrals?referral={r.id}" if r else "/referrals",
    )


async def _render_duplicate(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    r = await _referral(db, payload)
    return Content(
        title=f"Referral {r.human_id if r else ''} matches an active candidate".replace("  ", " ")[:300],
        body="No application was created and the referral earns no reward.",
        entity_type="referral" if r else None,
        entity_id=r.id if r else None,
        link_path=f"/referrals?referral={r.id}" if r else "/referrals",
    )


async def _render_eligible(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    r = await _referral(db, payload)
    req = payload.get("approval_request_id")
    return Content(
        title=f"Referral reward {r.human_id if r else ''} is ready for approval".replace("  ", " ")[:300],
        link_path=f"/approvals/{req}" if req else "/referrals/rewards",
    )


async def _render_paid(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    r = await _referral(db, payload)
    return Content(
        title=f"Your referral reward for {r.human_id if r else 'a referral'} was paid"[:300],
        link_path="/refer",
    )


def _register_notifications() -> None:
    from app.services import notifications_service as ns
    from app.services.referrals.events import (
        ReferralDuplicateFound,
        ReferralReceived,
        ReferralRewardEligible,
        ReferralRewardPaid,
        ReferralStatusChanged,
    )

    defs: dict[str, tuple[Any, type]] = {
        "referral_status_changed": (
            ns.NotificationType(
                "Referral status changed",
                "Referrals",
                "in_app",
                False,
                _referrer_recipients,
                _render_status,
                portal_path="/portal/work/referrals",
                external=True,
            ),
            ReferralStatusChanged,
        ),
        "referral_received": (
            ns.NotificationType(
                "Referral received",
                "Referrals",
                "both",
                False,
                _recruiters,
                _render_received,
                internal_only=True,
            ),
            ReferralReceived,
        ),
        "referral_duplicate": (
            ns.NotificationType(
                "Referral matched an active candidate",
                "Referrals",
                "in_app",
                False,
                _recruiters,
                _render_duplicate,
                internal_only=True,
            ),
            ReferralDuplicateFound,
        ),
        "referral_reward_eligible": (
            ns.NotificationType(
                "Referral reward ready for approval",
                "Referrals",
                "both",
                False,
                _approvers,
                _render_eligible,
                internal_only=True,
            ),
            ReferralRewardEligible,
        ),
        "referral_reward_paid": (
            ns.NotificationType(
                "Referral reward paid",
                "Referrals",
                "both",
                False,
                _referrer_recipients,
                _render_paid,
                portal_path="/portal/work/referrals",
                external=True,
            ),
            ReferralRewardPaid,
        ),
    }
    for key, (definition, cls) in defs.items():
        if key in ns.NOTIFICATION_TYPES:
            continue
        ns.register_notification_type(key, definition)
        ns.register_event_source(cls, key)


# ---------------------------------------------------------------- workflow trigger + snapshot
async def _snapshot(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID) -> dict[str, Any] | None:  # ruff: ignore[unused-function-argument]
    from app.models.referrals import Referral

    r = await db.get(Referral, entity_id)
    if r is None:
        return None
    return {
        "status": r.status,
        "channel": r.channel,
        "referrer_kind": r.referrer_kind,
        "demand_id": str(r.demand_id) if r.demand_id else None,
        "reward_eligible": r.reward_eligible,
    }


def _register_workflow() -> None:
    from app.models.referrals import CHANNELS, REFERRAL_STATUSES, REFERRER_KINDS
    from app.services.workflow import catalogue
    from app.services.workflow.conditions import FieldDef
    from app.services.workflow.registry import TRIGGERS, Trigger, register_trigger

    if catalogue.snapshot_loader("referral") is None:
        catalogue.register_snapshot_loader(
            "referral",
            _snapshot,
            {
                "status": FieldDef("enum", "Status", REFERRAL_STATUSES),
                "channel": FieldDef("enum", "Channel", CHANNELS),
                "referrer_kind": FieldDef("enum", "Referrer type", REFERRER_KINDS),
                "demand_id": FieldDef("uuid", "Demand"),
                "reward_eligible": FieldDef("bool", "Reward eligible"),
            },
        )
    key = "referrals.status_changed"
    if key not in TRIGGERS:
        register_trigger(
            key,
            Trigger(
                key,
                "Referral status changed",
                "app.services.referrals.events",
                "ReferralStatusChanged",
                "referral",
                "referral_id",
                extras={"to_status": ("to_status", FieldDef("enum", "New status", REFERRAL_STATUSES))},
            ),
        )


# ---------------------------------------------------------------- semantic / settings / files / audit
def _register_semantic() -> None:
    from app.services.referrals import semantic_source

    semantic_source.register()


def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting",
        "referrals",
        "Employee referrals",
        "Referral programs, rewards, duplicate window and referral link lifetimes.",
        "/api/v1/referral-settings",
        "referral:configure",
        owner_epic="Epic 42",
    )


async def _resume_access(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID, write: bool) -> bool:
    from app.services.referrals import tracking

    principal = ctx.principal
    if write or getattr(principal, "type", None) != "user" or not principal.has("referral:read"):
        return False
    try:
        await tracking.visible_referral(ctx, db, entity_id)
    except Exception:
        return False
    return True


def _register_access() -> None:
    from app.services import files_service

    files_service.register_access_checker("referral", _resume_access, plural="referrals")


def _register_audit() -> None:
    from app.core import audit

    for key, desc in AUDIT_ACTIONS:
        audit.register_audit_action(key, desc)


for _fn in (
    _register_core,
    _subscribe,
    _register_notifications,
    _register_workflow,
    _register_semantic,
    _register_settings,
    _register_access,
    _register_audit,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block start-up on an optional registry
        logger.exception("referrals: {} failed", _fn.__name__)
