# ruff: noqa: D401, E501
"""Workflow v2 graphs (Epic 32, story 19.3, FR-169, AD-31): schema, save-time validation, Phase 1 <-> graph
conversion and the human-readable outline (the form-based equivalent of the canvas).

    {"start": "n1", "nodes": [{"id": "n1", "type": "condition", "when": {...}, "then": "n2", "else": "n5"},
     {"id": "n2", "type": "delay", "duration": {"value": 2, "unit": "business_days"}, "next": "n3"},
     {"id": "n3", "type": "approval", "title": "...", "approver": {"kind": "role", "ref": "<role id>"},
      "escalation_policy_id": null, "timeout": {...}, "on_approved": "n4", "on_rejected": "n5", "on_timeout": "n5"},
     {"id": "n4", "type": "action", "action": {"type": "create_task", "params": {...}}, "next": null},
     {"id": "n5", "type": "end"}], "layout": {"n1": {"x": 0, "y": 0}}}

Validation (422 ``invalid_graph`` with ``details.path`` / ``details.reason``): acyclic, every node reachable from
``start``, <= ``WORKFLOW_GRAPH_MAX_STEPS`` nodes excluding ``end``, <= ``MAX_ACTIONS`` action nodes on any path,
delays / timeouts within ``WORKFLOW_MAX_DELAY_DAYS`` (minutes >= 5), approval nodes with a resolvable approver kind
and an ``on_approved`` branch. Conditions use the P1 grammar (``invalid_condition``); actions the closed registry
plus the human-only guard (``invalid_action``, reason ``human_only_target``). ``layout`` is cosmetic.

``from_p1`` derives a linear graph from a P1 rule at read time (no data migration); ``to_p1`` returns the P1 form
for graphs it can express (linear, no delay / approval, at most one condition at the head) -- such graphs are
stored in P1 form and keep running on the unchanged P1 engine.
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.core.errors import ValidationFailed
from app.core.settings import get_settings
from app.services.sla import durations
from app.services.workflow import conditions
from app.services.workflow.actions import ACTIONS, MAX_ACTIONS, validate_actions
from app.services.workflow.registry import TRIGGERS, fields_for

NODE_TYPES = ("condition", "delay", "approval", "action", "end")
DELAY_UNITS = durations.UNITS
APPROVER_KINDS = ("user", "role", "entity_owner", "primary_recruiter")
MIN_DELAY_MINUTES = 5
NodeId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class Duration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Annotated[int, Field(ge=1, le=200000)]
    unit: Literal["minutes", "hours", "days", "business_hours", "business_days"]


class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: NodeId


class ConditionNode(_Node):
    type: Literal["condition"]
    when: dict[str, Any] = Field(default_factory=dict)
    then: NodeId | None = None
    else_: NodeId | None = Field(default=None, alias="else")


class DelayNode(_Node):
    type: Literal["delay"]
    duration: Duration
    next: NodeId | None = None


class Approver(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["user", "role", "entity_owner", "primary_recruiter"]
    ref: str | None = None


class ApprovalNode(_Node):
    type: Literal["approval"]
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    approver: Approver
    escalation_policy_id: uuid.UUID | None = None
    timeout: Duration | None = None
    on_approved: NodeId | None = None
    on_rejected: NodeId | None = None
    on_timeout: NodeId | None = None


class ActionNode(_Node):
    type: Literal["action"]
    action: dict[str, Any]
    next: NodeId | None = None


class EndNode(_Node):
    type: Literal["end"]


Node = Annotated[ConditionNode | DelayNode | ApprovalNode | ActionNode | EndNode, Field(discriminator="type")]


class Graph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: NodeId
    nodes: Annotated[list[Node], Field(min_length=1, max_length=100)]
    layout: dict[str, dict[str, float]] = Field(default_factory=dict)


# ================================================================ helpers
EDGE_FIELDS: dict[str, tuple[str, ...]] = {
    "condition": ("then", "else"),
    "delay": ("next",),
    "approval": ("on_approved", "on_rejected", "on_timeout"),
    "action": ("next",),
    "end": (),
}


def edges(node: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(field, target id) of every outgoing edge of a normalized node."""
    return [(f, str(node[f])) for f in EDGE_FIELDS[str(node["type"])] if node.get(f)]


def by_id(graph: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(n["id"]): n for n in graph.get("nodes", [])}


def invalid(path: str, reason: str, message: str | None = None) -> ValidationFailed:
    return ValidationFailed(
        message or f"Invalid workflow graph at {path}: {reason}",
        code="invalid_graph",
        details={"path": path, "reason": reason},
    )


def _normalize(model: Graph) -> dict[str, Any]:
    out = model.model_dump(mode="json", by_alias=True)
    for node in out["nodes"]:
        if node["type"] == "approval" and node.get("timeout") is None:
            node["on_timeout"] = node.get("on_timeout")
    return out


