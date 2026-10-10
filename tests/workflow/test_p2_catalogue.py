# ruff: noqa: E501
"""Epic 32 story 19.4 (FR-167): the Phase 2 workflow catalogue is a binding contract.

Every trigger key is registered; condition fields come from the owning domain's snapshot loader. A key whose
loader / action is not registered yet must be listed in the pending dicts below with its owner epic -- and a
key that becomes registered must be removed (the test enforces both directions), in the pattern of
``tests/privacy/test_erasure_coverage.py``.
"""

import ast
from pathlib import Path

from app.core.hooks import autodiscover
from app.services import notifications_service
from app.services.workflow import catalogue, catalogue_p3
from app.services.workflow.actions import ACTIONS
from app.services.workflow.registry import TRIGGERS

# trigger key -> owner epic (entity snapshot loader not registered yet)
PENDING_TRIGGER_LOADERS: dict[str, str] = {
    "crm.lead_status_changed": "15",
    "crm.opportunity_stage_changed": "15",
    "placements.status_changed": "19",
    "placements.extension_status_changed": "19",
    "compliance.credential_expiring": "21",
    "compliance.credential_expired": "21",
    "billing.invoice_issued": "29",
    "receivables.invoice_overdue": "29",
    "receivables.promise_to_pay_broken": "29",
}
# action key -> owner epic (ActionDef not registered yet)
PENDING_ACTIONS: dict[str, str] = {
}

PHASE2_DOMAINS = (
    "legal_entities", "external_auth", "connectors", "commercials", "vendors", "placements", "compliance",
    "time", "postings", "billing", "payroll", "receivables", "documents", "crm", "bgv", "onboarding", "expenses",
    "client_portal", "reporting",
)  # fmt: skip


def _discover() -> None:
    autodiscover("app.services")


def test_every_trigger_key_is_registered() -> None:
    _discover()
    keys = {spec.key for spec in catalogue.TRIGGER_CATALOGUE} | set(catalogue.SLA_TRIGGER_KEYS)
    assert keys <= set(TRIGGERS), sorted(keys - set(TRIGGERS))
    for spec in catalogue.TRIGGER_CATALOGUE:
        assert spec.key.split(".", 1)[0] and "." in spec.key


def test_trigger_loader_coverage_and_pending_list() -> None:
    _discover()
    loaders = catalogue.snapshot_loaders()
    for spec in catalogue.TRIGGER_CATALOGUE:
        has_loader = spec.entity_type in loaders
        if spec.key in PENDING_TRIGGER_LOADERS:
            assert not has_loader, (
                f"{spec.key} now has a snapshot loader: remove it from PENDING_TRIGGER_LOADERS"
            )
        else:
            assert has_loader, f"{spec.key} ({spec.entity_type}) has no snapshot loader and is not pending"
    assert set(PENDING_TRIGGER_LOADERS) <= {s.key for s in catalogue.TRIGGER_CATALOGUE}
    # the SLA triggers of this epic come with their loaders
    assert {"sla_clock", "escalation_watch"} <= set(loaders)


def test_action_contract_and_pending_list() -> None:
    _discover()
    assert set(PENDING_ACTIONS) <= set(catalogue.ACTION_CATALOGUE)
    for key, owner in catalogue.ACTION_CATALOGUE.items():
        if key in catalogue_p3.P3_ACTION_CATALOGUE:
            continue  # Phase 3 keys: tests/workflow/test_p3_catalogue.py
        if key in PENDING_ACTIONS:
            assert PENDING_ACTIONS[key] == owner
            assert key not in ACTIONS, f"{key} is registered now: remove it from PENDING_ACTIONS"
        else:
            assert key in ACTIONS, f"{key} is neither registered nor pending"


def test_notification_types_registered() -> None:
    _discover()
    for key in catalogue.NOTIFICATION_KEYS:
        assert key in notifications_service.NOTIFICATION_TYPES, key
    assert notifications_service.NOTIFICATION_TYPES["sla_clock_breached"].mandatory_in_app is True


def test_workflow_and_sla_import_no_phase2_domain() -> None:
    """AD-32: ``services/workflow`` and ``services/sla`` reach Phase 2 domains only through registries."""
    import app.services.sla as sla_pkg
    import app.services.workflow as wf_pkg

    forbidden = {f"app.services.{d}" for d in PHASE2_DOMAINS} | {
        f"app.services.{d}_service" for d in PHASE2_DOMAINS
    }
    for package in (wf_pkg, sla_pkg):
        for path in Path(package.__file__).resolve().parent.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                modules: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
                elif isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                for module in modules:
                    assert not any(module == f or module.startswith(f + ".") for f in forbidden), (
                        f"{path.name}: {module}"
                    )
