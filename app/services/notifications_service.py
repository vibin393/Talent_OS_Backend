"""Notifications service (Epic 10, story 10.3, FR-57).

* ``NOTIFICATION_TYPES`` -- the registry: ``type -> (label, group, default_channel, mandatory_in_app,
  recipients resolver, renderer)``. Mandatory types (approvals, mentions, SLA) can never turn their in-app
  channel off (422 ``notification_mandatory``); only their email can be switched off.
* **Fan-out** -- domain events (subscribed at import, with ``subscribe_optional`` for epics that may not exist
  yet) enqueue ``notifications.fan_out(tenant_id, type, payload)``. The task resolves recipients, drops the
  actor, inactive users and users who cannot read the entity, inserts rows ``ON CONFLICT DO NOTHING`` with
  ``dedupe_key = type:event_id:user_id`` (retries and duplicate deliveries are no-ops) and, for the ``email`` /
  ``both`` channel, queues a platform email (``comms_service.queue_email``, template
  ``templates/email/notification.html.j2``, deep link ``FRONTEND_URL + link_path``).
* **Reads** -- list, unread count (index-only count capped at 100), read / read-all / delete, preferences.
* **Digest** -- hourly per tenant: users whose local hour is their digest hour and who had no digest today get
  one email with unread notifications (<= 50, grouped), overdue tasks and tasks due today; nothing to report =
  no email. ``purge_old`` deletes read notifications after 90 days and all after 180 days.

Epic 13 (AD-31): the types, event sources and recipient directories are registries
(``register_notification_type`` / ``register_event_source`` / ``register_recipient_directory``; Phase 1 names
``NOTIFICATION_TYPES`` / ``EVENT_SOURCES`` stay importable). Notifications address ``(recipient_kind,
recipient_id)``: resolvers return user ids (``set[UUID]``) or ``Recipient(kind, id)`` values; external
recipients (candidate / client_user / vendor_user) get the type's ``external_default_channel`` (preferences are
for users only) and their email address from the recipient directory.
"""  # ruff: ignore[line-too-long]

import dataclasses
import json
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import jinja2
from loguru import logger
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_context
from app.core.errors import NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page, PageParams, paginate
from app.core.permissions import CATALOG, Principal, Scope
from app.core.registry import Registry
from app.core.settings import get_settings
from app.events.notifications import NotificationsCreated
from app.models.notifications import (
    Notification,
    NotificationDelivery,
    NotificationPreference,
    NotificationSettings,
)
from app.schemas.notifications import (
    DigestSettingsOut,
    NotificationOut,
    PreferenceItemOut,
    PreferencesIn,
    PreferencesOut,
    UnreadCountOut,
)
from app.services import entity_refs

Payload = Mapping[str, Any]
UNREAD_CAP = 100
DIGEST_MAX_ITEMS = 50
READ_RETENTION = timedelta(days=90)
ALL_RETENTION = timedelta(days=180)