def _parse(raw: Any) -> Graph:
    if not isinstance(raw, dict):
        raise invalid("graph", "expected_object", "The graph must be a JSON object")
    try:
        return Graph.model_validate(raw)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = [str(p) for p in err.get("loc", ())]
        path = ""
        for part in loc:
            if part.isdigit():
                path += f"[{part}]"
            elif part in NODE_TYPES and path.endswith("]"):
                continue  # discriminator tag
            else:
                path += f".{part}" if path else part
        raise invalid(
            path or "graph", "schema", f"Invalid workflow graph at {path}: {err.get('msg')}"
        ) from exc


def _duration_ok(duration: Mapping[str, Any] | None, path: str) -> None:
    if not duration:
        return
    value, unit = float(duration["value"]), str(duration["unit"])
    if unit == "minutes" and value < MIN_DELAY_MINUTES:
        raise invalid(path, "delay_too_short", f"Delays are at least {MIN_DELAY_MINUTES} minutes")
    limit = get_settings().workflow_max_delay_days
    if durations.max_days(value, unit) > limit:
        raise invalid(path, "delay_too_long", f"Delays are at most {limit} days")


# ---------------------------------------------------------------- human-only guard (FR-181)
def human_only_violation(action: Mapping[str, Any]) -> str | None:
    """Reason when an action would perform a human-only transition, else None."""
    from app.services.workflow import catalogue
    from app.services.workflow.actions import workflow_allowed_target

    kind = action.get("type")
    params = action.get("params") or {}
    if kind == "move_stage" and not workflow_allowed_target(str(params.get("to_stage_key") or "")):
        return "human_only_target"
    definition = ACTIONS.get(kind) if isinstance(kind, str) else None
    for entity_type, target in getattr(definition, "performs_transitions", ()) or ():
        if catalogue.is_human_only(entity_type, target):
            return "human_only_target"
    return None


def _validate_action(action: Any, entity_type: str, path: str) -> dict[str, Any]:
    if isinstance(action, dict) and (reason := human_only_violation(action)):
        raise ValidationFailed(
            f"Invalid action at {path}: workflow rules cannot perform human-only transitions",
            code="invalid_action",
            details={"path": path, "reason": reason},
        )
    try:
        [normalized] = validate_actions([action], entity_type)
    except ValidationFailed as exc:
        details = dict(exc.details or {})
        inner = str(details.get("path", "actions[0]"))
        details["path"] = path + inner.removeprefix("actions[0]")
        raise ValidationFailed(
            exc.message.replace("actions[0]", path), code=exc.code, details=details
        ) from exc
    return normalized


# ================================================================ validation
def validate_graph(raw: Any, trigger: str) -> dict[str, Any]:
    """Validate a graph for ``trigger``; returns its normalized JSON (raises 422)."""
    model = _parse(raw)
    graph = _normalize(model)
    nodes: list[dict[str, Any]] = graph["nodes"]
    index: dict[str, int] = {}
    for i, node in enumerate(nodes):
        if node["id"] in index:
            raise invalid(f"nodes[{i}].id", "duplicate_id")
        index[node["id"]] = i
    if graph["start"] not in index:
        raise invalid("start", "unknown_node")
    max_steps = get_settings().workflow_graph_max_steps
    steps = sum(1 for n in nodes if n["type"] != "end")
    if steps > max_steps:
        raise invalid("nodes", "too_many_steps", f"A workflow has at most {max_steps} steps")
    for i, node in enumerate(nodes):
        for field, target in edges(node):
            if target not in index:
                raise invalid(f"nodes[{i}].{field}", "unknown_node")

    # acyclic (DFS, colours) and reachable from start
    state: dict[str, int] = {}

    def visit(node_id: str) -> None:
        state[node_id] = 1
        node = nodes[index[node_id]]
        for field, target in edges(node):
            if state.get(target) == 1:
                raise invalid(f"nodes[{index[node_id]}].{field}", "cycle", "The workflow graph has a cycle")
            if target not in state:
                visit(target)
        state[node_id] = 2

    visit(graph["start"])
    for i, node in enumerate(nodes):
        if node["id"] not in state:
            raise invalid(
                f"nodes[{i}]", "unreachable", f"Node '{node['id']}' is not reachable from the start"
            )

    # at most MAX_ACTIONS action nodes on any path
    memo: dict[str, int] = {}

    def actions_on_path(node_id: str) -> int:
        if node_id not in memo:
            node = nodes[index[node_id]]
            below = max((actions_on_path(t) for _, t in edges(node)), default=0)
            memo[node_id] = below + (1 if node["type"] == "action" else 0)
        return memo[node_id]

    if actions_on_path(graph["start"]) > MAX_ACTIONS:
        raise invalid("nodes", "too_many_actions", f"At most {MAX_ACTIONS} actions on any path")

    if trigger not in TRIGGERS:
        raise ValidationFailed(
            f"Unknown trigger '{trigger}'",
            code="invalid_trigger",
            details={"path": "trigger", "reason": "unknown"},
        )
    entity_type = TRIGGERS[trigger].entity_type
    fields = fields_for(trigger)
    for i, node in enumerate(nodes):
        path = f"nodes[{i}]"
        kind = node["type"]
        if kind == "condition":
            node["when"] = conditions.validate(node.get("when") or {}, fields, path=f"{path}.when")
        elif kind == "delay":
            _duration_ok(node["duration"], f"{path}.duration")
        elif kind == "approval":
            approver = node["approver"]
            if approver["kind"] in ("user", "role"):
                try:
                    uuid.UUID(str(approver.get("ref") or ""))
                except ValueError as exc:
                    raise invalid(
                        f"{path}.approver.ref", "approver_unresolved", "Choose the approver"
                    ) from exc
            else:
                approver["ref"] = None
            if not node.get("on_approved"):
                raise invalid(
                    f"{path}.on_approved", "missing_branch", "An approval needs an 'approved' branch"
                )
            _duration_ok(node.get("timeout"), f"{path}.timeout")
            if node.get("timeout") is None and node.get("on_timeout"):
                raise invalid(
                    f"{path}.on_timeout", "timeout_missing", "Set a timeout for the 'timed out' branch"
                )
        elif kind == "action":
            node["action"] = _validate_action(node["action"], entity_type, f"{path}.action")
    return graph


