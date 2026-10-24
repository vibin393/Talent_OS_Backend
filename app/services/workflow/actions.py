"""Workflow actions (FR-58): a CLOSED registry ``ACTIONS[type] = ActionDef(params_model, validate, run,
dry_run)``, at most 10 per rule.

Human-only guard (FR-65, AD-4): there is deliberately no action that rejects an application, sends a
submission, releases an offer, decides an approval or changes a demand's status. ``move_stage`` only accepts
targets the pipeline transition table opens to the ``workflow`` actor; human-only targets are refused at save
time (422 ``invalid_action``) and again at run time (``error transition_not_allowed``), before the pipeline
service is even reached, which re-enforces the guard itself.

Every action runs with the rule's workflow context (``actor_kind="workflow"``, ``actor_id=<rule id>``) inside
its own savepoint; ``dry_run`` validates and resolves (owner, recipients, transition, rendered subject) and
never writes.
"""

import dataclasses
import inspect
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_serializer,
    model_validator,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError, ValidationFailed
from app.core.registry import Registry
from app.services import entity_refs

MAX_ACTIONS = 10
# Pipeline stages only a human may move an application to (§5.2 / FR-65). The pipeline's TRANSITIONS table is
# authoritative once Epic 6 lands; this list is the save/run-time guard that holds even before it does.
HUMAN_ONLY_STAGES = frozenset(
    {"shortlisted", "submitted", "selected", "offered", "hired", "rejected", "withdrawn"}
)
SELECTOR_RE = r"^(primary_recruiter|entity_owner|user:[0-9a-fA-F-]{36})$"
RECIPIENT_RE = (
    r"^(primary_recruiter|entity_owner|user:[0-9a-fA-F-]{36}|team:[0-9a-fA-F-]{36}|role:[a-z0-9_]{1,64})$"
)


