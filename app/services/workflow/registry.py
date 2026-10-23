"""Workflow triggers and condition fields (FR-58), and the event subscriptions that start rule runs.

``TRIGGERS[key] = Trigger(event module/class, entity type, entity id attribute, event.* extras, filter)``.
Triggers whose events belong to epics that are not merged yet subscribe through ``subscribe_optional`` (one
warning, no subscription). A matching event enqueues ``workflow.evaluate_event(tenant_id, trigger,
entity_type, entity_id, event_id, extras, chain_depth)`` with no countdown (runs within 60 s).

``chain_depth`` = the event's ``workflow_chain_depth`` + 1 when it was emitted under a workflow context
(read from the event if the emitting service copied it, else from the dispatching context), otherwise 0.

``FIELD_REGISTRY[entity_type]`` lists the snapshot paths conditions may use. Compensation is never a field.

Epic 13 (AD-31): ``TRIGGERS`` and ``FIELD_REGISTRY`` are live read-only views of registries filled by
``register_trigger`` / ``register_entity_fields`` (Phase 1 entries register at import; Phase 2 domains from their
``hooks.py``, trigger keys ``<domain>.<event>``). ``register_condition`` / ``evaluate_condition`` (R6) let a domain
answer a named yes/no question for another without being imported (e.g. ``bench.matches_reviewed``).
"""

import dataclasses
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext, get_context
from app.core.registry import Registry
from app.services.workflow.conditions import FieldDef as FieldDef

DEMAND_STATUSES = ("draft", "pending_approval", "open", "on_hold", "filled", "cancelled", "closed")
PRIORITIES = ("low", "medium", "high", "critical")
EMPLOYMENT_TYPES = ("contract", "contract_to_hire", "permanent", "freelance")
WORK_MODES = ("onsite", "hybrid", "remote")
SLA_STATUSES = ("healthy", "at_risk", "breached", "met")
SLA_MILESTONES = ("assignment", "first_submission", "first_interview", "client_feedback")
CANDIDATE_SOURCES = (
    "manual",
    "resume_upload",
    "bulk_import",
    "career_portal",
    "referral",
    "job_board",
    "other",
)
APPLICATION_SOURCES = (
    "manual", "search", "match", "saved_list", "candidate_360", "career_portal", "import", "api",
)  # fmt: skip
FEEDBACK = ("interested", "rejected", "rejected_pending", "on_hold")
ACTOR_KINDS = ("user", "candidate", "system", "workflow", "ai", "api_key", "agent", "app")  # +agent (53), +app (66)

_P1_FIELDS: dict[str, dict[str, FieldDef]] = {
    "demand": {
        "status": FieldDef("enum", "Status", DEMAND_STATUSES),
        "priority": FieldDef("enum", "Priority", PRIORITIES),
        "employment_type": FieldDef("enum", "Employment type", EMPLOYMENT_TYPES),
        "client_id": FieldDef("uuid", "Client"),
        "business_unit_id": FieldDef("uuid", "Business unit"),
        "positions": FieldDef("int", "Positions"),
        "work_mode": FieldDef("enum", "Work mode", WORK_MODES),
        "location.city": FieldDef("str", "Location"),
        "required_skills": FieldDef("list", "Required skills"),
        "sla.status": FieldDef("enum", "SLA status", SLA_STATUSES),
    },
    "application": {
        "stage": FieldDef("str", "Stage"),
        "source": FieldDef("enum", "Source", APPLICATION_SOURCES),
        "demand.priority": FieldDef("enum", "Demand priority", PRIORITIES),
        "demand.client_id": FieldDef("uuid", "Demand client"),
        "candidate.tags": FieldDef("list", "Candidate tags"),
        "screening_passed": FieldDef("bool", "Screening passed"),
        "last_client_feedback": FieldDef("enum", "Last client feedback", FEEDBACK),
    },
    "submission": {
        "below_min_margin": FieldDef("bool", "Below minimum margin"),
        "client_id": FieldDef("uuid", "Client"),
    },
    "candidate": {
        "source": FieldDef("enum", "Source", CANDIDATE_SOURCES),
        "tags": FieldDef("list", "Tags"),
        "owner_id": FieldDef("uuid", "Owner"),
        "location.city": FieldDef("str", "City"),
        "experience_years": FieldDef("decimal", "Experience (years)"),
    },
}


_fields: Registry[str, dict[str, FieldDef]] = Registry("workflow entity fields")
for _entity_type, _defs in _P1_FIELDS.items():
    _fields.register(_entity_type, _defs)