def escalation_policy_ids(graph: Mapping[str, Any]) -> list[tuple[str, uuid.UUID]]:
    """(path, policy id) of every approval node / create_task action naming an escalation policy."""
    out: list[tuple[str, uuid.UUID]] = []
    for i, node in enumerate(graph.get("nodes", [])):
        if node.get("type") == "approval" and node.get("escalation_policy_id"):
            out.append((f"nodes[{i}].escalation_policy_id", uuid.UUID(str(node["escalation_policy_id"]))))
        if node.get("type") == "action":
            params = (node.get("action") or {}).get("params") or {}
            if params.get("escalation_policy_id"):
                out.append(
                    (
                        f"nodes[{i}].action.params.escalation_policy_id",
                        uuid.UUID(str(params["escalation_policy_id"])),
                    )
                )
    return out


def step_count(graph: Mapping[str, Any]) -> int:
    return sum(1 for n in graph.get("nodes", []) if n.get("type") != "end")


# ================================================================ P1 <-> graph
def from_p1(cond: Mapping[str, Any] | None, actions: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """The linear graph of a Phase 1 rule (derived at read time; nothing is stored)."""
    nodes: list[dict[str, Any]] = []
    action_ids = [f"a{i + 1}" for i in range(len(actions or []))]
    first_action = action_ids[0] if action_ids else "end"
    if cond:
        nodes.append(
            {"id": "c1", "type": "condition", "when": dict(cond), "then": first_action, "else": "end"}
        )
    for i, action in enumerate(actions or []):
        nxt = action_ids[i + 1] if i + 1 < len(action_ids) else "end"
        nodes.append({"id": action_ids[i], "type": "action", "action": dict(action), "next": nxt})
    nodes.append({"id": "end", "type": "end"})
    start = "c1" if cond else first_action
    layout = {n["id"]: {"x": 0.0, "y": float(140 * i)} for i, n in enumerate(nodes)}
    return {"start": start, "nodes": nodes, "layout": layout}


def to_p1(graph: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """(conditions, actions) when the graph is expressible as a P1 rule, else None."""
    nodes = by_id(graph)
    seen: set[str] = set()
    node = nodes.get(str(graph.get("start")))
    cond: dict[str, Any] = {}
    if node is not None and node["type"] == "condition":
        seen.add(node["id"])
        other = nodes.get(str(node.get("else"))) if node.get("else") else None
        if other is not None and other["type"] != "end":
            return None
        cond = dict(node.get("when") or {})
        node = nodes.get(str(node.get("then"))) if node.get("then") else None
    actions: list[dict[str, Any]] = []
    while node is not None and node["type"] == "action":
        seen.add(node["id"])
        actions.append(dict(node["action"]))
        node = nodes.get(str(node.get("next"))) if node.get("next") else None
    if node is not None and node["type"] != "end":
        return None
    if {i for i, n in nodes.items() if n["type"] != "end"} - seen:
        return None
    if len(actions) > MAX_ACTIONS:
        return None
    return cond, actions


# ================================================================ outline (FR-169)
_OP_TEXT = {
    "eq": "is", "ne": "is not", "in": "is one of", "not_in": "is not one of", "gt": ">", "gte": ">=", "lt": "<",
    "lte": "<=", "contains": "contains", "not_contains": "does not contain", "is_empty": "is empty",
    "is_not_empty": "is not empty", "starts_with": "starts with",
}  # fmt: skip


def condition_text(cond: Any, labels: Mapping[str, str] | None = None) -> str:
    labels = labels or {}
    if not cond:
        return "always"
    if "all" in cond or "any" in cond:
        key = "all" if "all" in cond else "any"
        parts = [condition_text(c, labels) for c in cond[key]]
        joined = f" {'and' if key == 'all' else 'or'} ".join(parts)
        return f"({joined})" if len(parts) > 1 else joined
    if "not" in cond:
        return f"not {condition_text(cond['not'], labels)}"
    field = labels.get(str(cond.get("field")), str(cond.get("field")))
    op = _OP_TEXT.get(str(cond.get("op")), str(cond.get("op")))
    if cond.get("op") in ("is_empty", "is_not_empty"):
        return f"{field} {op}"
    value = cond.get("value")
    shown = ", ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    return f"{field} {op} {shown}"


def _approver_text(approver: Mapping[str, Any]) -> str:
    kind = approver.get("kind")
    return {
        "user": "a named user",
        "role": "a role",
        "entity_owner": "the record owner",
        "primary_recruiter": "the primary recruiter",
    }.get(str(kind), str(kind))


def _action_text(action: Mapping[str, Any]) -> str:
    kind = str(action.get("type"))
    definition = ACTIONS.get(kind)
    label = definition.label if definition else kind
    params = action.get("params") or {}
    if kind == "create_task" and params.get("title"):
        return f"{label}: '{params['title']}'"
    if kind == "notify" and params.get("message"):
        return f"{label}: '{params['message']}'"
    return label


def node_text(node: Mapping[str, Any], labels: Mapping[str, str] | None = None) -> str:
    kind = node["type"]
    if kind == "condition":
        return f"If {condition_text(node.get('when'), labels)}"
    if kind == "delay":
        d = node["duration"]
        return f"Wait {durations.describe(d['value'], d['unit'])}"
    if kind == "approval":
        text = f"Ask {_approver_text(node['approver'])} to approve '{node.get('title')}'"
        if node.get("timeout"):
            t = node["timeout"]
            text += f" (times out after {durations.describe(t['value'], t['unit'])})"
        return text
    if kind == "action":
        return _action_text(node["action"])
    return "End"


_BRANCH_TEXT = {
    "then": "Yes",
    "else": "No",
    "on_approved": "If approved",
    "on_rejected": "If rejected",
    "on_timeout": "If timed out",
}


def outline(graph: Mapping[str, Any], trigger: str | None = None) -> list[dict[str, Any]]:
    """Indented, human-readable rows ``{depth, node_id, kind, text}`` (read-only; the accessible alternative
    to the canvas)."""
    labels: dict[str, str] = {}
    if trigger and trigger in TRIGGERS:
        labels = {path: f.label for path, f in fields_for(trigger).items()}
    nodes = by_id(graph)
    rows: list[dict[str, Any]] = []
    printed: set[str] = set()

    def walk(node_id: str | None, depth: int) -> None:
        while node_id:
            node = nodes.get(node_id)
            if node is None:
                return
            if node["type"] == "end":
                if depth == 0:
                    rows.append({"depth": depth, "node_id": node_id, "kind": "end", "text": "End"})
                return
            if node_id in printed:
                rows.append(
                    {
                        "depth": depth,
                        "node_id": node_id,
                        "kind": "goto",
                        "text": f"Continue at: {node_text(node, labels)}",
                    }
                )
                return
            printed.add(node_id)
            rows.append(
                {"depth": depth, "node_id": node_id, "kind": node["type"], "text": node_text(node, labels)}
            )
            if node["type"] in ("condition", "approval"):
                for field in EDGE_FIELDS[node["type"]]:
                    target = node.get(field)
                    if node["type"] == "approval" and field == "on_timeout" and not node.get("timeout"):
                        continue
                    rows.append(
                        {
                            "depth": depth + 1,
                            "node_id": node_id,
                            "kind": "branch",
                            "text": f"{_BRANCH_TEXT[field]}:",
                        }
                    )
                    if target and nodes.get(str(target), {}).get("type") != "end":
                        walk(str(target), depth + 2)
                    else:
                        rows.append(
                            {
                                "depth": depth + 2,
                                "node_id": str(target or node_id),
                                "kind": "end",
                                "text": "End",
                            }
                        )
                return
            node_id = node.get("next")

    walk(str(graph.get("start")), 0)
    return rows