class ActionError(Exception):
    """An action could not run; recorded as ``status=error`` with ``error_code``."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclasses.dataclass
class ActionContext:
    ctx: RequestContext
    db: AsyncSession
    tenant_id: uuid.UUID
    rule_id: uuid.UUID | None
    rule_created_by: uuid.UUID
    trigger: str
    entity_type: str
    entity_id: uuid.UUID
    snapshot: Mapping[str, Any]
    params: Any
    execution_id: uuid.UUID | None = None


# ================================================================ params
class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssignParams(_Params):
    user_ids: Annotated[list[uuid.UUID], Field(max_length=20)] = Field(default_factory=list)
    team_ids: Annotated[list[uuid.UUID], Field(max_length=20)] = Field(default_factory=list)
    primary_user_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _someone(self) -> "AssignParams":
        if not self.user_ids and not self.team_ids:
            msg = "give at least one user or team"
            raise ValueError(msg)
        if self.primary_user_id is not None and self.primary_user_id not in self.user_ids:
            msg = "primary_user_id must be one of user_ids"
            raise ValueError(msg)
        return self


class DueIn(_Params):
    """Epic 32: a task due date in business time (``core.business_hours``) or wall clock."""

    value: Annotated[int, Field(ge=1, le=8760)]
    unit: Literal["hours", "days", "business_hours", "business_days"]


class CreateTaskParams(_Params):
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    owner: Annotated[str, StringConstraints(pattern=SELECTOR_RE)] = "primary_recruiter"
    due_in_hours: Annotated[int, Field(ge=0, le=8760)] | None = None
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    reminder_before_minutes: Annotated[int, Field(ge=0, le=10080)] | None = None
    # Epic 32: escalation of the created task (library policy) and a business-time due date.
    escalation_policy_id: uuid.UUID | None = None
    due_in: DueIn | None = None

    @model_validator(mode="after")
    def _one_due(self) -> "CreateTaskParams":
        if self.due_in is not None and self.due_in_hours is not None:
            msg = "give due_in or due_in_hours, not both"
            raise ValueError(msg)
        return self

    @model_serializer(mode="wrap")
    def _compact(self, handler: Any) -> Any:  # Phase 1 rules keep their exact stored shape
        data = handler(self)
        for key in ("escalation_policy_id", "due_in"):
            if isinstance(data, dict) and data.get(key) is None:
                data.pop(key, None)
        return data


class NotifyParams(_Params):
    recipients: Annotated[
        list[Annotated[str, StringConstraints(pattern=RECIPIENT_RE)]], Field(min_length=1, max_length=20)
    ]
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    # Epic 32: internal users only -- External Principals are notified only through domain-registered
    # notification types, never free text from a rule.
    recipient_kind: Literal["user"] = "user"

    @model_serializer(mode="wrap")
    def _compact(self, handler: Any) -> Any:
        data = handler(self)
        if isinstance(data, dict) and data.get("recipient_kind") == "user":
            data.pop("recipient_kind", None)
        return data


class SendEmailParams(_Params):
    template_id: uuid.UUID
    to: Literal["candidate", "primary_contact", "entity_owner", "primary_recruiter"]


class MoveStageParams(_Params):
    to_stage_key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class AddTagParams(_Params):
    names: Annotated[
        list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]],
        Field(min_length=1, max_length=5),
    ]


class RefreshMatchesParams(_Params):
    pass


# ================================================================ helpers
def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _demand_id(ac: ActionContext) -> uuid.UUID | None:
    return ac.entity_id if ac.entity_type == "demand" else _uuid(ac.snapshot.get("demand_id"))


def _candidate_id(ac: ActionContext) -> uuid.UUID | None:
    return ac.entity_id if ac.entity_type == "candidate" else _uuid(ac.snapshot.get("candidate_id"))


async def _primary_recruiter(ac: ActionContext) -> uuid.UUID | None:
    explicit = _uuid(ac.snapshot.get("primary_recruiter_id"))
    if explicit is not None:
        return explicit
    demand_id = _demand_id(ac)
    if demand_id is None:
        return None
    from app.services.demands import workflow_snapshot as demand_facts

    return await demand_facts.primary_recruiter_id(ac.db, demand_id)


async def _select_user(ac: ActionContext, selector: str) -> uuid.UUID | None:
    if selector == "primary_recruiter":
        return await _primary_recruiter(ac)
    if selector == "entity_owner":
        return _uuid(ac.snapshot.get("owner_id"))
    if selector.startswith("user:"):
        return _uuid(selector.split(":", 1)[1])
    return None


async def _select_recipients(ac: ActionContext, selectors: list[str]) -> set[uuid.UUID]:
    from app.services import directory_service as directory

    out: set[uuid.UUID] = set()
    for selector in selectors:
        if selector.startswith("team:"):
            team_id = _uuid(selector.split(":", 1)[1])
            if team_id is not None:
                out |= await directory.team_member_ids(ac.db, [team_id])
        elif selector.startswith("role:"):
            out |= await entity_refs.role_user_ids(ac.db, selector.split(":", 1)[1])
        elif (user_id := await _select_user(ac, selector)) is not None:
            out.add(user_id)
    return await entity_refs.active_user_ids(ac.db, out)


def _refs(ac: ActionContext) -> dict[str, Any]:
    refs: dict[str, Any] = {f"{ac.entity_type}_id": str(ac.entity_id)}
    for key in ("demand_id", "candidate_id", "client_id", "application_id"):
        if ac.snapshot.get(key):
            refs.setdefault(key, str(ac.snapshot[key]))
    if ac.entity_type == "demand" and ac.snapshot.get("client_id"):
        refs["client_id"] = str(ac.snapshot["client_id"])
    return refs


async def _render_text(ac: ActionContext, text: str) -> str:
    from app.services.comms import merge_fields

    values = await entity_refs.merge_data(ac.db, ac.tenant_id, _refs(ac), sender_user_id=ac.rule_created_by)
    rendered, _ = merge_fields.render(text, values, escape=False)
    return rendered.strip()


async def _names(ac: ActionContext, user_ids: set[uuid.UUID]) -> str:
    people = await entity_refs.users(ac.db, user_ids)
    return ", ".join(sorted(p.name for p in people.values())) or "nobody"


def _hours(value: int | None) -> str:
    return f"due in {value} h" if value is not None else "no due date"


def _due_text(params: Any) -> str:
    due_in = getattr(params, "due_in", None)
    if due_in is not None:
        return f"due in {due_in.value} {due_in.unit.replace('_', ' ')}"
    return _hours(params.due_in_hours)


def _domain(exc: DomainError) -> ActionError:
    return ActionError(exc.code, exc.message)


# ================================================================ assign
async def _assign_plan(ac: ActionContext) -> tuple[Any, str]:
    from app.schemas.demands import AssignmentsIn
    from app.services.demands import assignment

    params: AssignParams = ac.params
    if ac.snapshot.get("status") not in ("open", "on_hold"):
        raise ActionError("demand_not_assignable", "Only Open or On Hold demands can be assigned")
    missing = set(params.user_ids) - await entity_refs.active_user_ids(ac.db, params.user_ids)
    if missing:
        raise ActionError("invalid_reference", f"{len(missing)} user(s) are not active")
    try:
        current = await assignment.list_assignments(ac.ctx, ac.db, ac.entity_id)
    except DomainError as exc:
        raise _domain(exc) from exc
    users = [a.user_id for a in current.items if a.user_id] + list(params.user_ids)
    teams = [a.team_id for a in current.items if a.team_id] + list(params.team_ids)
    primary = params.primary_user_id or next((a.user_id for a in current.items if a.is_primary), None)
    plan = AssignmentsIn(
        user_ids=list(dict.fromkeys(users)), team_ids=list(dict.fromkeys(teams)), primary_user_id=primary
    )
    who = await _names(ac, set(params.user_ids))
    detail = f"assign {len(params.user_ids)} user(s) ({who}) and {len(params.team_ids)} team(s)"
    return plan, detail


async def _assign_run(ac: ActionContext) -> str:
    from app.services.demands import assignment

    plan, detail = await _assign_plan(ac)
    try:
        await assignment.set_assignments(ac.ctx, ac.db, ac.entity_id, plan)
    except DomainError as exc:
        raise _domain(exc) from exc
    return detail[0].upper() + detail[1:]


async def _assign_dry(ac: ActionContext) -> str:
    _, detail = await _assign_plan(ac)
    return f"Would {detail}"


# ================================================================ create_task
async def _task_plan(ac: ActionContext) -> tuple[dict[str, Any], str]:
    from app.models.tasks import TASK_ENTITY_TYPES

    params: CreateTaskParams = ac.params
    owner = await _select_user(ac, params.owner)
    if owner is None or not await entity_refs.active_user_ids(ac.db, [owner]):
        raise ActionError("owner_unresolved", f"No active user for owner '{params.owner}'")
    title = (await _render_text(ac, params.title))[:200] or params.title
    now = datetime.now(UTC)
    due_at = now + timedelta(hours=params.due_in_hours) if params.due_in_hours is not None else None
    if params.due_in is not None:
        from app.core.business_hours import tenant_calendar
        from app.services.sla import durations

        cal, tz = await tenant_calendar(ac.db)
        due_at = durations.add(now, params.due_in.value, params.due_in.unit, cal, tz)
    reminder_at = None
    if due_at is not None and params.reminder_before_minutes is not None:
        candidate = due_at - timedelta(minutes=params.reminder_before_minutes)
        reminder_at = candidate if candidate > now + timedelta(seconds=30) else None
    linked = ac.entity_type in TASK_ENTITY_TYPES
    data = {
        "title": title,
        "owner_id": owner,
        "entity_type": ac.entity_type if linked else None,
        "entity_id": ac.entity_id if linked else None,
        "due_at": due_at,
        "priority": params.priority,
        "reminder_at": reminder_at,
    }
    person = await entity_refs.user(ac.db, owner)
    return (data, f"create task '{title}' for {person.name if person else owner} ({_due_text(params)})")


async def _task_run(ac: ActionContext) -> str:
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    data, detail = await _task_plan(ac)
    try:
        task = await tasks_service.create_task(
            ac.ctx, ac.db, TaskCreate(**data), source="workflow", workflow_execution_id=ac.execution_id
        )
    except DomainError as exc:
        raise _domain(exc) from exc
    policy_id = getattr(ac.params, "escalation_policy_id", None)
    if policy_id is not None:  # Epic 32: the task escalates under the named library policy
        from app.services.sla import escalations

        await escalations.open_task_watch(ac.db, task.id, policy_id)
    return detail[0].upper() + detail[1:]


async def _task_dry(ac: ActionContext) -> str:
    _, detail = await _task_plan(ac)
    return f"Would {detail}"


# ================================================================ notify
async def _notify_plan(ac: ActionContext) -> tuple[set[uuid.UUID], str, str]:
    params: NotifyParams = ac.params
    recipients = await _select_recipients(ac, params.recipients)
    if not recipients:
        raise ActionError("no_recipients", "No active recipients resolved")
    message = (await _render_text(ac, params.message))[:300] or params.message
    return (
        recipients,
        message,
        f"notify {len(recipients)} user(s) ({await _names(ac, recipients)}): '{message}'",
    )


async def _notify_run(ac: ActionContext) -> str:
    from app.services import notifications_service

    recipients, message, detail = await _notify_plan(ac)
    await notifications_service.notify_custom(
        ac.ctx,
        ac.db,
        recipients=recipients,
        message=message,
        entity_type=ac.entity_type,
        entity_id=ac.entity_id,
        event_id=f"wf:{ac.execution_id}",
    )
    return detail[0].upper() + detail[1:]


async def _notify_dry(ac: ActionContext) -> str:
    _, _, detail = await _notify_plan(ac)
    return f"Would {detail}"


# ================================================================ send_email
async def _email_plan(ac: ActionContext) -> tuple[Any, Any, Any, uuid.UUID | None, str]:
    from app.models.comms import EmailTemplate
    from app.services import comms_service

    params: SendEmailParams = ac.params
    template = await ac.db.get(EmailTemplate, params.template_id)
    if template is None or template.archived_at is not None:
        raise ActionError("template_not_found", "The email template no longer exists")
    variant = await comms_service._variant_for(ac.db, template.id, "en")
    if variant is None:
        raise ActionError("template_not_found", "The email template has no English version")
    reply_to = await _select_user(ac, "entity_owner") or ac.rule_created_by
    try:
        if params.to == "candidate":
            candidate_id = _candidate_id(ac)
            if candidate_id is None:
                raise ActionError("recipient_unresolved", "The trigger has no candidate")
            recipient = await comms_service.recipient_for(ac.db, "candidate", candidate_id)
        elif params.to == "primary_contact":
            client_id = _uuid(ac.snapshot.get("client_id") or ac.snapshot.get("demand.client_id"))
            contact_id = await entity_refs.primary_contact_id(ac.db, client_id) if client_id else None
            if contact_id is None:
                raise ActionError("recipient_unresolved", "The client has no contact with an email")
            recipient = await comms_service.recipient_for(ac.db, "contact", contact_id)
        else:
            user_id = await _select_user(ac, params.to)
            person = await entity_refs.user(ac.db, user_id)
            if person is None or not person.is_active:
                raise ActionError("recipient_unresolved", f"No active user for '{params.to}'")
            recipient = comms_service.RecipientSpec("to", person.email.lower())
    except DomainError as exc:
        raise _domain(exc) from exc
    values = await entity_refs.merge_data(
        ac.db, ac.tenant_id, _refs(ac), sender_user_id=reply_to, recipient=recipient.addressee()
    )
    from app.services.comms import merge_fields

    subject, _ = merge_fields.render(variant.subject, values, escape=False)
    return variant, template, recipient, reply_to, f"email '{subject.strip()}' to {recipient.address}"


async def _email_run(ac: ActionContext) -> str:
    from app.services import comms_service

    variant, _template, recipient, reply_to, detail = await _email_plan(ac)
    await comms_service.queue_email(
        ac.ctx,
        ac.db,
        origin="workflow",
        to=[recipient],
        subject=variant.subject,
        body_html=variant.body_html,
        related=[(ac.entity_type, ac.entity_id)]
        if ac.entity_type in ("demand", "candidate", "application")
        else (),
        refs=_refs(ac),
        template_variant_id=variant.id,
        template_version=variant.version,
        workflow_execution_id=ac.execution_id,
        reply_to_user_id=reply_to,
    )
    return detail[0].upper() + detail[1:]


async def _email_dry(ac: ActionContext) -> str:
    *_, detail = await _email_plan(ac)
    return f"Would {detail}"


# ================================================================ move_stage
def _pipeline() -> Any | None:
    return entity_refs.optional_service("app.services.pipeline_service")


# Anchored (non-custom) stage keys; any other key names a tenant custom stage.
ANCHORED_STAGES = HUMAN_ONLY_STAGES | {"applied", "interviewing"}


def workflow_allowed_target(to_stage_key: str) -> bool:
    """Save / run-time guard: only targets the pipeline's ``TRANSITIONS`` open to ``workflow`` (from a real
    stage). Human-only targets are refused even before the pipeline exists."""
    if to_stage_key in HUMAN_ONLY_STAGES:
        return False
    pipeline = _pipeline()
    table = getattr(pipeline, "TRANSITIONS", None) if pipeline is not None else None
    if not table:
        return True  # pipeline not merged: the run-time call re-checks
    targets = {
        str(dst)
        for (src, dst), actors in table.items()
        if "workflow" in actors and not str(src).startswith("__")
    }
    if to_stage_key in targets:
        return True
    return "custom" in targets and to_stage_key not in ANCHORED_STAGES


def _mover() -> Callable[..., Awaitable[Any]]:
    pipeline = _pipeline()
    move = getattr(pipeline, "transition_application", None) if pipeline is not None else None
    if not callable(move):
        raise ActionError("pipeline_unavailable", "The pipeline is not available yet")
    return move  # type: ignore[no-any-return]


async def _move_plan(ac: ActionContext) -> str:  # NOSONAR
    params: MoveStageParams = ac.params
    if not workflow_allowed_target(params.to_stage_key):
        raise ActionError(
            "transition_not_allowed", f"Workflow rules cannot move applications to '{params.to_stage_key}'"
        )
    current = ac.snapshot.get("stage")
    if current == params.to_stage_key:
        raise ActionError("same_stage", "The application is already in this stage")
    return f"move the application from '{current or '?'}' to '{params.to_stage_key}'"


async def _move_run(ac: ActionContext) -> str:
    detail = await _move_plan(ac)
    move = _mover()
    try:
        await move(ac.ctx, ac.db, ac.entity_id, ac.params.to_stage_key, "workflow")
    except DomainError as exc:
        code = "transition_not_allowed" if exc.status_code in (403, 409) else exc.code
        raise ActionError(code, exc.message) from exc
    return detail[0].upper() + detail[1:]


async def _move_dry(ac: ActionContext) -> str:
    detail = await _move_plan(ac)
    _mover()
    pipeline = _pipeline()
    allowed_actors = getattr(pipeline, "allowed_actors", None)
    current = ac.snapshot.get("stage")
    if callable(allowed_actors) and current:
        target_kind = "anchored" if ac.params.to_stage_key in ANCHORED_STAGES else "custom"
        current_kind = "anchored" if current in ANCHORED_STAGES else "custom"
        allowed = allowed_actors(current, current_kind, ac.params.to_stage_key, target_kind)
        if allowed is None or "workflow" not in allowed:
            raise ActionError("transition_not_allowed", f"Transition not allowed from '{current}'")
    return f"Would {detail}"


# ================================================================ add_tag
async def _tag_plan(ac: ActionContext) -> tuple[uuid.UUID, str]:  # NOSONAR
    candidate_id = _candidate_id(ac)
    if candidate_id is None:
        raise ActionError("candidate_unresolved", "The trigger has no candidate")
    names = ", ".join(ac.params.names)
    return candidate_id, f"add tag(s) {names} to the candidate"


async def _tag_run(ac: ActionContext) -> str:
    from app.schemas.candidates import TagNamesIn
    from app.services.candidates import tags_lists

    candidate_id, detail = await _tag_plan(ac)
    try:
        await tags_lists.add_tags(ac.ctx, ac.db, candidate_id, TagNamesIn(names=list(ac.params.names)))
    except DomainError as exc:
        raise _domain(exc) from exc
    return detail[0].upper() + detail[1:]


async def _tag_dry(ac: ActionContext) -> str:
    _, detail = await _tag_plan(ac)
    return f"Would {detail}"


# ================================================================ refresh_matches
def _refresher() -> Callable[..., Awaitable[Any]] | None:
    module = entity_refs.optional_service("app.services.matching_service", "app.services.matching")
    fn = getattr(module, "request_refresh", None) if module is not None else None
    return fn if callable(fn) else None


async def _refresh_plan(ac: ActionContext) -> tuple[Callable[..., Awaitable[Any]], uuid.UUID]:  # NOSONAR
    demand_id = _demand_id(ac)
    if demand_id is None:
        raise ActionError("demand_unresolved", "The trigger has no demand")
    fn = _refresher()
    if fn is None:
        raise ActionError("matching_unavailable", "Matching is not available yet")
    return fn, demand_id


async def _refresh_run(ac: ActionContext) -> str:
    fn, demand_id = await _refresh_plan(ac)
    try:
        params = inspect.signature(fn).parameters
        result = fn(ac.ctx, ac.db, demand_id) if "db" in params else fn(ac.ctx, demand_id)
        if inspect.isawaitable(result):
            await result
    except DomainError as exc:
        raise _domain(exc) from exc
    return "Requested a match refresh for the demand"


async def _refresh_dry(ac: ActionContext) -> str:
    await _refresh_plan(ac)
    return "Would request a match refresh for the demand"


# ================================================================ registry
Validator = Callable[[Any, str, str], None]  # (params, trigger entity type, path)


def _entity_types(*allowed: str) -> Validator:
    def check(params: Any, entity_type: str, path: str) -> None:  # ruff: ignore[unused-function-argument]
        if entity_type not in allowed:
            raise _invalid(path, f"only for {', '.join(allowed)} triggers")

    return check


def _validate_move(params: Any, entity_type: str, path: str) -> None:
    _entity_types("application")(params, entity_type, path)
    if not workflow_allowed_target(params.to_stage_key):
        raise _invalid(f"{path}.params.to_stage_key", "transition_not_allowed: human-only target stage")


def _validate_email(params: Any, entity_type: str, path: str) -> None:
    if params.to == "candidate" and entity_type not in ("candidate", "application"):
        raise _invalid(f"{path}.params.to", "only candidate or application triggers have a candidate")
    if params.to == "primary_contact" and entity_type not in ("demand", "application", "submission"):
        raise _invalid(f"{path}.params.to", "only demand, application or submission triggers have a client")


def _any(params: Any, entity_type: str, path: str) -> None:  # ruff: ignore[unused-function-argument]
    return None


@dataclasses.dataclass(frozen=True, slots=True)
class ActionDef:
    label: str
    params_model: type[BaseModel]
    validate: Validator
    run: Callable[[ActionContext], Awaitable[str]]
    dry_run: Callable[[ActionContext], Awaitable[str]]
    # Epic 32 (FR-167 / FR-181): trigger entity types the action accepts (empty = any, checked by ``validate``)
    # and the (entity_type, target status) transitions it performs -- never a human-only one.
    entity_types: tuple[str, ...] = ()
    performs_transitions: tuple[tuple[str, str], ...] = ()
    # Epic 38 (additive): optional save-time check that needs the database, ``(db, params, path) -> None``
    # raising ``ValidationFailed`` (e.g. ``talent_pools.add_to_pool`` refuses a dynamic Pool).
    check: Callable[[AsyncSession, Any, str], Awaitable[None]] | None = None


async def check_graph_actions(db: AsyncSession, graph: Mapping[str, Any]) -> None:
    """Run the optional async ``check`` of every action node (rule save path, after ``validate_actions``)."""
    for i, node in enumerate(graph.get("nodes", []) or []):
        if node.get("type") != "action":
            continue
        action = node.get("action") or {}
        definition = ACTIONS.get(action.get("type")) if isinstance(action.get("type"), str) else None  # type: ignore[arg-type]
        if definition is None or definition.check is None:
            continue
        params = definition.params_model.model_validate(action.get("params") or {})
        await definition.check(db, params, f"nodes[{i}].action.params")


_P1_ACTIONS: dict[str, ActionDef] = {
    "assign": ActionDef(
        "Assign recruiters / teams", AssignParams, _entity_types("demand"), _assign_run, _assign_dry
    ),
    "create_task": ActionDef("Create a task", CreateTaskParams, _any, _task_run, _task_dry),
    "notify": ActionDef("Send a notification", NotifyParams, _any, _notify_run, _notify_dry),
    "send_email": ActionDef(
        "Send a templated email", SendEmailParams, _validate_email, _email_run, _email_dry
    ),
    "move_stage": ActionDef("Move pipeline stage", MoveStageParams, _validate_move, _move_run, _move_dry),
    "add_tag": ActionDef(
        "Add candidate tags", AddTagParams, _entity_types("candidate", "application"), _tag_run, _tag_dry
    ),
    "refresh_matches": ActionDef(
        "Refresh matches",
        RefreshMatchesParams,
        _entity_types("demand", "application"),
        _refresh_run,
        _refresh_dry,
    ),
}


_actions: Registry[str, ActionDef] = Registry("workflow action")
for _kind, _definition in _P1_ACTIONS.items():
    _actions.register(_kind, _definition)

# Phase 1 name: a live read-only view of the (Epic 13) registry; ``MAX_ACTIONS`` is unchanged.
ACTIONS: Mapping[str, ActionDef] = _actions.view()


def register_action(key: str, definition: ActionDef) -> None:
    """Phase 2 domains add actions from ``hooks.py`` (never a human-only action, FR-65). Epic 32: a definition
    declaring a human-only transition (``catalogue.register_human_only_transitions``) is refused."""
    from app.services.workflow import catalogue

    for entity_type, target in definition.performs_transitions:
        if catalogue.is_human_only(entity_type, target):
            msg = f"action {key!r} performs the human-only transition {entity_type} -> {target}"
            raise ValueError(msg)
    _actions.register(key, definition)


def _invalid(path: str, reason: str) -> ValidationFailed:
    return ValidationFailed(
        f"Invalid action at {path}: {reason}", code="invalid_action", details={"path": path, "reason": reason}
    )


def validate_actions(actions: Any, entity_type: str) -> list[dict[str, Any]]:
    """Validate a rule's action list (closed registry, params, trigger compatibility); returns it normalised."""  # ruff: ignore[line-too-long]
    if actions is None:
        return []
    if not isinstance(actions, list):
        raise _invalid("actions", "expected a list")
    if len(actions) > MAX_ACTIONS:
        raise _invalid("actions", f"at most {MAX_ACTIONS} actions")
    out: list[dict[str, Any]] = []
    for i, action in enumerate(actions):
        path = f"actions[{i}]"
        if not isinstance(action, dict) or set(action) - {"type", "params"}:
            raise _invalid(path, "expected {type, params}")
        kind = action.get("type")
        definition = ACTIONS.get(kind) if isinstance(kind, str) else None
        if definition is None:
            raise _invalid(f"{path}.type", f"unknown action '{kind}'")
        try:
            params = definition.params_model.model_validate(action.get("params") or {})
        except ValidationError as exc:
            err = exc.errors()[0]
            loc = ".".join(str(p) for p in err.get("loc", ()))
            raise _invalid(
                f"{path}.params" + (f".{loc}" if loc else ""), str(err.get("msg", "invalid"))
            ) from exc
        definition.validate(params, entity_type, path)
        out.append({"type": kind, "params": params.model_dump(mode="json")})
    return out


def parse_params(action: Mapping[str, Any]) -> tuple[ActionDef, Any]:
    """Resolve a stored action; raises ``ActionError`` for anything outside the closed registry."""
    kind = action.get("type")
    definition = ACTIONS.get(kind) if isinstance(kind, str) else None
    if definition is None:
        raise ActionError("invalid_action", f"Unknown action '{kind}'")
    try:
        return definition, definition.params_model.model_validate(action.get("params") or {})
    except ValidationError as exc:
        raise ActionError("invalid_params", "The action parameters are invalid") from exc


def params_schemas() -> dict[str, dict[str, Any]]:
    return {
        k: {"label": d.label, "params_schema": d.params_model.model_json_schema()} for k, d in ACTIONS.items()
    }
