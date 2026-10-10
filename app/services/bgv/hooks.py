# ruff: noqa: E501
"""Registrations of the ``bgv`` domain (autodiscovered by ``core.hooks``; Epic 20 A8).

* Start-gate kind ``bgv`` (not overridable; resolver over the placement's required cases).
* Event subscriptions -> ``bgv.handle_event``: ``ComplianceRequirementsEvaluated``, ``PlacementDropped``,
  ``ConsentWithdrawn``.
* Notification types ``bgv_documents_requested``, ``bgv_discrepancy``, ``bgv_check_overdue``,
  ``bgv_consent_withdrawn``, ``reference_requested``.
* Workflow: triggers ``bgv.case_opened``, ``bgv.discrepancy_raised``, ``bgv.case_adjudicated`` (the catalogue
  owns ``bgv.case_status_changed``), the ``bgv_case`` snapshot loader and the ``bgv.create_case`` action.
* SLA milestone ``('bgv', 'check_turnaround')``; files access checker ``bgv_case``; Worker 360 section ``bgv``;
  ``bgv_evidence`` retention bindings (purge as ``expire_fn``); the BGV webhook handler (fake / real adapters).
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import subscribe


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- start gate
def _register_gate() -> None:
    from app.services.bgv.adjudication import gate_resolver
    from app.services.placements.registry import StartGateKindDef, register_start_gate_kind, start_gate_kinds

    if "bgv" not in start_gate_kinds():
        register_start_gate_kind(
            "bgv",
            StartGateKindDef(label="Background verification", overridable=False, resolver=gate_resolver),
        )


# ---------------------------------------------------------------- events
def _handler(name: str) -> Any:
    def handle(event: Any) -> None:
        from app.services.notifications_service import event_payload
        from app.tasks import bgv as tasks
        from app.tasks.runtime import enqueue

        if name == "ConsentWithdrawn" and getattr(event, "purpose", None) != "bgv":
            return
        enqueue(tasks.handle_event, str(event.tenant_id), name, event_payload(event))

    handle.__name__ = f"bgv_on_{name}"
    return handle


def _subscribe() -> None:
    from app.events.compliance import ComplianceRequirementsEvaluated
    from app.events.placements import PlacementDropped
    from app.events.privacy import ConsentWithdrawn

    for cls in (ComplianceRequirementsEvaluated, PlacementDropped, ConsentWithdrawn):
        subscribe(cls, _handler(cls.__name__))


# ---------------------------------------------------------------- notifications
async def _candidate(db: AsyncSession, payload: Any) -> set[Any]:  # noqa: ARG001
    from app.services.notifications_service import Recipient

    cid = _uuid(payload.get("candidate_id"))
    return {Recipient("candidate", cid)} if cid else set()


async def _discrepancy_candidate(db: AsyncSession, payload: Any) -> set[Any]:
    from app.models.bgv import BgvDiscrepancy
    from app.services.notifications_service import Recipient

    did = _uuid(payload.get("discrepancy_id"))
    cid = (
        (
            await db.execute(select(BgvDiscrepancy.candidate_id).where(BgvDiscrepancy.id == did))
        ).scalar_one_or_none()
        if did
        else None
    )
    return {Recipient("candidate", cid)} if cid else set()


async def _listed(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # noqa: ARG001
    return {u for u in (_uuid(x) for x in payload.get("recipient_ids") or []) if u}


async def _case_ref(db: AsyncSession, case_id: uuid.UUID | None) -> str:
    from app.models.bgv import BgvCase

    if case_id is None:
        return "A BGV case"
    human = (await db.execute(select(BgvCase.human_id).where(BgvCase.id == case_id))).scalar_one_or_none()
    return f"BGV case {human}" if human else "A BGV case"


def _content(title: str, *, case_id: uuid.UUID | None = None, link: str | None = None) -> Any:
    from app.services.notifications_service import Content

    return Content(
        title=title[:300],
        entity_type="bgv_case" if case_id else None,
        entity_id=case_id,
        link_path=link or (f"/bgv/{case_id}" if case_id else None),
    )


async def _render_docs(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
    return _content(
        "Background verification: please give consent and upload your documents", link="/portal/work/bgv"
    )


async def _render_discrepancy(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
    return _content("Background verification: a finding needs your response", link="/portal/work/bgv")


async def _render_overdue(db: AsyncSession, payload: Any) -> Any:
    cid = _uuid(payload.get("case_id"))
    return _content(f"{await _case_ref(db, cid)}: a check is overdue", case_id=cid)


async def _render_withdrawn(db: AsyncSession, payload: Any) -> Any:
    cid = _uuid(payload.get("case_id"))
    return _content(
        f"{await _case_ref(db, cid)}: the candidate withdrew BGV consent -- work is halted", case_id=cid
    )


async def _render_reference(db: AsyncSession, payload: Any) -> Any:  # noqa: ARG001
    return _content("Please name your referees for a reference check", link="/portal/work/bgv")


def _register_notifications() -> None:
    from app.events.bgv import BgvDiscrepancyRaised
    from app.services.bgv import events as ev
    from app.services.notifications_service import (
        NotificationType,
        register_event_source,
        register_notification_type,
    )

    types = {
        "bgv_documents_requested": (
            NotificationType("BGV documents requested", "Compliance", "both", True, _candidate, _render_docs),
            ev.BgvDocumentsRequested,
        ),
        "bgv_discrepancy": (
            NotificationType(
                "BGV discrepancy", "Compliance", "both", True, _discrepancy_candidate, _render_discrepancy
            ),
            BgvDiscrepancyRaised,
        ),
        "bgv_check_overdue": (
            NotificationType("BGV check overdue", "Compliance", "both", False, _listed, _render_overdue),
            ev.BgvCheckOverdue,
        ),
        "bgv_consent_withdrawn": (
            NotificationType("BGV consent withdrawn", "Compliance", "both", True, _listed, _render_withdrawn),
            ev.BgvConsentWithdrawnNotice,
        ),
        "reference_requested": (
            NotificationType(
                "Reference check requested", "Compliance", "both", False, _candidate, _render_reference
            ),
            ev.ReferenceRequested,
        ),
    }
    for key, (definition, event_cls) in types.items():
        try:
            register_notification_type(key, definition)
        except Exception:
            logger.debug("notification type {} already registered", key)
            continue
        register_event_source(event_cls, key)


# ---------------------------------------------------------------- workflow
async def _snapshot(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID) -> dict[str, Any] | None:  # noqa: ARG001
    from app.models.bgv import BgvCase

    case = await db.get(BgvCase, entity_id)
    if case is None:
        return None
    return {
        "status": case.status,
        "required": case.required,
        "package_id": str(case.package_id),
        "placement_id": str(case.placement_id) if case.placement_id else None,
        "candidate_id": str(case.candidate_id),
        "outcome_qualifier": case.outcome_qualifier,
    }


def _register_workflow() -> None:
    from pydantic import BaseModel, ConfigDict

    from app.models.bgv import CASE_STATUSES
    from app.services.workflow import actions, catalogue, registry
    from app.services.workflow.conditions import FieldDef

    fields = {
        "status": FieldDef("enum", "Status", CASE_STATUSES),
        "required": FieldDef("bool", "Required"),
        "package_id": FieldDef("uuid", "Package"),
        "placement_id": FieldDef("uuid", "Placement"),
    }
    if "bgv_case" not in catalogue.snapshot_loaders():
        catalogue.register_snapshot_loader("bgv_case", _snapshot, fields)
    for key, label, cls, attr in (
        ("bgv.case_opened", "BGV case opened", "BgvCaseOpened", "case_id"),
        ("bgv.discrepancy_raised", "BGV discrepancy raised", "BgvDiscrepancyRaised", "case_id"),
        ("bgv.case_adjudicated", "BGV case adjudicated", "BgvCaseAdjudicated", "case_id"),
    ):
        if key in registry.TRIGGERS:
            continue
        registry.register_trigger(
            key,
            registry.Trigger(
                key=key,
                label=label,
                event_module="app.events.bgv",
                event_class=cls,
                entity_type="bgv_case",
                entity_attr=attr,
            ),
        )

    class CreateCaseParams(BaseModel):
        model_config = ConfigDict(extra="forbid")
        package_id: uuid.UUID

    def _validate(params: Any, entity_type: str, path: str) -> None:  # noqa: ARG001
        if entity_type not in ("candidate", "placement"):
            raise actions._invalid(path, "only for candidate or placement triggers")

    async def _run(ac: Any) -> str:
        from app.models.bgv import BgvPackage
        from app.models.placements import Placement
        from app.services.bgv import cases

        package = await ac.db.get(BgvPackage, ac.params.package_id)
        if package is None or package.archived_at is not None:
            return "Package not found"
        placement_id = None
        candidate_id = ac.entity_id
        if ac.entity_type == "placement":
            p = await ac.db.get(Placement, ac.entity_id)
            if p is None:
                return "Placement not found"
            placement_id, candidate_id = p.id, p.candidate_id
        case = await cases.create_case(
            ac.ctx,
            ac.db,
            candidate_id=candidate_id,
            package=package,
            placement_id=placement_id,
            application_id=None,
            required=False,
        )
        return f"Opened BGV case {case.human_id}"

    async def _dry(ac: Any) -> str:  # noqa: ARG001
        return "Would open a BGV case"

    if "bgv.create_case" not in actions.ACTIONS:
        actions.register_action(
            "bgv.create_case",
            actions.ActionDef(
                "Open a BGV case",
                CreateCaseParams,
                _validate,
                _run,
                _dry,
                entity_types=("candidate", "placement"),
            ),
        )


# ---------------------------------------------------------------- SLA, files, Worker 360, retention, webhooks
def _register_sla() -> None:
    from app.services.sla.registry import SlaMilestoneDef, milestone, register_sla_milestone

    if milestone("bgv", "check_turnaround") is None:
        register_sla_milestone(
            "bgv", "check_turnaround", SlaMilestoneDef("BGV check turnaround", scope_keys=("bgv_package_id",))
        )


async def _file_access(ctx: RequestContext, db: AsyncSession, entity_id: uuid.UUID, write: bool) -> bool:
    from app.models.bgv import BgvCase

    principal = ctx.principal
    if getattr(principal, "type", None) != "user":
        return False
    if not principal.has("bgv:manage" if write else "bgv:read"):
        return False
    return await db.get(BgvCase, entity_id) is not None


async def _worker_section(ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID) -> Any:
    from app.services.bgv import cases

    return [s.model_dump(mode="json") for s in await cases.summaries(ctx, db, candidate_id=candidate_id)]


def _register_misc() -> None:
    from app.services import files_service
    from app.services.placements.registry import register_worker_360_section

    files_service.register_access_checker("bgv_case", _file_access, plural="bgv-cases")
    register_worker_360_section("bgv", _worker_section)


def _register_retention() -> None:
    from app.services.bgv import retention
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    for table in ("bgv_evidence", "bgv_document_requests", "bgv_discrepancies"):
        if table in retention_bindings():
            continue

        async def subject(db: AsyncSession, row_id: uuid.UUID, _t: str = table) -> uuid.UUID | None:
            return await retention.candidate_for_row(db, row_id, table=_t)

        async def anchor(db: AsyncSession, row_id: uuid.UUID, _t: str = table) -> Any:
            return await retention.anchor_for_row(db, row_id, table=_t)

        register_retention_binding(
            table,
            "bgv_evidence",
            subject_resolver=subject,
            anchor_resolver=anchor,
            expire_fn=retention.expire_binding,
            pii_columns=("declared",) if table == "bgv_document_requests" else (),
        )


async def _webhook(ctx: RequestContext, db: AsyncSession, payload: dict[str, Any], config: Any) -> None:  # noqa: ARG001
    """Inbound provider result -> ``provider_proposal`` (never the result itself)."""
    from app.models.bgv import BgvCheck
    from app.services.bgv.common import now

    check_id = _uuid(payload.get("check_id"))
    ref = payload.get("external_ref")
    stmt = (
        select(BgvCheck).where(BgvCheck.id == check_id)
        if check_id
        else select(BgvCheck).where(BgvCheck.external_ref == ref)
    )
    check = (await db.execute(stmt)).scalar_one_or_none()
    if check is None or check.status != "in_progress":
        return
    result = payload.get("result")
    if result not in ("verified", "discrepancy", "unable_to_verify"):
        return
    check.provider_proposal = {
        "result": result,
        "note": payload.get("note"),
        "verifier_name": payload.get("verifier_name"),
        "source_contacted": payload.get("source_contacted"),
        "verified_on": payload.get("verified_on"),
        "evidence_file_ids": [],
        "submitted_at": now().isoformat(),
        "via": "webhook",
    }
    await db.flush()


def _register_webhook() -> None:
    from app.services.connectors.registry import register_webhook_handler, webhook_handler

    if webhook_handler("bgv") is None:
        register_webhook_handler("bgv", _webhook)


for _fn in (
    _register_gate,
    _subscribe,
    _register_notifications,
    _register_workflow,
    _register_sla,
    _register_misc,
    _register_retention,
    _register_webhook,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block start-up on an optional registry
        logger.exception("bgv: {} failed", _fn.__name__)
