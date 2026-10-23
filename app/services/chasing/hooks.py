# ruff: file-ignore[line-too-long, unused-function-argument]
"""Registrations of the ``chasing`` domain (autodiscovered by ``core.hooks``), Epic 62.

* Notification types ``compliance_chase_reminder`` (Worker portal, in-app + email), ``compliance_chase_escalation``
  (internal: coordinator + Placement AM) and ``bgv_follow_up_limit`` (internal: BGV case owner). Delivered with
  explicit recipients by the domain (no event fan-out).
* ``PlacementCreated`` -> ``chasing.placement_created`` (a Compliance Agent run when something is already due).
* Workflow trigger ``chasing.chase_escalated``.
"""

from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession


async def _none(db: AsyncSession, payload: Any) -> set[Any]:
    return set()


def _render(title: str) -> Any:
    async def render(db: AsyncSession, payload: Any) -> Any:
        from app.services.notifications_service import Content

        return Content(title=title)

    return render


def _register_notifications() -> None:
    from app.services import notifications_service as ns

    defs: dict[str, Any] = {
        "compliance_chase_reminder": ns.NotificationType(
            "Documents needed (Compliance Agent)", "Compliance", "both", False, _none, _render("Documents needed"),
            external_default_channel="in_app", portal_path="/portal/work/onboarding", external=True,
        ),
        "compliance_chase_escalation": ns.NotificationType(
            "Compliance chase escalated", "Compliance", "both", False, _none, _render("Compliance items still missing"),
            internal_only=True,
        ),
        "bgv_follow_up_limit": ns.NotificationType(
            "BGV provider follow-up limit reached", "BGV", "in_app", False, _none, _render("BGV follow-up limit reached"),
            internal_only=True,
        ),
    }  # fmt: skip
    for key, definition in defs.items():
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, definition)


def _subscribe() -> None:
    from app.events import subscribe_optional

    def placement_created(e: Any) -> None:
        from app.tasks import chasing as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.placement_created, str(e.tenant_id), str(e.candidate_id))

    placement_created.__name__ = "chasing_on_PlacementCreated"
    subscribe_optional("app.events.placements", "PlacementCreated", placement_created)


def _register_workflow() -> None:
    from app.services.workflow.conditions import FieldDef
    from app.services.workflow.registry import TRIGGERS, Trigger, register_trigger

    key = "chasing.chase_escalated"
    if key not in TRIGGERS:
        register_trigger(
            key,
            Trigger(
                key, "Compliance chase escalated", "app.events.chasing", "ChaseEscalated", "candidate", "candidate_id",
                {"reason": ("reason", FieldDef("str", "Reason"))},
            ),
        )  # fmt: skip


for _fn in (_register_notifications, _subscribe, _register_workflow):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("chasing hooks: {} failed", _fn.__name__)
