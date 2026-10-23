"""Registrations of the ``vendors`` domain (autodiscovered by ``core.hooks``; Epic 17 A10).

* Approval subject ``vendor_bank_change`` (AD-24).
* ``files_service`` access checkers ``vendor`` (vendor documents) and ``vendor_submission`` (resumes).
* Workflow: snapshot loader ``vendor_submission`` (trigger ``vendors.submission_received`` is in the Epic 32
  catalogue), trigger ``vendors.status_changed``, action ``vendors.release_demand_to_tier``.
* Notification types ``vendors.release_received`` / ``vendors.submission_progress`` (vendor users),
  ``vendors.document_expiry`` / ``vendors.submission_received`` (internal users).
* SLA milestones ``vendor_submission.review`` and ``vendor_release.first_submission``.
* After-commit reactions (Celery ``vendors.*``): demand status / requirements, vendor status, pipeline stage,
  interviews, placements, submission work (notice link, SLA clocks, resume parse).
* Erasure: ``app/services/vendors_erasure.py`` (loaded by the privacy registry).
"""

import uuid
from collections.abc import Mapping
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import subscribe
from app.services import files_service
from app.services.approvals import register_approval_subject
from app.services.vendors import records
from app.services.vendors.work import VendorWork

# ---------------------------------------------------------------- approvals, files
register_approval_subject(
    records.BANK_SUBJECT,
    on_final_decided=records.on_bank_change_decided,
    describe=records.describe_bank_changes,
)
files_service.register_access_checker("vendor", records.file_access, plural="vendors")


async def _submission_file_access(
    ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID, write: bool
) -> bool:
    from app.core.permissions import Principal
    from app.models.vendors import VendorSubmission

    principal = ctx.principal
    if principal is None:
        return True
    if not isinstance(principal, Principal):
        return False
    sub = (
        await db.execute(select(VendorSubmission).where(VendorSubmission.id == submission_id))
    ).scalar_one_or_none()
    if sub is None:
        return False
    if principal.type == "vendor_user":
        return principal.org_id == sub.vendor_id
    if principal.type != "user" or write:
        return False
    return (
        principal.scope_of("vendor_submission:review") is not None
        or principal.scope_of("vendor:read") is not None
    )


files_service.register_access_checker(
    "vendor_submission", _submission_file_access, plural="vendor-submissions"
)


# ---------------------------------------------------------------- notifications
def _register_notifications() -> None:
    from app.services import notifications_service as ns

    async def _vendor_users(db: AsyncSession, payload: Mapping[str, Any]) -> set[Any]:
        from app.models.external_auth import VendorUser

        raw = payload.get("vendor_ids") or ([payload["vendor_id"]] if payload.get("vendor_id") else [])
        ids = [uuid.UUID(str(v)) for v in raw]
        if not ids:
            return set()
        rows = (
            await db.execute(
                select(VendorUser.id).where(VendorUser.vendor_id.in_(ids), VendorUser.status == "active")
            )
        ).scalars()
        return {ns.Recipient("vendor_user", uid) for uid in rows}

    async def _reviewers(db: AsyncSession, payload: Mapping[str, Any]) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
        from app.services import identity_bridge

        return {u["id"] for u in await identity_bridge.users_with_permission(db, "vendor_submission:review")}

    async def _nobody(db: AsyncSession, payload: Mapping[str, Any]) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
        return set()

    async def _render_release(db: AsyncSession, payload: Mapping[str, Any]) -> Any:
        from app.models.vendors import VendorRelease

        rid = payload.get("release_id")
        title = None
        if rid:
            title = (
                await db.execute(select(VendorRelease.title).where(VendorRelease.id == uuid.UUID(str(rid))))
            ).scalar_one_or_none()
        return ns.Content(
            title=f"New requirement released to you: {title or ''}"[:300], link_path=f"/vendor/releases/{rid}"
        )

    async def _render_received(db: AsyncSession, payload: Mapping[str, Any]) -> Any:  # ruff: ignore[unused-function-argument]
        return ns.Content(title="A vendor submitted a candidate for review", link_path="/vendors/review")

    def _render_plain(label: str) -> Any:
        async def render(db: AsyncSession, payload: Mapping[str, Any]) -> Any:  # ruff: ignore[unused-function-argument]
            return ns.Content(title=str(payload.get("title") or label))

        return render

    from app.events.vendors import VendorReleaseCreated, VendorSubmissionReceived

    types: list[tuple[str, Any, Any]] = [
        (
            "vendors.release_received",
            ns.NotificationType(
                "Requirement released", "Vendors", "both", False, _vendor_users, _render_release
            ),
            VendorReleaseCreated,
        ),
        (
            "vendors.submission_received",
            ns.NotificationType(
                "Vendor submission received", "Vendors", "in_app", False, _reviewers, _render_received
            ),
            VendorSubmissionReceived,
        ),
        (
            "vendors.submission_progress",
            ns.NotificationType(
                "Submission progress", "Vendors", "both", False, _nobody, _render_plain("Submission update")
            ),
            None,
        ),
        (
            "vendors.document_expiry",
            ns.NotificationType(
                "Vendor document expiry",
                "Vendors",
                "both",
                False,
                _nobody,
                _render_plain("Vendor document expiry"),
            ),
            None,
        ),
    ]
    for key, definition, event_cls in types:
        if key in ns.NOTIFICATION_TYPES:
            continue
        ns.register_notification_type(key, definition)
        if event_cls is not None:
            ns.register_event_source(event_cls, key)


_register_notifications()


