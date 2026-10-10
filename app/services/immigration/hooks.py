# ruff: noqa: E501
"""Registrations of the ``immigration`` domain (autodiscovered by ``core.hooks``), Epic 46 A6.

* Consent notice ``immigration`` (English default; lazily seeded per tenant by the foundation mechanism).
* Human-only (*) transitions ``immigration_case: approved, refused`` (FR-257).
* Notification types: ``immigration_case_step`` (foundation 0.7; local event ``ImmigrationCaseStep``) and the two
  domain-local recipient types ``immigration_permit_expiring`` / ``immigration_permit_expired`` on the P2
  ``CredentialExpiring`` / ``CredentialExpired`` events (only for ``permit.*`` credential types): they add the
  owning Case's Coordinator and the Account Managers of the Worker's open Placements. Expiry alerts themselves stay
  P2's (no second alert engine).
* P2 ``CredentialRecorded`` of a ``permit.*`` type -> back-link ``permits.credential_id`` (task on ``maintenance``).
* File / activity access for owner kind ``immigration_case`` (documents of a Case readable with
  ``immigration:read``), audit action descriptions, settings entry, retention binding (class ``immigration``).
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext

NOTICE_TITLE = "How we use your immigration and travel documents"
NOTICE_BODY = """We collect your passport, visa, education, employment, medical and other documents listed on your
immigration checklist, and the details of family members travelling with you, **only to prepare and track your
visa or work-permit application** for the assignment we are placing you in.

**Who sees them:** our immigration coordinators and HR team, the immigration counsel handling your case, the
sponsoring employer and, where the application requires it, the embassy, consulate or immigration authority of the
destination country. We file nothing with any government ourselves; your counsel or you do.

**How long we keep them:** for 3 years after your case closes or your last permit expires, whichever is later,
unless the law requires longer.

