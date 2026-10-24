# ruff: file-ignore[line-too-long]
"""Registrations of the ``compliance`` domain (autodiscovered by ``core.hooks``), Epic 21 A8.

* Event subscriptions -> Celery tasks: ``PlacementCreated`` (evaluate), ``ApplicationStageChanged`` to
  ``selected`` (evaluate when configured), ``PlacementDropped`` (close), ``PolicyAcknowledged`` (onboarding,
  through the foundation event only), local ``ReevaluationRequested``.
* Start-gate kind ``credential`` (overridable, resolver over the checklist).
* Notification types ``compliance.credential_expiring``, ``compliance.credential_expired``,
  ``compliance.placement_non_compliant``, ``compliance.redaction_rejected``; workflow triggers of the first three.
* Tenant provisioning: seed credential types. Worker 360 section ``credentials``; placement filter ``compliance``.
* Retention binding of ``credentials`` (class ``worker_profile``, anchored on placement end; inert until Epic 35).
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import subscribe
from app.services.compliance.common import (
    CredentialExpired,
    CredentialExpiring,
    RedactionRejected,
    ReevaluationRequested,
)


# ---------------------------------------------------------------- events -> tasks
def _subscribe() -> None:
    from app.events.compliance import (
        PlacementComplianceChanged,  # ruff: ignore[unused-import]  (catalogue import check)  # noqa: F401
    )
    from app.events.onboarding import PolicyAcknowledged
    from app.events.pipeline import ApplicationStageChanged
    from app.events.placements import PlacementCreated, PlacementDropped
    from app.tasks import compliance as tasks
    from app.tasks.runtime import enqueue

    def on_created(e: PlacementCreated) -> None:
        enqueue(tasks.evaluate, str(e.tenant_id), "placement", str(e.placement_id))

    def on_stage(e: ApplicationStageChanged) -> None:
        if e.to_key == "selected":
            enqueue(tasks.evaluate, str(e.tenant_id), "application", str(e.application_id))

    def on_dropped(e: PlacementDropped) -> None:
        enqueue(tasks.close, str(e.tenant_id), str(e.placement_id))

    def on_policy(e: PolicyAcknowledged) -> None:
        enqueue(tasks.policy_ack, str(e.tenant_id), str(e.candidate_id), str(e.template_id))

    def on_reevaluate(e: ReevaluationRequested) -> None:
        enqueue(tasks.reevaluate_active, str(e.tenant_id), str(e.run_id))

    on_created.__name__ = "compliance_on_placement_created"
    subscribe(PlacementCreated, on_created)
    subscribe(ApplicationStageChanged, on_stage)
    subscribe(PlacementDropped, on_dropped)
    subscribe(PolicyAcknowledged, on_policy)
    subscribe(ReevaluationRequested, on_reevaluate)


_subscribe()


# ---------------------------------------------------------------- start gate
def _register_gate() -> None:
    from app.services.compliance import checklist
    from app.services.placements_service import StartGateKindDef, register_start_gate_kind, start_gate_kinds

    if "credential" not in start_gate_kinds():
        register_start_gate_kind(
            "credential", StartGateKindDef("Credentials", overridable=True, resolver=checklist.gate_resolver)
        )


_register_gate()


# ---------------------------------------------------------------- notifications
def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


async def _type_label(db: AsyncSession, key: Any) -> str:
    from app.models.compliance import CredentialType

    label = (
        await db.execute(select(CredentialType.label).where(CredentialType.key == str(key)))
    ).scalar_one_or_none()
    return label or str(key)


async def _staff(db: AsyncSession) -> set[uuid.UUID]:
    from app.services import entity_refs

    return (await entity_refs.role_user_ids(db, "hr_onboarding")) | (
        await entity_refs.role_user_ids(db, "compliance")
    )


async def _account_managers(db: AsyncSession, candidate_id: uuid.UUID | None) -> set[uuid.UUID]:
    from app.models.placements import Placement
    from app.services.compliance.common import OPEN_PLACEMENT_STATUSES

    if candidate_id is None:
        return set()
    rows = await db.execute(
        select(Placement.account_manager_user_id).where(
            Placement.candidate_id == candidate_id, Placement.status.in_(OPEN_PLACEMENT_STATUSES)
        )
    )
    return {r for r in rows.scalars() if r}


async def _expiring_recipients(db: AsyncSession, payload: Any) -> set[Any]:
    from app.services.notifications_service import Recipient

    cid = _uuid(payload.get("candidate_id"))
    out: set[Any] = set(await _staff(db))
    if cid:
        out.add(Recipient("candidate", cid))
    return out


async def _expired_recipients(db: AsyncSession, payload: Any) -> set[Any]:
    out = await _expiring_recipients(db, payload)
    return out | await _account_managers(db, _uuid(payload.get("candidate_id")))


async def _worker_only(db: AsyncSession, payload: Any) -> set[Any]:  # ruff: ignore[unused-function-argument]  # NOSONAR
    from app.services.notifications_service import Recipient

    cid = _uuid(payload.get("candidate_id"))
    return {Recipient("candidate", cid)} if cid else set()


async def _placement_recipients(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    from app.models.placements import Placement
    from app.services import entity_refs

    out = set(await entity_refs.role_user_ids(db, "compliance"))
    pid = _uuid(payload.get("placement_id"))
    if pid:
        am = (
            await db.execute(select(Placement.account_manager_user_id).where(Placement.id == pid))
        ).scalar_one_or_none()
        if am:
            out.add(am)
    return out


async def _render_expiring(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    label = await _type_label(db, payload.get("type_key"))
    return Content(
        title=f"{label} expires on {payload.get('expires_on')} ({payload.get('offset_days')} days)"[:300],
        body="Please renew it before it expires.",
        link_path="/compliance/credentials",
    )


async def _render_expired(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    label = await _type_label(db, payload.get("type_key"))
    return Content(
        title=f"{label} expired on {payload.get('expires_on')}"[:300], link_path="/compliance/credentials"
    )


async def _render_redaction(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    label = await _type_label(db, payload.get("type_key"))
    return Content(
        title=f"Please upload your {label} image again"[:300],
        body="The first 8 digits were still visible. Cover them with the redaction tool before uploading.",
        link_path="/portal/work/credentials",
    )


async def _render_placement(db: AsyncSession, payload: Any) -> Any:
    from app.models.placements import Placement
    from app.services.notifications_service import Content

    pid = _uuid(payload.get("placement_id"))
    human = (
        (await db.execute(select(Placement.human_id).where(Placement.id == pid))).scalar_one_or_none()
        if pid
        else None
    )
    state = "is blocked by compliance" if payload.get("blocking") else "is non-compliant"
    return Content(
        title=f"Placement {human or ''} {state}"[:300],
        entity_type="placement" if pid else None,
        entity_id=pid,
        link_path=f"/placements/{pid}?tab=compliance" if pid else None,
    )


def _register_notifications() -> None:
    from app.events.compliance import PlacementComplianceChanged
    from app.services import notifications_service as ns

    defs = {
        "compliance.credential_expiring": (
            ns.NotificationType(
                "Credential expiring", "Compliance", "both", False, _expiring_recipients, _render_expiring
            ),
            CredentialExpiring,
            None,
        ),
        "compliance.credential_expired": (
            ns.NotificationType(
                "Credential expired", "Compliance", "both", True, _expired_recipients, _render_expired
            ),
            CredentialExpired,
            None,
        ),
        "compliance.redaction_rejected": (
            ns.NotificationType(
                "Aadhaar image re-upload",
                "Compliance",
                "both",
                False,
                _worker_only,
                _render_redaction,
                "both",
            ),
            RedactionRejected,
            None,
        ),
        "compliance.placement_non_compliant": (
            ns.NotificationType(
                "Placement non-compliant",
                "Compliance",
                "in_app",
                False,
                _placement_recipients,
                _render_placement,
            ),
            PlacementComplianceChanged,
            lambda p: None if p.get("compliant") else "compliance.placement_non_compliant",
        ),
    }
    for key, (definition, cls, chooser) in defs.items():
        if key in ns.NOTIFICATION_TYPES:
            continue
        ns.register_notification_type(key, definition)
        ns.register_event_source(cls, chooser or key)


try:
    _register_notifications()
except Exception:  # pragma: no cover
    logger.exception("compliance: notification registration failed")


# ---------------------------------------------------------------- workflow triggers
def _register_triggers() -> None:
    from app.services.workflow.registry import TRIGGERS, Trigger, register_trigger

    local = "app.services.compliance.common"
    for trig in (
        Trigger(
            "compliance.credential_expiring",
            "Credential expiring",
            local,
            "CredentialExpiring",
            "candidate",
            "candidate_id",
        ),
        Trigger(
            "compliance.credential_expired",
            "Credential expired",
            local,
            "CredentialExpired",
            "candidate",
            "candidate_id",
        ),
        Trigger(
            "compliance.placement_non_compliant",
            "Placement became non-compliant",
            "app.events.compliance",
            "PlacementComplianceChanged",
            "placement",
            "placement_id",
            matches=lambda e: not getattr(e, "compliant", True),
        ),
    ):
        if trig.key not in TRIGGERS:
            register_trigger(trig.key, trig)


try:
    _register_triggers()
except Exception:  # pragma: no cover
    logger.exception("compliance: trigger registration failed")


# ---------------------------------------------------------------- provisioning
async def _provision(db: AsyncSession, tenant: Any) -> None:
    from app.services.compliance.requirements import seed_credential_types

    await seed_credential_types(db, tenant.id)


def _register_provisioning() -> None:
    from app.core.provisioning import register_tenant_provisioner

    try:
        register_tenant_provisioner("compliance.credential_types", _provision, order=60)
    except Exception:
        logger.debug("compliance provisioner already registered")


_register_provisioning()


# ---------------------------------------------------------------- Worker 360 / placement filter
async def _worker_section(ctx: Any, db: AsyncSession, candidate_id: uuid.UUID) -> Any:  # ruff: ignore[unused-function-argument]
    from app.services.compliance import credentials

    return [
        c.model_dump(mode="json", exclude={"rejection_note"})
        for c in await credentials.list_for_candidate(db, candidate_id)
    ]


def _compliance_filter(stmt: Any, value: str) -> Any:
    from app.models.placements import Placement

    if value == "non_compliant":
        return stmt.where(Placement.non_compliant.is_(True))
    if value == "blocking":
        return stmt.where(Placement.compliance_blocked.is_(True))
    if value == "compliant":
        return stmt.where(Placement.non_compliant.is_(False))
    return stmt


def _register_workforce() -> None:
    from app.services.placements_service import register_placement_filter, register_worker_360_section

    register_worker_360_section("credentials", _worker_section)
    register_placement_filter("compliance", _compliance_filter)


try:
    _register_workforce()
except Exception:  # pragma: no cover
    logger.exception("compliance: worker 360 / filter registration failed")


# ---------------------------------------------------------------- retention (R15)
async def _subject(db: AsyncSession, row_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.compliance import Credential

    c = await db.get(Credential, row_id)
    return c.candidate_id if c else None


async def _anchor(db: AsyncSession, candidate_id: uuid.UUID, *_: Any, **__: Any) -> Any:
    from sqlalchemy import func

    from app.models.placements import Placement

    return (
        await db.execute(
            select(func.max(func.coalesce(Placement.end_date, Placement.planned_end_date))).where(
                Placement.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none()


async def _expire(*_: Any, **__: Any) -> None:
    """Retention expiry is executed by the erasure handler (``compliance_erasure``)."""
    return


def _register_retention() -> None:
    try:
        from app.services.privacy.retention_classes import register_retention_binding, retention_bindings
    except ImportError:  # pragma: no cover
        return
    if "credentials" not in retention_bindings():
        register_retention_binding(
            "credentials",
            "worker_profile",
            subject_resolver=_subject,
            anchor_resolver=_anchor,
            expire_fn=_expire,
            pii_columns=("number", "issuer", "rejection_note"),
        )


try:
    _register_retention()
except Exception:  # pragma: no cover
    logger.exception("compliance: retention binding failed")


# ---------------------------------------------------------------- Epic 46: PermitRecorded -> Credential (AD-52)
# The handler that turns an immigration Permit into a P2 Credential lives here (build plan 5): compliance never
# imports ``immigration`` -- the permit number is read through a read-only ``table()`` projection of ``permits``
# (it is never put on the event) and compliance writes nothing into immigration tables. Idempotent: a non-erased
# Credential of the same type, Candidate and validity dates means the event was already handled. The recording
# Coordinator is the human source (``source='user'``) and the verifier: the Permit was recorded on a human-only
# (*) Approved outcome, so the Credential is ``valid`` at once and the P2 expiry engine alerts at the type's offsets.
async def record_permit_credential(
    db: AsyncSession,
    *,
    permit_id: uuid.UUID,
    candidate_id: uuid.UUID,
    credential_type_key: str,
    country: str,
    valid_from: Any,
    valid_to: Any,
) -> uuid.UUID | None:
    from sqlalchemy import column, table

    from app.core import audit
    from app.core.context import RequestContext
    from app.core.encrypted import Sealed, open_sealed
    from app.core.events import emit
    from app.events.compliance import CredentialVerified
    from app.models.compliance import Credential, CredentialType
    from app.schemas.compliance import CredentialIn, RenewIn
    from app.services.compliance import checklist, credentials, expiry
    from app.services.compliance.common import get_settings_row, now, tenant_of, today

    permits = table("permits", column("id"), column("number"), column("recorded_actor"))
    row = (
        await db.execute(select(permits.c.number, permits.c.recorded_actor).where(permits.c.id == permit_id))
    ).first()
    if row is None:
        return None
    t = (
        await db.execute(select(CredentialType).where(CredentialType.key == credential_type_key))
    ).scalar_one_or_none()
    if t is None:
        logger.warning("compliance: no credential type {} for permit {}", credential_type_key, permit_id)
        return None
    same = (
        await db.execute(
            select(Credential.id).where(
                Credential.candidate_id == candidate_id,
                Credential.type_key == credential_type_key,
                Credential.erased_at.is_(None),
                Credential.issued_on == valid_from,
                Credential.expires_on.is_(None) if valid_to is None else Credential.expires_on == valid_to,
            )
        )
    ).first()
    if same is not None:
        return None
    tenant_id = tenant_of(db)
    number = open_sealed(tenant_id, Sealed(row.number)) if row.number else None  # system copy, never returned
    ctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
    successors = select(Credential.previous_id).where(
        Credential.previous_id.is_not(None), Credential.erased_at.is_(None)
    )
    head = (
        await db.execute(
            select(Credential)
            .where(
                Credential.candidate_id == candidate_id,
                Credential.type_key == credential_type_key,
                Credential.erased_at.is_(None),
                Credential.superseded_at.is_(None),
                ~Credential.id.in_(successors),
            )
            .order_by(Credential.version_no.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if head is not None:
        c = await credentials.renew(
            ctx,
            db,
            head.id,
            RenewIn(number=number, issuer=country, issued_on=valid_from, expires_on=valid_to),
            None,
            source="user",
        )
    else:
        c = await credentials.record(
            ctx,
            db,
            candidate_id,
            CredentialIn(
                type_key=credential_type_key,
                number=number,
                issuer=country,
                issued_on=valid_from,
                expires_on=valid_to,
            ),
            None,
            source="user",
        )
    actor = row.recorded_actor or {}
    c.verification_status = "valid"
    c.verified_by = _uuid(actor.get("id")) if actor.get("kind") == "user" else None
    c.verified_at = now()
    settings = await get_settings_row(db)
    c.expiry_status = expiry.status_for(
        c.expires_on, await today(db), credentials.offsets_for(t, settings.default_alert_offsets)
    )
    prev_id = c.previous_id
    while prev_id is not None:
        prev = await db.get(Credential, prev_id)
        if prev is None:
            break
        if prev.superseded_at is None:
            prev.superseded_at = now()
        prev_id = prev.previous_id
    await db.flush()
    audit.record(
        "credential.verified",
        "credential",
        c.id,
        session=db,
        decision="valid",
        type_key=c.type_key,
        via="immigration_permit",
    )
    emit(
        db,
        CredentialVerified(
            tenant_id=c.tenant_id,
            credential_id=c.id,
            candidate_id=c.candidate_id,
            type_key=c.type_key,
            verification_status=c.verification_status,
        ),
    )
    await checklist.refresh_for_candidate(db, c.candidate_id, [c.type_key])
    return c.id


def _subscribe_permits() -> None:
    from app.core.events import subscribe
    from app.events.immigration import PermitRecorded

    def on_permit(e: PermitRecorded) -> None:
        from app.tasks import compliance as tasks
        from app.tasks.runtime import enqueue

        enqueue(
            tasks.permit_recorded,
            str(e.tenant_id),
            str(e.permit_id),
            str(e.candidate_id),
            e.credential_type_key,
            e.country,
            e.valid_from.isoformat(),
            e.valid_to.isoformat() if e.valid_to else None,
        )

    on_permit.__name__ = "compliance_on_permit_recorded"
    subscribe(PermitRecorded, on_permit)


try:
    _subscribe_permits()
except Exception:  # pragma: no cover
    logger.exception("compliance: PermitRecorded subscription failed")
