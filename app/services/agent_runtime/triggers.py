# ruff: noqa: E501
"""Agent triggers (Epic 53, story 53.9, FR-273).

* **Event triggers** are subscribed at post-discovery from every registered ``TriggerSpec(kind="event")``: the handler
  (after commit) computes the subject and dedup key and enqueues ``agents.start_from_event``. The same event delivered
  twice -> one run (``uq_agent_runs_dedup`` + the ``attach``/``drop`` rule).
* **Schedule triggers** are evaluated by the beat task ``agents.fire_schedules`` every minute (5-field UTC cron) with
  the dedup key ``sched:<trigger>:<minute>``.
* **Workflow action** ``agents.start_run`` ("Start agent run"): registered from ``hooks.py`` with a callable so
  ``workflow`` never imports Phase 4; dedup ``wf:<rule>:<execution>:<agent>``.
"""

import importlib
import pkgutil
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agentic.agents import TriggerSpec, agent_triggers, agents
from app.core.context import RequestContext
from app.core.events import subscribe

_wired: set[tuple[str, str]] = set()


def _event_class(trigger: TriggerSpec) -> type | None:
    import app.events as events_pkg

    modules = (
        [trigger.event_module]
        if trigger.event_module
        else [f"app.events.{m.name}" for m in pkgutil.iter_modules(events_pkg.__path__)]
    )
    for name in modules:
        try:
            module = importlib.import_module(name)  # type: ignore[arg-type]
        except ImportError:
            continue
        cls = getattr(module, trigger.event or "", None)
        if isinstance(cls, type):
            return cls
    return None


def _handler(agent_key: str, trigger: TriggerSpec) -> Callable[[Any], None]:
    def handle(event: Any) -> None:
        tenant_id = getattr(event, "tenant_id", None)
        if tenant_id is None:
            return
        try:
            subject = trigger.subject(event) if trigger.subject is not None else None
            dedup = (
                trigger.dedup_key(event)
                if trigger.dedup_key is not None
                else f"{trigger.event}:{subject[0]}:{subject[1]}"
                if subject
                else None
            )
        except Exception:
            logger.exception("Agent trigger {}.{} could not read its event", agent_key, trigger.key)
            return
        from app.tasks import agents as agent_tasks
        from app.tasks.runtime import enqueue

        enqueue(
            agent_tasks.start_from_event,
            str(tenant_id),
            agent_key,
            trigger.key,
            subject[0] if subject else None,
            str(subject[1]) if subject else None,
            dedup,
        )

    handle.__name__ = f"agent_trigger_{agent_key}_{trigger.key}"
    return handle


def wire_event_triggers() -> int:
    """Subscribe every registered event trigger once (post-discovery)."""
    wired = 0
    for key in agents():
        for trigger in agent_triggers(key):
            if trigger.kind != "event" or (key, trigger.key) in _wired:
                continue
            cls = _event_class(trigger)
            if cls is None:
                logger.warning(
                    "Agent trigger {}.{}: event class {} not found", key, trigger.key, trigger.event
                )
                continue
            subscribe(cls, _handler(key, trigger))
            _wired.add((key, trigger.key))
            wired += 1
    return wired


# ---------------------------------------------------------------- cron
def _field_matches(expr: str, value: int, low: int, high: int) -> bool:
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part in ("*", ""):
            start, end = low, high
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if start <= value <= end and (value - start) % step == 0:
            return True
    return False


def cron_matches(expr: str, at: datetime) -> bool:
    minute, hour, dom, month, dow = expr.split()
    return (
        _field_matches(minute, at.minute, 0, 59)
        and _field_matches(hour, at.hour, 0, 23)
        and _field_matches(dom, at.day, 1, 31)
        and _field_matches(month, at.month, 1, 12)
        and _field_matches(dow, (at.weekday() + 1) % 7, 0, 6)
    )


async def fire_schedules(ctx: RequestContext, db: AsyncSession, at: datetime) -> int:
    from app.services.agent_runtime.runs import start_run

    minute = at.strftime("%Y%m%d%H%M")
    started = 0
    for key, spec in agents().items():
        if spec.placeholder:
            continue
        for trigger in agent_triggers(key):
            if trigger.kind != "schedule" or not trigger.schedule or not cron_matches(trigger.schedule, at):
                continue
            ref = await start_run(
                ctx, db, agent_key=key, trigger_kind="schedule", trigger_key=trigger.key,
                dedup_key=f"sched:{trigger.key}:{minute}",
            )  # fmt: skip
            started += int(ref.run_id is not None and ref.reason is None)
    return started


# ---------------------------------------------------------------- workflow action
class StartRunParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_key: str = Field(min_length=2, max_length=40)


ACTION_KEY = "agents.start_run"


def register_workflow_action() -> None:
    from app.services.workflow.actions import ACTIONS, ActionDef, ActionError, register_action

    if ACTION_KEY in ACTIONS:
        return

    def _validate(params: Any, entity_type: str, path: str) -> None:  # noqa: ARG001
        from app.core.agentic.agents import get_agent
        from app.core.errors import ValidationFailed

        try:
            get_agent(params.agent_key)
        except KeyError as exc:
            raise ValidationFailed(
                f"Invalid action at {path}: unknown agent",
                code="invalid_action",
                details={"path": path, "reason": "unknown_agent"},
            ) from exc

    async def _run(ac: Any) -> str:
        from app.core.agentic.agents import get_agent
        from app.services.agent_runtime.runs import start_run

        spec = get_agent(ac.params.agent_key)
        ref = await start_run(
            ac.ctx, ac.db, agent_key=ac.params.agent_key, trigger_kind="workflow", trigger_key=None,
            subject=(ac.entity_type, ac.entity_id),
            owner_user_id=None if spec.owner is not None else ac.rule_created_by,
            dedup_key=f"wf:{ac.rule_id}:{ac.execution_id or uuid.uuid4()}:{ac.params.agent_key}",
        )  # fmt: skip
        if ref.run_id is None:
            raise ActionError(ref.reason or "not_started", f"The agent did not start ({ref.reason})")
        return f"Started agent run {ref.run_id}"

    async def _dry(ac: Any) -> str:
        return f"Would start the {ac.params.agent_key} agent on {ac.entity_type} {ac.entity_id}"

    register_action(ACTION_KEY, ActionDef("Start agent run", StartRunParams, _validate, _run, _dry))


__all__ = [
    "ACTION_KEY",
    "StartRunParams",
    "cron_matches",
    "fire_schedules",
    "register_workflow_action",
    "wire_event_triggers",
]