**Your rights:** you can see, correct or ask us to erase your data, and withdraw this consent at any time from your
privacy settings. Withdrawing consent stops new uploads; documents already filed with an authority cannot be
recalled by us."""


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- consent notice / human-only
def _register_notice() -> None:
    from app.services.privacy.consent import register_default_notice

    register_default_notice("immigration", NOTICE_TITLE, NOTICE_BODY)


def _register_human_only() -> None:
    from app.services.workflow.catalogue import register_human_only_transitions

    register_human_only_transitions("immigration_case", ["approved", "refused"])


# ---------------------------------------------------------------- notifications
async def _case(db: AsyncSession, payload: Any) -> Any:
    from app.models.immigration import ImmigrationCase

    cid = _uuid(payload.get("case_id"))
    return await db.get(ImmigrationCase, cid) if cid else None


async def _account_managers(
    db: AsyncSession, candidate_id: uuid.UUID | None, placement_id: uuid.UUID | None = None
) -> set[uuid.UUID]:
    from app.models.placements import Placement
    from app.services.immigration.common import OPEN_PLACEMENT_STATUSES

    if candidate_id is None and placement_id is None:
        return set()
    stmt = select(Placement.account_manager_user_id)
    if placement_id is not None:
        stmt = stmt.where(Placement.id == placement_id)
    else:
        stmt = stmt.where(
            Placement.candidate_id == candidate_id, Placement.status.in_(OPEN_PLACEMENT_STATUSES)
        )
    return {r for r in (await db.execute(stmt)).scalars() if r}


async def _step_recipients(db: AsyncSession, payload: Any) -> set[Any]:
    from app.services.notifications_service import Recipient

    case = await _case(db, payload)
    if case is None:
        return set()
    out: set[Any] = {case.owner_user_id}
    out |= await _account_managers(db, case.candidate_id, case.placement_id) if case.placement_id else set()
    step = payload.get("step")
    if step == "documents_requested" or (step == "outcome_recorded" and payload.get("outcome") == "approved"):
        out.add(Recipient("candidate", case.candidate_id))
    return out


async def _render_step(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    case = await _case(db, payload)
    ref = case.human_id if case is not None else "Immigration case"
    step = payload.get("step")
    title = {
        "stage_changed": f"{ref} moved to a new stage",
        "overdue": f"{ref} is overdue",
        "documents_requested": f"Documents requested for {ref}",
        "outcome_recorded": f"{ref}: {str(payload.get('outcome') or 'outcome').replace('_', ' ')}",
    }.get(str(step), f"{ref} updated")
    cid = case.id if case is not None else None
    return Content(
        title=title[:300],
        entity_type="immigration_case" if cid else None,
        entity_id=cid,
        link_path=f"/immigration/cases/{cid}" if cid else None,
    )


async def _expiry_recipients(db: AsyncSession, payload: Any) -> set[Any]:
    from app.models.immigration import ImmigrationCase, VisaType

    cand = _uuid(payload.get("candidate_id"))
    type_key = str(payload.get("type_key") or "")
    if cand is None or not type_key.startswith("permit."):
        return set()
    owners = set(
        (
            await db.execute(
                select(ImmigrationCase.owner_user_id)
                .join(VisaType, VisaType.id == ImmigrationCase.visa_type_id)
                .where(ImmigrationCase.candidate_id == cand, VisaType.key == type_key.removeprefix("permit."))
            )
        ).scalars()
    )
    return owners | await _account_managers(db, cand)


async def _render_expiring(db: AsyncSession, payload: Any) -> Any:
    from app.models.compliance import CredentialType
    from app.services.notifications_service import Content

    label = (
        await db.execute(
            select(CredentialType.label).where(CredentialType.key == str(payload.get("type_key")))
        )
    ).scalar_one_or_none() or "Permit"
    return Content(
        title=f"{label} expires on {payload.get('expires_on')} ({payload.get('offset_days')} days)"[:300],
        body="Start the renewal with the worker and counsel.",
        link_path="/immigration",
    )


async def _render_expired(db: AsyncSession, payload: Any) -> Any:
    from app.models.compliance import CredentialType
    from app.services.notifications_service import Content

    label = (
        await db.execute(
            select(CredentialType.label).where(CredentialType.key == str(payload.get("type_key")))
        )
    ).scalar_one_or_none() or "Permit"
    return Content(title=f"{label} expired on {payload.get('expires_on')}"[:300], link_path="/immigration")


def _permit_chooser(key: str) -> Any:
    def choose(payload: Any) -> str | None:
        return key if str(payload.get("type_key") or "").startswith("permit.") else None

    return choose


def _register_notifications() -> None:
    from app.services import notifications_service as ns
    from app.services.compliance.common import CredentialExpired, CredentialExpiring
    from app.services.immigration.events import ImmigrationCaseStep

    defs: dict[str, tuple[Any, type, Any]] = {
        "immigration_case_step": (
            ns.NotificationType(
                "Immigration case step",
                "Immigration",
                "both",
                False,
                _step_recipients,
                _render_step,
                portal_path="/portal/work/immigration/{case_id}",
                external=True,
            ),
            ImmigrationCaseStep,
            "immigration_case_step",
        ),
        "immigration_permit_expiring": (
            ns.NotificationType(
                "Permit expiring (immigration)",
                "Immigration",
                "both",
                False,
                _expiry_recipients,
                _render_expiring,
            ),
            CredentialExpiring,
            _permit_chooser("immigration_permit_expiring"),
        ),
        "immigration_permit_expired": (
            ns.NotificationType(
                "Permit expired (immigration)",
                "Immigration",
                "both",
                True,
                _expiry_recipients,
                _render_expired,
            ),
            CredentialExpired,
            _permit_chooser("immigration_permit_expired"),
        ),
    }
    for key, (definition, cls, selector) in defs.items():
        if key in ns.NOTIFICATION_TYPES:
            continue
        ns.register_notification_type(key, definition)
        ns.register_event_source(cls, selector)


# ---------------------------------------------------------------- P2 events
def _subscribe() -> None:
    from app.core.events import subscribe
    from app.events.compliance import CredentialRecorded

    def on_credential(e: CredentialRecorded) -> None:
        if not e.type_key.startswith("permit."):
            return
        from app.tasks import immigration as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.link_credential, str(e.tenant_id), str(e.credential_id))

    on_credential.__name__ = "immigration_on_credential_recorded"
    subscribe(CredentialRecorded, on_credential)


# ---------------------------------------------------------------- access / audit / settings / retention
async def _case_access(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID, write: bool) -> bool:
    from app.models.immigration import ImmigrationCase
    from app.services.immigration.common import IMM_MANAGE, IMM_READ, scoped

    principal = ctx.principal
    if getattr(principal, "type", None) != "user":
        return False
    perm = IMM_MANAGE if write else IMM_READ
    if not principal.has(perm):
        return False
    stmt = scoped(ctx, select(ImmigrationCase.id).where(ImmigrationCase.id == entity_id), perm)
    return (await db.execute(stmt)).first() is not None


def _register_access() -> None:
    from app.services import activity_service, files_service

    files_service.register_access_checker("immigration_case", _case_access, plural="immigration-cases")
    activity_service.register_entity("immigration_case", _case_access, plural="immigration-cases")


def _register_audit() -> None:
    from app.core import audit

    for key, desc in (
        ("immigration.receipt_unmasked", "An immigration receipt number was unmasked"),
        ("immigration.permit_unmasked", "A permit number was unmasked"),
        ("immigration.passport_unmasked", "A dependant passport number was unmasked"),
        ("immigration.outcome_recorded", "An immigration case outcome (approved / refused) was recorded"),
        ("immigration.visa_type_adopted", "A visa type template was adopted"),
        ("immigration.visa_type_versioned", "A visa type definition was changed (new version)"),
    ):
        audit.register_audit_action(key, desc)


def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "workforce",
        "immigration",
        "Immigration",
        "Visa types (adopted from US, UK and UAE templates), immigration counsel and permit alert offsets.",
        "/api/v1/immigration/visa-types",
        "immigration:configure",
        owner_epic="Epic 46",
    )


async def _subject(db: AsyncSession, row_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.immigration import ImmigrationCase

    c = await db.get(ImmigrationCase, row_id)
    return c.candidate_id if c else None


async def _anchor(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
    """Case close date or the last Permit expiry, whichever is later (+3 years is the class period)."""
    from sqlalchemy import func

    from app.models.immigration import ImmigrationCase, Permit

    c = await db.get(ImmigrationCase, row_id)
    if c is None:
        return None
    last = (
        await db.execute(select(func.max(Permit.valid_to)).where(Permit.case_id == c.id))
    ).scalar_one_or_none()
    closed = c.closed_at.date() if c.closed_at else None
    if closed is None and c.status in ("open", "in_stage"):
        return None
    return max(d for d in (closed, last) if d is not None) if (closed or last) else None


async def _expire(*_: Any, **__: Any) -> None:
    """Retention expiry is executed by the erasure handler (``immigration_erasure``)."""
    return


def _register_retention() -> None:
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    if "immigration_cases" not in retention_bindings():
        register_retention_binding(
            "immigration_cases",
            "immigration",
            subject_resolver=_subject,
            anchor_resolver=_anchor,
            expire_fn=_expire,
            pii_columns=("receipt_number", "outcome_note", "withdraw_reason"),
        )


for _fn in (
    _register_notice,
    _register_human_only,
    _register_notifications,
    _subscribe,
    _register_access,
    _register_audit,
    _register_settings,
    _register_retention,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block start-up on an optional registry
        logger.exception("immigration: {} failed", _fn.__name__)