_TEMPLATES = jinja2.Environment(
    loader=jinja2.FileSystemLoader(Path(__file__).resolve().parents[1] / "templates" / "email"),
    autoescape=True,
    undefined=jinja2.ChainableUndefined,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _principal(ctx: RequestContext) -> Principal:
    principal = ctx.principal
    if not isinstance(principal, Principal) or principal.type != "user" or principal.tenant_id is None:
        raise NotFound("Resource not found")
    return principal


# ================================================================ registry
@dataclasses.dataclass(frozen=True, slots=True)
class Content:
    title: str
    body: str | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    link_path: str | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Recipient:
    """A notification recipient (Epic 13): ``kind`` in user | candidate | client_user | vendor_user."""

    kind: str
    id: uuid.UUID


@dataclasses.dataclass(frozen=True, slots=True)
class RecipientInfo:
    email: str | None
    display_name: str
    org: tuple[str, uuid.UUID] | None  # ("client" | "vendor" | "worker", id) for external recipients


Resolver = Callable[[AsyncSession, Payload], Awaitable[set[uuid.UUID] | set[Recipient]]]
Renderer = Callable[[AsyncSession, Payload], Awaitable[Content]]
RecipientDirectory = Callable[[AsyncSession, uuid.UUID], Awaitable[RecipientInfo | None]]
RECIPIENT_KINDS = ("user", "candidate", "client_user", "vendor_user")
_ORG_COLUMN = {"candidate": "candidate_id", "client_user": "client_id", "vendor_user": "vendor_id"}


@dataclasses.dataclass(frozen=True, slots=True)
class NotificationType:
    label: str
    group: str
    default_channel: str  # in_app | email | both
    mandatory_in_app: bool
    recipients_resolver: Resolver
    render: Renderer
    # Epic 13: channel for external recipients (they have no preferences).
    external_default_channel: str = "both"
    # Epic 35 (FR-184): portal deep link for External Principals, a path template formatted with the payload
    # (``/approvals/{approval_id}``) and prefixed by ``portal_link`` per recipient kind. ``external=True`` declares
    # the type is delivered to External Principals: registering it without a ``portal_path`` is refused (422).
    portal_path: str | None = None
    external: bool = False
    internal_only: bool = False


def _uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError:
        return None


def _ids(payload: Payload, *keys: str) -> set[uuid.UUID]:
    out: set[uuid.UUID] = set()
    for key in keys:
        value = payload.get(key)
        values = value if isinstance(value, list | tuple | set) else [value]
        out.update(u for u in (_uuid(v) for v in values) if u is not None)
    return out


def _pick(*keys: str) -> Resolver:
    async def resolve(db: AsyncSession, payload: Payload) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
        return _ids(payload, *keys)

    return resolve


async def _primary_of_demand(db: AsyncSession, payload: Payload) -> set[uuid.UUID]:
    demand_id = _uuid(payload.get("demand_id"))
    if demand_id is None:
        return set()
    from app.services.demands import workflow_snapshot as demand_facts

    primary = await demand_facts.primary_recruiter_id(db, demand_id)
    return {primary} if primary else set()


def _plus_primary(*keys: str) -> Resolver:
    async def resolve(db: AsyncSession, payload: Payload) -> set[uuid.UUID]:
        return _ids(payload, *keys) | await _primary_of_demand(db, payload)

    return resolve


async def _approvers(db: AsyncSession, payload: Payload) -> set[uuid.UUID]:
    from app.services import directory_service as directory

    out = _ids(payload, "approver_user_ids")
    role_id = _uuid(payload.get("approver_role_id"))
    if role_id is not None:
        out |= await directory.user_ids_with_role(db, role_id)
    return out


def _system_ctx(db: AsyncSession) -> RequestContext:
    tenant_id = entity_refs.session_tenant(db)
    principal = Principal(
        type="user",
        id=uuid.UUID(int=0),
        tenant_id=tenant_id,
        permissions=dict.fromkeys(CATALOG, Scope.TENANT),
    )
    return RequestContext(tenant_id=tenant_id, principal=principal, actor_kind="system")


async def _requester(db: AsyncSession, payload: Payload) -> set[uuid.UUID]:
    request_id = _uuid(payload.get("request_id"))
    if request_id is None:
        return set()
    from app.services import approvals

    try:
        request = await approvals.get_request(_system_ctx(db), db, request_id)
    except Exception:  # missing request, or approvals unavailable
        logger.warning("approval_decided: request {} not readable", request_id)
        return set()
    return {request.submitted_by} if request.submitted_by else set()


async def _demand_assignees(db: AsyncSession, payload: Payload) -> set[uuid.UUID]:
    explicit = _ids(payload, "assignee_ids", "assignee_user_ids")
    if explicit:
        return explicit
    demand_id = _uuid(payload.get("demand_id"))
    if demand_id is None:
        return set()
    from app.services.demands import workflow_snapshot as demand_facts

    return await demand_facts.assignee_user_ids(db, demand_id)


async def _workflow_admins(db: AsyncSession, payload: Payload) -> set[uuid.UUID]:  # ruff: ignore[unused-function-argument]
    from app.services import identity_bridge

    return {row["id"] for row in await identity_bridge.users_with_permission(db, "workflow:manage")}


def _entity(payload: Payload, entity_type: str, key: str) -> tuple[str | None, uuid.UUID | None]:
    entity_id = _uuid(payload.get(key))
    return (entity_type, entity_id) if entity_id is not None else (None, None)


def _renderer(
    title: str, entity_type: str, key: str, body: str | None = None, *, label_title: bool = True
) -> Renderer:
    """Title template may use ``{label}`` (entity display label) and payload keys."""

    async def render(db: AsyncSession, payload: Payload) -> Content:
        etype, eid = _entity(payload, entity_type, key)
        label = await entity_refs.entity_label(db, etype, eid) if label_title else None
        values = {k: v for k, v in payload.items() if isinstance(v, str | int)}
        values["label"] = label or {"demand": "a demand", "candidate": "a candidate"}.get(
            entity_type, "an item"
        )
        text = title.format_map(_Default(values))
        return Content(
            title=text[:300],
            body=body.format_map(_Default(values))[:2000] if body else None,
            entity_type=etype,
            entity_id=eid,
            link_path=entity_refs.link_path(etype, eid),
        )

    return render


class _Default(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""


async def _render_mention(db: AsyncSession, payload: Payload) -> Content:
    etype = str(payload.get("entity_type") or "") or None
    eid = _uuid(payload.get("entity_id"))
    author = await entity_refs.user(db, _uuid(payload.get("author_id")))
    label = await entity_refs.entity_label(db, etype, eid)
    who = author.name if author else "Someone"
    return Content(
        title=f"{who} mentioned you" + (f" on {label}" if label else ""),
        entity_type=etype,
        entity_id=eid,
        link_path=entity_refs.link_path(etype, eid),
    )


async def _render_approval_requested(db: AsyncSession, payload: Payload) -> Content:
    subject_type = str(payload.get("subject_type") or "")
    label = await entity_refs.entity_label(db, subject_type, _uuid(payload.get("subject_id")))
    request_id = _uuid(payload.get("request_id"))
    return Content(
        title=f"Approval requested: {label or subject_type.replace('_', ' ')}"[:300],
        entity_type="approval_request",
        entity_id=request_id,
        link_path=entity_refs.link_path("approval_request", request_id),
    )


async def _render_approval_decided(db: AsyncSession, payload: Payload) -> Content:
    subject_type = str(payload.get("subject_type") or "")
    label = await entity_refs.entity_label(db, subject_type, _uuid(payload.get("subject_id")))
    status = str(payload.get("status") or "decided").replace("_", " ")
    request_id = _uuid(payload.get("request_id"))
    return Content(
        title=f"Your approval request for {label or subject_type.replace('_', ' ')} was {status}"[:300],
        entity_type="approval_request",
        entity_id=request_id,
        link_path=entity_refs.link_path("approval_request", request_id),
    )


async def _render_sla(db: AsyncSession, payload: Payload) -> Content:
    demand_id = _uuid(payload.get("demand_id"))
    label = await entity_refs.entity_label(db, "demand", demand_id)
    milestone = str(payload.get("milestone") or "").replace("_", " ")
    state = "breached" if payload.get("status") == "breached" else "at risk"
    return Content(
        title=f"SLA {state}: {milestone} on {label or 'a demand'}"[:300],
        entity_type="demand",
        entity_id=demand_id,
        link_path=entity_refs.link_path("demand", demand_id),
    )


async def _render_task(db: AsyncSession, payload: Payload, prefix: str) -> Content:
    task_id = _uuid(payload.get("task_id"))
    label = await entity_refs.entity_label(db, "task", task_id)
    return Content(
        title=f"{prefix}: {label or 'a task'}"[:300],
        entity_type="task",
        entity_id=task_id,
        link_path=entity_refs.link_path("task", task_id),
    )


async def _render_task_assigned(db: AsyncSession, payload: Payload) -> Content:
    return await _render_task(db, payload, "Task assigned to you")


async def _render_task_reminder(db: AsyncSession, payload: Payload) -> Content:
    return await _render_task(db, payload, "Reminder")


async def _render_custom(db: AsyncSession, payload: Payload) -> Content:  # ruff: ignore[unused-function-argument]
    etype = str(payload.get("entity_type") or "") or None
    eid = _uuid(payload.get("entity_id"))
    return Content(
        title=str(payload.get("message") or "Workflow notification")[:300],
        entity_type=etype,
        entity_id=eid,
        link_path=entity_refs.link_path(etype, eid),
    )


def _t(
    label: str, group: str, default: str, mandatory: bool, resolver: Resolver, render: Renderer
) -> NotificationType:
    return NotificationType(label, group, default, mandatory, resolver, render)


_P1_TYPES: dict[str, NotificationType] = {
    "demand_assigned": _t(
        "Assigned to a demand", "Demands", "both", False,
        _pick("added_user_ids", "expanded_team_member_ids"),
        _renderer("You were assigned to {label}", "demand", "demand_id"),
    ),
    "approval_requested": _t(
        "Approval requested", "Approvals", "both", True, _approvers, _render_approval_requested
    ),
    "approval_decided": _t("Approval decided", "Approvals", "in_app", False, _requester, _render_approval_decided),  # ruff: ignore[line-too-long]
    "mention": _t("Mentioned in a note", "Activity", "both", True, _pick("mentioned_user_id"), _render_mention),  # ruff: ignore[line-too-long]
    "sla_at_risk": _t("SLA at risk", "Demands", "both", True, _pick("recipient_ids"), _render_sla),
    "sla_breached": _t("SLA breached", "Demands", "both", True, _pick("recipient_ids"), _render_sla),
    "client_feedback": _t(
        "Client feedback received", "Pipeline", "both", False,
        _plus_primary("application_owner_id", "owner_id", "primary_recruiter_id"),
        _renderer("Client feedback received ({decision})", "application", "application_id", label_title=False),  # ruff: ignore[line-too-long]
    ),
    "screening_answered": _t(
        "AI screening answered", "Pipeline", "in_app", False,
        _pick("application_owner_id", "owner_id"),
        _renderer("A candidate answered the AI screening", "application", "application_id", label_title=False),  # ruff: ignore[line-too-long]
    ),
    "scorecard_due": _t(
        "Scorecard due", "Interviews", "both", False,
        _pick("interviewer_user_id", "user_id", "participant_user_id"),
        _renderer("Your interview scorecard is due", "interview", "interview_id", label_title=False),
    ),
    "interview_scheduled": _t(
        "Interview scheduled", "Interviews", "in_app", False,
        _pick("interviewer_user_ids", "internal_interviewer_ids", "internal_user_ids"),
        _renderer("You are on an interview panel", "interview", "interview_id", label_title=False),
    ),
    "offer_response": _t(
        "Offer response", "Offers", "both", False,
        _plus_primary("created_by", "offer_created_by", "owner_id"),
        _renderer("Offer {outcome}", "offer", "offer_id", label_title=False),
    ),
    "import_completed": _t(
        "Import completed", "Candidates", "in_app", False, _pick("created_by"),
        _renderer("Your candidate import has finished", "import_batch", "batch_id", label_title=False),
    ),
    "rediscovery_ready": _t(
        "Rediscovery ready", "Demands", "in_app", False, _demand_assignees,
        _renderer("Matching candidates are ready for {label}", "demand", "demand_id"),
    ),
    "task_assigned": _t("Task assigned", "Tasks", "both", False, _pick("owner_id"), _render_task_assigned),
    "task_reminder": _t("Task reminder", "Tasks", "both", False, _pick("owner_id"), _render_task_reminder),
    "email_failed": _t(
        "Email not sent", "Email", "in_app", False, _pick("sender_user_id"),
        _renderer("An email could not be sent", "email_message", "message_id", label_title=False),
    ),
    "email_bounced": _t(
        "Email bounced", "Email", "in_app", False, _pick("sender_user_id"),
        _renderer("An email you sent bounced ({bounce_type})", "email_message", "message_id", label_title=False),  # ruff: ignore[line-too-long]
    ),
    "workflow_failed": _t(
        "Workflow rule failed", "Workflow", "in_app", False, _workflow_admins,
        _renderer("A workflow rule failed ({status})", "workflow_execution", "execution_id", label_title=False),  # ruff: ignore[line-too-long]
    ),
    "export_ready": _t(
        "Export ready", "Reports", "in_app", False, _pick("requested_by", "user_id", "created_by"),
        _renderer("Your export is ready", "export", "export_id", label_title=False),
    ),
    "workflow_notice": _t("Workflow notification", "Workflow", "in_app", False, _pick("recipient_ids"), _render_custom),  # ruff: ignore[line-too-long]
}  # fmt: skip

_types: Registry[str, NotificationType] = Registry("notification type")
for _key, _definition in _P1_TYPES.items():
    _types.register(_key, _definition)

# Phase 1 name: a live read-only view of the registry.
NOTIFICATION_TYPES: Mapping[str, NotificationType] = _types.view()


def register_notification_type(key: str, definition: NotificationType) -> None:
    """Phase 2 domains register their types from ``hooks.py`` (duplicate keys raise). Epic 35: a type declared
    ``external`` must give a ``portal_path`` (422 at start-up otherwise)."""
    if definition.external and not definition.portal_path:
        raise ValidationFailed(
            f"Notification type {key} reaches External Principals but has no portal path",
            code="portal_path_required",
            details={"type": key},
        )
    _types.register(key, definition)


# ---------------------------------------------------------------- portal links (Epic 35, FR-184)
PORTAL_PREFIXES: Mapping[str, str] = {"client_user": "/client", "vendor_user": "/vendor", "candidate": "/portal/work"}


def _absolute(path: str | None) -> str | None:
    return f"{get_settings().frontend_url.rstrip('/')}{path}" if path else None


def portal_link(recipient_kind: str, path: str) -> str | None:
    """A portal deep link (relative to the app origin): never an internal-app link."""
    prefix = PORTAL_PREFIXES.get(recipient_kind)
    if prefix is None or not path:
        return None
    if path.startswith(("/client/", "/vendor/", "/portal/")):
        return path if path.startswith(prefix + "/") else None
    return f"{prefix}/{path.lstrip('/')}"


def _external_link(definition: "NotificationType", recipient_kind: str, payload: Mapping[str, Any] | None, content: "Content") -> str | None:
    if definition.portal_path:
        try:
            path = definition.portal_path.format_map(_Default(payload or {}))
        except (KeyError, ValueError, IndexError):
            path = ""
        return portal_link(recipient_kind, path)
    # no portal path: only a link that already points into this recipient's portal
    if content.link_path:
        return portal_link(recipient_kind, content.link_path) if content.link_path.startswith(("/client/", "/vendor/", "/portal/")) else None
    return None


def _external_content(content: "Content") -> "Content":
    """NFR-24 at render: no bank / identity values reach External Principals."""
    from app.services.ai.redaction import redact

    title, _ = redact(content.title)
    body, _ = redact(content.body) if content.body else (content.body, {})
    return dataclasses.replace(content, title=title, body=body)


async def _portal_email_off(db: AsyncSession, recipient: "Recipient", type_key: str) -> bool:
    from app.models.portal_notifications import PortalNotificationPreference

    row = (
        await db.execute(
            select(PortalNotificationPreference.email).where(
                PortalNotificationPreference.recipient_kind == recipient.kind,
                PortalNotificationPreference.recipient_id == recipient.id,
                PortalNotificationPreference.type == type_key,
            )
        )
    ).scalar_one_or_none()
    return row is False


# ---------------------------------------------------------------- recipient directory (Epic 13)
_directories: Registry[str, RecipientDirectory] = Registry("recipient directory")


def register_recipient_directory(kind: str, fn: RecipientDirectory) -> None:
    if kind not in RECIPIENT_KINDS:
        msg = f"unknown recipient kind {kind!r}"
        raise ValueError(msg)
    _directories.register(kind, fn)


async def _user_directory(db: AsyncSession, user_id: uuid.UUID) -> RecipientInfo | None:
    person = await entity_refs.user(db, user_id)
    if person is None or not person.is_active:
        return None
    return RecipientInfo(person.email.lower() if person.email else None, person.name, None)


register_recipient_directory("user", _user_directory)


async def recipient_info(db: AsyncSession, recipient: Recipient) -> RecipientInfo | None:
    directory = _directories.find(recipient.kind)
    return None if directory is None else await directory(db, recipient.id)


# ================================================================ event sources
def _sla_type(payload: Payload) -> str | None:
    return {"at_risk": "sla_at_risk", "breached": "sla_breached"}.get(str(payload.get("status")))


# (event module, event class, type key or selector, fixed payload additions)
EVENT_SOURCES: tuple[tuple[str, str, str | Callable[[Payload], str | None], dict[str, Any]], ...] = (
    ("app.events.demands", "DemandAssigned", "demand_assigned", {}),
    ("app.events.approvals", "ApprovalStepActivated", "approval_requested", {}),
    ("app.events.approvals", "ApprovalCompleted", "approval_decided", {}),
    ("app.events.activity", "UserMentioned", "mention", {}),
    ("app.events.demands", "DemandSlaStatusChanged", _sla_type, {}),
    ("app.events.pipeline", "ClientFeedbackReceived", "client_feedback", {}),
    ("app.events.pipeline", "AIScreeningAnswered", "screening_answered", {}),
    ("app.events.interviews", "ScorecardDue", "scorecard_due", {}),
    ("app.events.interviews", "InterviewScheduled", "interview_scheduled", {}),
    ("app.events.offers", "OfferAccepted", "offer_response", {"outcome": "accepted"}),
    ("app.events.offers", "OfferDeclined", "offer_response", {"outcome": "declined"}),
    ("app.events.offers", "OfferExpired", "offer_response", {"outcome": "expired"}),
    ("app.events.candidates", "ImportCompleted", "import_completed", {}),
    ("app.events.matching", "RediscoveryCompleted", "rediscovery_ready", {}),
    ("app.events.tasks", "TaskAssigned", "task_assigned", {}),
    ("app.events.tasks", "TaskReminderDue", "task_reminder", {}),
    ("app.events.comms", "EmailFailed", "email_failed", {}),
    ("app.events.comms", "EmailBounced", "email_bounced", {}),
    ("app.events.workflow", "WorkflowExecutionFailed", "workflow_failed", {}),
    ("app.events.reporting", "ExportCompleted", "export_ready", {}),
)

_event_sources: list[tuple[str, str | Callable[[Payload], str | None], dict[str, Any]]] = []


def register_event_source(
    event_cls: type, type_key_or_chooser: str | Callable[[Payload], str | None], extras: Mapping[str, Any] | None = None
) -> None:
    """Fan out ``event_cls`` events as notifications of ``type_key_or_chooser`` (Epic 13 registry form of
    ``EVENT_SOURCES``)."""
    from app.core.events import subscribe

    name = event_cls.__name__
    _event_sources.append((name, type_key_or_chooser, dict(extras or {})))
    subscribe(event_cls, _make_handler(name, type_key_or_chooser, dict(extras or {})))


# Payload keys naming the user who caused the event (never notified about their own action).
ACTOR_KEYS = ("actor_user_id", "author_id", "decided_by", "actor_id")
# Types whose events carry no actor field: the emitting request's user is the actor.
ACTOR_FROM_CONTEXT = frozenset({"demand_assigned"})


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def event_payload(event: object) -> dict[str, Any]:
    if dataclasses.is_dataclass(event) and not isinstance(event, type):
        raw = {f.name: getattr(event, f.name) for f in dataclasses.fields(event)}
    else:
        raw = {k: v for k, v in vars(event).items() if not k.startswith("_")}
    return {k: _jsonable(v) for k, v in raw.items()}


def event_id_for(event_name: str, payload: Mapping[str, Any], now: datetime | None = None) -> str:
    """The event's own id when it has one, else a deterministic id (same event, same hour = same id), so a
    re-delivered event never notifies twice."""  # ruff: ignore[non-imperative-mood]
    if payload.get("event_id"):
        return str(payload["event_id"])
    bucket = (now or _now()).strftime("%Y%m%d%H")
    material = json.dumps({k: v for k, v in payload.items() if k != "tenant_id"}, sort_keys=True, default=str)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"talent-os:{event_name}:{material}:{bucket}"))


