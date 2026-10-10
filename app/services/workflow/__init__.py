"""Workflow rules service package (Epic 10, story 10.4; FR-58, FR-59, FR-65).

Facade for routers, tasks and other services:

* ``registry`` -- ``TRIGGERS``, ``FIELD_REGISTRY`` and the event subscriptions (registered at import);
* ``conditions`` -- the closed JSON condition grammar (``validate`` / ``evaluate``);
* ``actions`` -- the closed ``ACTIONS`` registry (no human-only actions exist, FR-65);
* ``engine`` -- live runs (``evaluate_event``) and dry runs;
* ``rules`` -- administration (CRUD, activation, executions, dry runs, builder registry).
"""

from app.services.workflow import actions, conditions, engine, registry, rules
from app.services.workflow.actions import ACTIONS
from app.services.workflow.engine import evaluate_event
from app.services.workflow.registry import FIELD_REGISTRY, TRIGGERS
from app.services.workflow.rules import (
    archive_rule,
    create_rule,
    dry_run_draft,
    dry_run_rule,
    get_execution,
    get_rule,
    list_executions,
    list_rules,
    purge_executions,
    set_active,
    update_rule,
)

registry.register()

__all__ = [
    "ACTIONS",
    "FIELD_REGISTRY",
    "TRIGGERS",
    "actions",
    "archive_rule",
    "conditions",
    "create_rule",
    "dry_run_draft",
    "dry_run_rule",
    "engine",
    "evaluate_event",
    "get_execution",
    "get_rule",
    "list_executions",
    "list_rules",
    "purge_executions",
    "registry",
    "rules",
    "set_active",
    "update_rule",
]