# ---------------------------------------------------------------- SLA milestones
def _register_sla() -> None:
    from app.services.sla.registry import SlaMilestoneDef, milestone, register_sla_milestone

    for subject, key, label in (
        ("vendor_submission", "review", "Vendor submission reviewed"),
        ("vendor_release", "first_submission", "First vendor submission"),
    ):
        if milestone(subject, key) is None:
            register_sla_milestone(subject, key, SlaMilestoneDef(label, scope_keys=("vendor_id",)))


_register_sla()


# ---------------------------------------------------------------- workflow
class ReleaseToTierParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: str = Field(pattern=r"^(gold|silver|bronze|custom)$")
    reveal_client: bool = False
    submission_limit: int | None = Field(default=None, ge=1, le=1000)


def _register_workflow() -> None:
    from app.services.workflow import actions, catalogue
    from app.services.workflow.registry import TRIGGERS, FieldDef, Trigger, register_trigger

    async def _snapshot(
        ctx: RequestContext,  # ruff: ignore[unused-function-argument]
        db: AsyncSession,
        submission_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        from app.models.vendors import VendorSubmission

        sub = (
            await db.execute(select(VendorSubmission).where(VendorSubmission.id == submission_id))
        ).scalar_one_or_none()
        if sub is None:
            return None
        return {
            "id": str(sub.id),
            "status": sub.status,
            "vendor_id": str(sub.vendor_id),
            "demand_id": str(sub.demand_id),
            "client_id": str(sub.client_id),
        }

    if catalogue.snapshot_loader("vendor_submission") is None:
        catalogue.register_snapshot_loader(
            "vendor_submission",
            _snapshot,
            {
                "status": FieldDef(
                    "enum",
                    "Status",
                    ("submitted", "under_review", "accepted", "rejected", "duplicate", "withdrawn"),
                ),
                "vendor_id": FieldDef("uuid", "Vendor"),
                "demand_id": FieldDef("uuid", "Demand"),
                "client_id": FieldDef("uuid", "Client"),
            },
        )
    if "vendors.status_changed" not in TRIGGERS:
        register_trigger(
            "vendors.status_changed",
            Trigger(
                "vendors.status_changed",
                "Vendor status changed",
                "app.events.vendors",
                "VendorStatusChanged",
                "vendor",
                "vendor_id",
                {
                    "from_status": ("from_status", FieldDef("str", "From status")),
                    "to_status": ("to_status", FieldDef("str", "To status")),
                },
            ),
        )

    def _validate(params: Any, entity_type: str, path: str) -> None:  # ruff: ignore[unused-function-argument]
        if entity_type != "demand":
            raise actions._invalid(path, "only for demand triggers")

    async def _run(ac: Any) -> str:
        from app.schemas.vendors import ReleaseCreate
        from app.services.vendors import releases

        out = await releases.create_releases(
            ac.ctx,
            ac.db,
            ac.entity_id,
            ReleaseCreate(
                tier=ac.params.tier,
                reveal_client=ac.params.reveal_client,
                submission_limit=ac.params.submission_limit,
            ),
        )
        n = sum(1 for i in out.items if i.status == "released")
        return f"Released to {n} vendor(s) of tier {ac.params.tier}"

    async def _dry(ac: Any) -> str:
        return f"Would release the demand to the {ac.params.tier} vendors"

    if "vendors.release_demand_to_tier" not in actions.ACTIONS:
        actions.register_action(
            "vendors.release_demand_to_tier",
            actions.ActionDef(
                "Release to vendor tier", ReleaseToTierParams, _validate, _run, _dry, entity_types=("demand",)
            ),
        )


try:
    _register_workflow()
except Exception:  # pragma: no cover - never block start-up on the optional catalogue
    logger.exception("vendors: workflow catalogue registration failed")


# ---------------------------------------------------------------- after-commit reactions -> Celery
def _enqueue(task_name: str, *args: str) -> None:
    from app.tasks import vendors as tasks
    from app.tasks.ops_runtime import enqueue

    enqueue(getattr(tasks, task_name), *args)


def _on_work(event: VendorWork) -> None:
    _enqueue("work", str(event.tenant_id), event.action, str(event.ref_id))


def _on_demand_status(event: Any) -> None:
    if event.from_status == "open" and event.to_status != "open":
        _enqueue("demand_closed", str(event.tenant_id), str(event.demand_id))


def _on_demand_requirements(event: Any) -> None:
    _enqueue("demand_changed", str(event.tenant_id), str(event.demand_id))


def _on_stage(event: Any) -> None:
    _enqueue("stage_changed", str(event.tenant_id), str(event.application_id), str(event.to_key))


def _on_interview(event: Any) -> None:
    _enqueue("interview_changed", str(event.tenant_id), str(event.application_id))


def _on_placement(event: Any) -> None:
    vendor_id = getattr(event, "vendor_id", "x")
    if vendor_id is None:
        return
    _enqueue("placement_changed", str(event.tenant_id), str(event.placement_id), str(type(event).__name__))


def _subscribe() -> None:
    from app.events.demands import DemandRequirementsChanged, DemandStatusChanged
    from app.events.interviews import (
        InterviewCancelled,
        InterviewCompleted,
        InterviewRescheduled,
        InterviewScheduled,
    )
    from app.events.pipeline import ApplicationStageChanged
    from app.events.placements import PlacementCreated, PlacementDropped, PlacementStatusChanged

    subscribe(VendorWork, _on_work)
    subscribe(DemandStatusChanged, _on_demand_status)
    subscribe(DemandRequirementsChanged, _on_demand_requirements)
    subscribe(ApplicationStageChanged, _on_stage)
    for cls in (InterviewScheduled, InterviewRescheduled, InterviewCancelled, InterviewCompleted):
        subscribe(cls, _on_interview)
    for cls in (PlacementCreated, PlacementStatusChanged, PlacementDropped):
        subscribe(cls, _on_placement)


_subscribe()