def _make_handler(
    event_name: str, selector: str | Callable[[Payload], str | None], extra: Mapping[str, Any]
) -> Callable[[object], None]:
    def handle(event: object) -> None:
        payload = {**event_payload(event), **extra}
        type_key = selector(payload) if callable(selector) else selector
        tenant_id = payload.get("tenant_id")
        if type_key is None or tenant_id is None:
            return
        if type_key in ACTOR_FROM_CONTEXT and not any(payload.get(k) for k in ACTOR_KEYS):
            # The event carries no actor: the user whose request emitted it (after-commit dispatch).
            ctx = get_context()
            principal = ctx.principal
            if ctx.actor_kind is None and getattr(principal, "type", None) == "user":
                payload["actor_user_id"] = str(principal.id)
        payload["event_id"] = event_id_for(event_name, payload)
        from app.tasks import notifications as notification_tasks
        from app.tasks.runtime import enqueue

        enqueue(notification_tasks.fan_out, str(tenant_id), type_key, payload)

    handle.__name__ = f"notify_{event_name}"
    return handle


# ================================================================ fan-out
async def _channels(db: AsyncSession, user_ids: Iterable[uuid.UUID], type_key: str) -> dict[uuid.UUID, str]:
    ids = list(user_ids)
    if not ids:
        return {}
    rows = await db.execute(
        select(NotificationPreference.user_id, NotificationPreference.channel).where(
            NotificationPreference.user_id.in_(ids), NotificationPreference.type == type_key
        )
    )
    return dict(rows.tuples().all())