# Phase 1 name: a live read-only view.
FIELD_REGISTRY: Mapping[str, dict[str, FieldDef]] = _fields.view()


def register_entity_fields(entity_type: str, fields: Mapping[str, FieldDef]) -> None:
    _fields.register(entity_type, dict(fields))


@dataclasses.dataclass(frozen=True, slots=True)
class Trigger:
    key: str
    label: str
    event_module: str
    event_class: str
    entity_type: str
    entity_attr: str
    extras: Mapping[str, tuple[str, FieldDef]] = dataclasses.field(
        default_factory=dict
    )  # name -> (attr, def)
    matches: Callable[[object], bool] | None = None
    # Epic 48 (coordinator decision 9): when set, the only action types rules of this trigger may use
    # (save and run refuse others: ``action_not_allowed_for_trigger``). ``None`` = every action.
    allowed_actions: frozenset[str] | None = None


def _status_is(value: str) -> Callable[[object], bool]:
    def check(event: object) -> bool:
        return getattr(event, "status", None) == value

    return check


_DEMAND_STATUS = FieldDef("enum", "Demand status", DEMAND_STATUSES)

_P1_TRIGGERS: dict[str, Trigger] = {
    t.key: t
    for t in (
        Trigger(
            "demand.created", "Demand created", "app.events.demands", "DemandCreated", "demand", "demand_id"
        ),
        Trigger(
            "demand.status_changed",
            "Demand status changed",
            "app.events.demands",
            "DemandStatusChanged",
            "demand",
            "demand_id",
            {"from_status": ("from_status", _DEMAND_STATUS), "to_status": ("to_status", _DEMAND_STATUS)},
        ),
        Trigger(
            "demand.sla_at_risk",
            "Demand SLA at risk",
            "app.events.demands",
            "DemandSlaStatusChanged",
            "demand",
            "demand_id",
            {"milestone": ("milestone", FieldDef("enum", "SLA milestone", SLA_MILESTONES))},
            _status_is("at_risk"),
        ),
        Trigger(
            "demand.sla_breached",
            "Demand SLA breached",
            "app.events.demands",
            "DemandSlaStatusChanged",
            "demand",
            "demand_id",
            {"milestone": ("milestone", FieldDef("enum", "SLA milestone", SLA_MILESTONES))},
            _status_is("breached"),
        ),
        Trigger(
            "application.stage_changed",
            "Application stage changed",
            "app.events.pipeline",
            "ApplicationStageChanged",
            "application",
            "application_id",
            {
                "from_stage": ("from_key", FieldDef("str", "From stage")),
                "to_stage": ("to_key", FieldDef("str", "To stage")),
                "actor_kind": ("actor_kind", FieldDef("enum", "Changed by", ACTOR_KINDS)),
            },
        ),
        Trigger(
            "submission.created",
            "Submission created",
            "app.events.pipeline",
            "SubmissionCreated",
            "submission",
            "submission_id",
        ),
        Trigger(
            "client_feedback.received",
            "Client feedback received",
            "app.events.pipeline",
            "ClientFeedbackReceived",
            "application",
            "application_id",
            {"decision": ("decision", FieldDef("enum", "Decision", ("interested", "rejected", "on_hold")))},
        ),
        Trigger(
            "interview.scheduled",
            "Interview scheduled",
            "app.events.interviews",
            "InterviewScheduled",
            "application",
            "application_id",
            {"interview_stage": ("stage_name", FieldDef("str", "Interview stage"))},
        ),
        Trigger(
            "interview.completed",
            "Interview completed",
            "app.events.interviews",
            "InterviewCompleted",
            "application",
            "application_id",
            {"interview_stage": ("stage_name", FieldDef("str", "Interview stage"))},
        ),
        Trigger(
            "offer.accepted",
            "Offer accepted",
            "app.events.offers",
            "OfferAccepted",
            "application",
            "application_id",
        ),
        Trigger(
            "offer.declined",
            "Offer declined",
            "app.events.offers",
            "OfferDeclined",
            "application",
            "application_id",
        ),
        Trigger(
            "candidate.created",
            "Candidate created",
            "app.events.candidates",
            "CandidateCreated",
            "candidate",
            "candidate_id",
            {"source": ("source", FieldDef("enum", "Source", CANDIDATE_SOURCES))},
        ),
    )
}


_triggers: Registry[str, Trigger] = Registry("workflow trigger")
for _trigger in _P1_TRIGGERS.values():
    _triggers.register(_trigger.key, _trigger)

# Phase 1 name: a live read-only view.
TRIGGERS: Mapping[str, Trigger] = _triggers.view()