def render_email(template: str, **values: Any) -> str:
    return _TEMPLATES.get_template(template).render(**values)


def _split(recipients: Iterable[uuid.UUID | Recipient]) -> tuple[set[uuid.UUID], set[Recipient]]:
    users: set[uuid.UUID] = set()
    external: set[Recipient] = set()
    for r in recipients:
        if r is None:
            continue
        if isinstance(r, Recipient):
            if r.kind == "user":
                users.add(r.id)
            elif r.kind in RECIPIENT_KINDS:
                external.add(r)
        else:
            users.add(r)
    return users, external


async def _deliver_external(
    db: AsyncSession,
    type_key: str,
    definition: NotificationType,
    recipients: set[Recipient],
    content: Content,
    *,
    event_id: str,
    actor_user_id: uuid.UUID | None,
    payload: Mapping[str, Any] | None = None,
) -> list[Recipient]:
    """External recipients (Epic 13): the type's external channel, email from the recipient directory."""
    from app.services import comms_service

    tenant_id = entity_refs.session_tenant(db)
    tenant = await entity_refs.tenant(db, tenant_id)
    channel = definition.external_default_channel
    in_app = channel in ("in_app", "both") or definition.mandatory_in_app
    by_email = channel in ("email", "both")
    notified: list[Recipient] = []
    content = _external_content(content)  # Epic 35: NFR-24 at render
    for recipient in sorted(recipients, key=lambda r: (r.kind, str(r.id))):
        info = await recipient_info(db, recipient)
        if info is None or info.org is None:
            continue
        portal_href = _external_link(definition, recipient.kind, payload, content)
        org_values = {"client_id": None, "vendor_id": None, "candidate_id": None}
        org_values[_ORG_COLUMN[recipient.kind]] = info.org[1]
        inserted = (
            await db.execute(
                pg_insert(Notification)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    user_id=None,
                    recipient_kind=recipient.kind,
                    recipient_id=recipient.id,
                    type=type_key,
                    title=content.title[:300],
                    body=(content.body or None) and content.body[:2000],
                    entity_type=content.entity_type,
                    entity_id=content.entity_id,
                    link_path=portal_href,
                    actor_user_id=actor_user_id,
                    dedupe_key=f"{type_key}:{event_id}:{recipient.kind}:{recipient.id}"[:200],
                    in_app=in_app,
                    **org_values,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "recipient_kind", "recipient_id", "dedupe_key"])
                .returning(Notification.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            continue
        notified.append(recipient)
        email_off = not definition.mandatory_in_app and await _portal_email_off(db, recipient, type_key)
        if by_email and info.email and not email_off:
            html = render_email(
                "notification.html.j2",
                recipient_name=entity_refs.first_name(info.display_name) or info.display_name,
                title=content.title,
                body=content.body,
                link=_absolute(portal_href),
                tenant_name=tenant.name,
            )
            queued = await comms_service.queue_email(
                RequestContext(tenant_id=tenant_id),
                db,
                origin="system",
                to=[comms_service.RecipientSpec("to", info.email.lower())],
                subject=content.title,
                body_html=html,
                raw=True,
            )
            db.add(
                NotificationDelivery(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    notification_id=inserted,
                    channel="email",
                    status="queued",
                    email_message_id=queued.message_ids[0] if queued.message_ids else None,
                )
            )
    await db.flush()
    return notified


async def deliver(
    db: AsyncSession,
    type_key: str,
    recipients: Iterable[uuid.UUID | Recipient],
    content: Content,
    *,
    event_id: str,
    actor_user_id: uuid.UUID | None = None,
    payload: Mapping[str, Any] | None = None,
) -> list[uuid.UUID]:
    """Insert the notifications (idempotent) and queue the emails; returns the users newly notified (external
    recipients are delivered too, see ``_deliver_external``)."""
    from app.services import comms_service

    definition = NOTIFICATION_TYPES[type_key]
    tenant_id = entity_refs.session_tenant(db)
    user_ids, external = _split(recipients)
    if external:
        await _deliver_external(
            db, type_key, definition, external, content, event_id=event_id, actor_user_id=actor_user_id,
            payload=payload,
        )
    candidates = {u for u in user_ids if u is not None and u != actor_user_id}
    people = {u.id: u for u in (await entity_refs.users(db, candidates)).values() if u.is_active}
    prefs = await _channels(db, people, type_key)
    tenant = await entity_refs.tenant(db, tenant_id)
    base_url = get_settings().frontend_url.rstrip("/")
    notified: list[uuid.UUID] = []
    for user_id in sorted(people):
        if not await entity_refs.user_can_read(
            db, tenant_id, user_id, content.entity_type, content.entity_id
        ):
            continue
        channel = prefs.get(user_id, definition.default_channel)
        in_app = channel in ("in_app", "both") or definition.mandatory_in_app
        by_email = channel in ("email", "both")
        if not in_app and not by_email:
            continue
        inserted = (
            await db.execute(
                pg_insert(Notification)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    user_id=user_id,
                    recipient_kind="user",
                    recipient_id=user_id,
                    type=type_key,
                    title=content.title[:300],
                    body=(content.body or None) and content.body[:2000],
                    entity_type=content.entity_type,
                    entity_id=content.entity_id,
                    link_path=content.link_path,
                    actor_user_id=actor_user_id,
                    dedupe_key=f"{type_key}:{event_id}:{user_id}"[:200],
                    in_app=in_app,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "recipient_kind", "recipient_id", "dedupe_key"])
                .returning(Notification.id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            continue  # already delivered (retry / duplicate event)
        notified.append(user_id)
        if by_email:
            person = people[user_id]
            html = render_email(
                "notification.html.j2",
                recipient_name=entity_refs.first_name(person.name) or person.name,
                title=content.title,
                body=content.body,
                link=f"{base_url}{content.link_path}" if content.link_path else None,
                tenant_name=tenant.name,
            )
            queued = await comms_service.queue_email(
                RequestContext(tenant_id=tenant_id),
                db,
                origin="system",
                to=[comms_service.RecipientSpec("to", person.email.lower())],
                subject=content.title,
                body_html=html,
                raw=True,
            )
            db.add(
                NotificationDelivery(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    notification_id=inserted,
                    channel="email",
                    status="queued",
                    email_message_id=queued.message_ids[0] if queued.message_ids else None,
                )
            )
    await db.flush()
    if notified:
        emit(db, NotificationsCreated(tenant_id=tenant_id, type=type_key, user_ids=tuple(notified)))
    return notified


async def fan_out(db: AsyncSession, type_key: str, payload: Payload) -> int:
    """Celery body of ``notifications.fan_out``."""
    definition = NOTIFICATION_TYPES.get(type_key)
    if definition is None:
        logger.warning("Unknown notification type {}", type_key)
        return 0
    recipients = await definition.recipients_resolver(db, payload)
    actor = next((u for u in (_uuid(payload.get(k)) for k in ACTOR_KEYS) if u is not None), None)
    content = await definition.render(db, payload)
    event_id = str(payload.get("event_id") or uuid.uuid4())
    return len(await deliver(db, type_key, recipients, content, event_id=event_id, actor_user_id=actor, payload=payload))


async def notify_custom(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    recipients: Iterable[uuid.UUID],
    message: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    event_id: str | None = None,
) -> list[uuid.UUID]:
    """Workflow ``notify`` action: a free-text notification (type ``workflow_notice``) in the caller's session."""  # ruff: ignore[line-too-long]
    content = Content(
        title=message[:300],
        entity_type=entity_type,
        entity_id=entity_id,
        link_path=entity_refs.link_path(entity_type, entity_id),
    )
    return await deliver(
        db, "workflow_notice", recipients, content, event_id=event_id or str(uuid.uuid4()), actor_user_id=None
    )


# ================================================================ reads
def _out(n: Notification) -> NotificationOut:
    definition = NOTIFICATION_TYPES.get(n.type)
    return NotificationOut(
        id=n.id,
        type=n.type,
        group=definition.group if definition else "Other",
        title=n.title,
        body=n.body,
        entity_type=n.entity_type,
        entity_id=n.entity_id,
        link_path=n.link_path,
        actor_user_id=n.actor_user_id,
        read_at=n.read_at,
        created_at=n.created_at,
    )


_PRINCIPAL_RECIPIENT_KIND = {
    "user": "user",
    "candidate": "candidate",
    "client_user": "client_user",
    "vendor_user": "vendor_user",
}


def _recipient(ctx: RequestContext) -> Principal:
    """The caller as a notification recipient (users, candidates, Client / Vendor Users)."""
    principal = ctx.principal
    if (
        principal is None
        or getattr(principal, "type", None) not in _PRINCIPAL_RECIPIENT_KIND
        or getattr(principal, "tenant_id", None) is None
    ):
        raise NotFound("Resource not found")
    return principal  # type: ignore[no-any-return]


def _mine(principal: Principal) -> Any:
    return (
        Notification.recipient_kind == _PRINCIPAL_RECIPIENT_KIND[principal.type],
        Notification.recipient_id == principal.id,
        Notification.in_app.is_(True),
    )


async def list_for_principal(ctx: RequestContext, db: AsyncSession, params: PageParams) -> Page[NotificationOut]:
    """Any recipient principal's notifications (portal endpoints come in Epic 35)."""
    return await list_notifications(ctx, db, False, params)


async def list_notifications(
    ctx: RequestContext, db: AsyncSession, unread_only: bool, params: PageParams
) -> Page[NotificationOut]:
    principal = _recipient(ctx)
    stmt = select(Notification).where(*_mine(principal))
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    page = await paginate(db, stmt.order_by(Notification.created_at.desc(), Notification.id.desc()), params)
    return Page[NotificationOut](
        items=[_out(n) for n in page.items], total=page.total, page=page.page, page_size=page.page_size
    )


async def unread_count(ctx: RequestContext, db: AsyncSession) -> UnreadCountOut:
    """Index-only count of the caller's unread notifications, capped at ``UNREAD_CAP``."""
    principal = _recipient(ctx)
    capped = (
        select(Notification.created_at)
        .where(*_mine(principal), Notification.read_at.is_(None))
        .order_by(Notification.created_at.desc())
        .limit(UNREAD_CAP)
        .subquery()
    )
    count, latest = (await db.execute(select(func.count(), func.max(capped.c.created_at)))).one()
    return UnreadCountOut(count=int(count), latest_created_at=latest)


async def _own(db: AsyncSession, principal: Principal, notification_id: uuid.UUID) -> Notification:
    row = (
        await db.execute(select(Notification).where(Notification.id == notification_id, *_mine(principal)))
    ).scalar_one_or_none()
    if row is None:
        raise NotFound("Notification not found")
    return row


async def mark_read(ctx: RequestContext, db: AsyncSession, notification_id: uuid.UUID) -> NotificationOut:
    principal = _recipient(ctx)
    row = await _own(db, principal, notification_id)
    if row.read_at is None:
        row.read_at = _now()
        await db.flush()
    return _out(row)


async def mark_all_read(ctx: RequestContext, db: AsyncSession) -> UnreadCountOut:
    principal = _recipient(ctx)
    await db.execute(
        update(Notification)
        .where(*_mine(principal), Notification.read_at.is_(None))
        .values(read_at=_now())
        .execution_options(synchronize_session=False)
    )
    return UnreadCountOut(count=0, latest_created_at=None)


async def delete_notification(ctx: RequestContext, db: AsyncSession, notification_id: uuid.UUID) -> None:
    principal = _principal(ctx)
    row = await _own(db, principal, notification_id)
    await db.delete(row)
    await db.flush()


# ================================================================ preferences
async def _settings_row(db: AsyncSession, principal: Principal) -> NotificationSettings | None:
    return await db.get(NotificationSettings, (principal.tenant_id, principal.id))


async def get_preferences(ctx: RequestContext, db: AsyncSession) -> PreferencesOut:
    principal = _principal(ctx)
    rows = await db.execute(
        select(NotificationPreference.type, NotificationPreference.channel).where(
            NotificationPreference.user_id == principal.id
        )
    )
    stored = dict(rows.tuples().all())
    items = [
        PreferenceItemOut(
            type=key,
            label=d.label,
            group=d.group,
            channel=stored.get(key, d.default_channel),
            default_channel=d.default_channel,
            mandatory=d.mandatory_in_app,
        )
        for key, d in NOTIFICATION_TYPES.items()
    ]
    settings = await _settings_row(db, principal)
    tenant = await entity_refs.tenant(db, principal.tenant_id)  # type: ignore[arg-type]
    return PreferencesOut(
        items=items,
        digest=DigestSettingsOut(
            enabled=settings.digest_enabled if settings else False,
            hour_local=settings.digest_hour_local if settings else 8,
            timezone=tenant.timezone,
        ),
    )


async def put_preferences(ctx: RequestContext, db: AsyncSession, data: PreferencesIn) -> PreferencesOut:
    principal = _principal(ctx)
    for item in data.items:
        definition = NOTIFICATION_TYPES.get(item.type)
        if definition is None:
            raise ValidationFailed(
                f"Unknown notification type {item.type}",
                code="unknown_notification_type",
                details={"type": item.type},
            )
        if item.channel == "off" and definition.mandatory_in_app:
            raise ValidationFailed(
                "This notification cannot be turned off; only its email can be",
                code="notification_mandatory",
                details={"type": item.type},
            )
    existing = {
        p.type: p
        for p in (
            await db.execute(
                select(NotificationPreference).where(NotificationPreference.user_id == principal.id)
            )
        ).scalars()
    }
    for item in data.items:
        row = existing.get(item.type)
        if row is None:
            db.add(
                NotificationPreference(
                    id=uuid.uuid4(),
                    tenant_id=principal.tenant_id,
                    user_id=principal.id,
                    type=item.type,
                    channel=item.channel,
                )
            )
        elif row.channel != item.channel:
            row.channel = item.channel
    if data.digest is not None:
        settings = await _settings_row(db, principal)
        if settings is None:
            db.add(
                NotificationSettings(
                    tenant_id=principal.tenant_id,
                    user_id=principal.id,
                    digest_enabled=data.digest.enabled,
                    digest_hour_local=data.digest.hour_local,
                )
            )
        else:
            settings.digest_enabled = data.digest.enabled
            settings.digest_hour_local = data.digest.hour_local
    await db.flush()
    return await get_preferences(ctx, db)


# ================================================================ digest
async def run_digest(db: AsyncSession, now: datetime | None = None) -> int:
    """Send the daily digest to users of this tenant whose local hour is their digest hour (once a day)."""
    from app.services import comms_service, tasks_service

    now = now or _now()
    tenant_id = entity_refs.session_tenant(db)
    tenant = await entity_refs.tenant(db, tenant_id)
    tz = tenant.tz
    local_now = now.astimezone(tz)
    today = local_now.date()
    rows = list(
        (
            await db.execute(
                select(NotificationSettings)
                .where(
                    NotificationSettings.digest_enabled.is_(True),
                    NotificationSettings.digest_hour_local == local_now.hour,
                    or_(
                        NotificationSettings.last_digest_date.is_(None),
                        NotificationSettings.last_digest_date < today,
                    ),
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    base_url = get_settings().frontend_url.rstrip("/")
    sent = 0
    people = await entity_refs.users(db, [r.user_id for r in rows])
    for settings in rows:
        person = people.get(settings.user_id)
        if person is None or not person.is_active:
            settings.last_digest_date = today
            continue
        if settings.last_digest_date is not None:
            since = datetime.combine(settings.last_digest_date, time(settings.digest_hour_local), tzinfo=tz)
        else:
            since = now - timedelta(days=1)
        unread = list(
            (
                await db.execute(
                    select(Notification)
                    .where(
                        Notification.user_id == settings.user_id,
                        Notification.in_app.is_(True),
                        Notification.read_at.is_(None),
                        Notification.created_at >= since,
                    )
                    .order_by(Notification.created_at.desc())
                    .limit(DIGEST_MAX_ITEMS)
                )
            ).scalars()
        )
        overdue, due_today = await tasks_service.overdue_and_today(db, settings.user_id, tz, now)
        settings.last_digest_date = today
        if not unread and not overdue and not due_today:
            continue
        groups: dict[str, list[dict[str, Any]]] = {}
        for n in unread:
            label = NOTIFICATION_TYPES[n.type].group if n.type in NOTIFICATION_TYPES else "Other"
            groups.setdefault(label, []).append(
                {"title": n.title, "link": f"{base_url}{n.link_path}" if n.link_path else None}
            )

        def _task(t: Any) -> dict[str, Any]:
            due = (
                t.due_at.astimezone(tz).strftime("%d %b" if t.due_all_day else "%d %b %H:%M")
                if t.due_at
                else None
            )
            return {"title": t.title, "due": due, "link": f"{base_url}{entity_refs.link_path('task', t.id)}"}

        html = render_email(
            "digest.html.j2",
            recipient_name=entity_refs.first_name(person.name) or person.name,
            date_label=today.strftime("%a, %d %b %Y"),
            overdue_tasks=[_task(t) for t in overdue],
            today_tasks=[_task(t) for t in due_today],
            groups=[{"label": k, "items": v} for k, v in groups.items()],
            settings_link=f"{base_url}/settings/notifications",
        )
        queued = await comms_service.queue_email(
            RequestContext(tenant_id=tenant_id),
            db,
            origin="system",
            to=[comms_service.RecipientSpec("to", person.email.lower())],
            subject=f"Your {tenant.name} daily summary",
            body_html=html,
            raw=True,
        )
        for n in unread:
            db.add(
                NotificationDelivery(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    notification_id=n.id,
                    channel="digest",
                    status="queued",
                    email_message_id=queued.message_ids[0] if queued.message_ids else None,
                )
            )
        sent += 1
    await db.flush()
    return sent


async def purge_old(db: AsyncSession, now: datetime | None = None) -> int:
    now = now or _now()
    result = await db.execute(
        delete(Notification).where(
            or_(
                (Notification.read_at.is_not(None)) & (Notification.created_at < now - READ_RETENTION),
                Notification.created_at < now - ALL_RETENTION,
            )
        )
    )
    return int(result.rowcount or 0)


# ================================================================ wiring
_registered = False


def register() -> None:
    """Subscribe every registry source (idempotent; missing event classes only warn once)."""
    global _registered  # process-wide wiring
    if _registered:
        return
    from app.events import resolve_event_class, subscribe_optional

    for module_path, class_name, selector, extra in EVENT_SOURCES:
        if resolve_event_class(module_path, class_name) is None:
            subscribe_optional(module_path, class_name, _make_handler(class_name, selector, extra))  # warns once
            continue
        register_event_source(resolve_event_class(module_path, class_name), selector, extra)  # type: ignore[arg-type]
    _registered = True


register()