def register_trigger(key: str, trigger: Trigger) -> None:
    """Register a trigger (Phase 2 ``hooks.py``); subscribes its event handler at once when the Phase 1
    triggers are already wired."""
    if trigger.key != key:
        msg = f"trigger key mismatch: {key!r} vs {trigger.key!r}"
        raise ValueError(msg)
    _triggers.register(key, trigger)
    if _registered:
        from app.events import subscribe_optional

        subscribe_optional(trigger.event_module, trigger.event_class, _handler(trigger))


# ---------------------------------------------------------------- allowed actions (Epic 48, decision 9)
_restricted: dict[str, frozenset[str]] = {}


def restrict_trigger_actions(key: str, actions: Iterable[str]) -> None:
    """Restrict the action types of a trigger registered elsewhere (e.g. a contract-catalogue trigger); works
    whether the trigger is registered before or after this call."""
    _restricted[key] = frozenset(actions)


def allowed_actions_for(key: str) -> frozenset[str] | None:
    trigger = _triggers.find(key)
    declared = trigger.allowed_actions if trigger is not None else None
    extra = _restricted.get(key)
    if declared is None:
        return extra
    return declared if extra is None else declared & extra


def disallowed_actions(key: str, action_types: Iterable[str]) -> list[str]:
    allowed = allowed_actions_for(key)
    if allowed is None:
        return []
    return sorted({t for t in action_types if t not in allowed})


# ---------------------------------------------------------------- named conditions (R6)
ConditionFn = Callable[..., Awaitable[bool]]
_conditions: Registry[str, ConditionFn] = Registry("workflow condition")


def register_condition(key: str, fn: ConditionFn) -> None:
    _conditions.register(key, fn)


async def evaluate_condition(ctx: RequestContext, db: AsyncSession, key: str, **kwargs: Any) -> bool | None:
    """The registered condition's answer, or ``None`` when no domain registered ``key``."""
    fn = _conditions.find(key)
    if fn is None:
        return None
    return bool(await fn(ctx, db, **kwargs))


def fields_for(trigger_key: str) -> dict[str, FieldDef]:
    """Condition fields of a trigger: the entity snapshot paths plus its ``event.*`` extras."""
    trigger = TRIGGERS[trigger_key]
    fields = dict(FIELD_REGISTRY.get(trigger.entity_type, {}))
    fields.update({f"event.{name}": definition for name, (_, definition) in trigger.extras.items()})
    return fields


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def chain_depth_of(event: object) -> int:
    """Depth of the rule run an event starts: 0 when a human / the system caused it, else the depth of the
    emitting workflow run + 1 (``origin_chain_depth`` copied onto the event by the emitting service, or the
    dispatching context's ``workflow_chain_depth``)."""
    ctx = get_context()
    under_workflow = getattr(event, "actor_kind", None) == "workflow" or ctx.actor_kind == "workflow"
    if not under_workflow:
        return 0
    origin = getattr(event, "origin_chain_depth", None)
    if origin is None:
        origin = getattr(event, "workflow_chain_depth", None)
    if origin is None or (not origin and ctx.actor_kind == "workflow"):
        origin = max(int(origin or 0), ctx.workflow_chain_depth)
    return int(origin) + 1


def _handler(trigger: Trigger) -> Callable[[object], None]:
    def handle(event: object) -> None:
        if trigger.matches is not None and not trigger.matches(event):
            return
        tenant_id = getattr(event, "tenant_id", None)
        entity_id = getattr(event, trigger.entity_attr, None)
        if tenant_id is None or entity_id is None:
            return
        extras = {name: _jsonable(getattr(event, attr, None)) for name, (attr, _) in trigger.extras.items()}
        event_id = str(getattr(event, "event_id", None) or uuid.uuid4())
        from app.tasks import workflow as workflow_tasks
        from app.tasks.runtime import enqueue

        enqueue(
            workflow_tasks.evaluate_event,
            str(tenant_id),
            trigger.key,
            trigger.entity_type,
            str(entity_id),
            event_id,
            extras,
            chain_depth_of(event),
        )

    handle.__name__ = f"workflow_{trigger.key.replace('.', '_')}"
    return handle


_registered = False


def register() -> None:
    """Subscribe one handler per trigger (idempotent; missing event classes only warn)."""
    global _registered  # process-wide wiring
    if _registered:
        return
    from app.events import subscribe_optional

    for trigger in TRIGGERS.values():
        subscribe_optional(trigger.event_module, trigger.event_class, _handler(trigger))
    _registered = True


register()
