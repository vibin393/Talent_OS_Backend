"""AD-31: generic registries and the Phase 1 names converted to registry views (Epic 13)."""

from types import MappingProxyType

import pytest

from app.core.reconciliation import Finding, reconciliation_checks, register_reconciliation_check
from app.core.registry import Registry, RegistryError


def test_register_get_and_duplicates() -> None:
    reg: Registry[str, int] = Registry("thing")
    reg.register("a", 1)
    assert reg.get("a") == 1
    with pytest.raises(RegistryError, match="duplicate thing 'a'"):
        reg.register("a", 2)
    with pytest.raises(RegistryError, match="unknown thing 'zz'"):
        reg.get("zz")
    assert reg.find("zz") is None
    replaceable: Registry[str, int] = Registry("thing", allow_replace=True)
    replaceable.register("a", 1)
    replaceable.register("a", 2)
    assert replaceable.get("a") == 2


def test_view_is_live_and_read_only() -> None:
    reg: Registry[str, int] = Registry("thing")
    view = reg.view()
    assert isinstance(view, MappingProxyType)
    reg.register("b", 2)
    assert dict(view) == {"b": 2}
    with pytest.raises(TypeError):
        view["c"] = 3  # type: ignore[index]


def test_override_restores() -> None:
    reg: Registry[str, int] = Registry("thing")
    reg.register("a", 1)
    with reg.override("a", 9), reg.override("new", 5):
        assert reg.get("a") == 9 and reg.get("new") == 5
    assert reg.get("a") == 1 and reg.find("new") is None


def test_phase1_names_are_views_with_phase1_entries() -> None:
    from app.services import notifications_service, workflow

    assert isinstance(workflow.TRIGGERS, MappingProxyType)
    assert isinstance(workflow.ACTIONS, MappingProxyType)
    assert isinstance(workflow.FIELD_REGISTRY, MappingProxyType)
    assert isinstance(notifications_service.NOTIFICATION_TYPES, MappingProxyType)
    assert "demand.created" in workflow.TRIGGERS
    assert "notify" in workflow.ACTIONS
    assert "approval_requested" in notifications_service.NOTIFICATION_TYPES
    with pytest.raises(RegistryError):
        notifications_service.register_notification_type(
            "approval_requested", notifications_service.NOTIFICATION_TYPES["approval_requested"]
        )
    with pytest.raises(RegistryError):
        workflow.actions.register_action("notify", workflow.ACTIONS["notify"])


async def test_workflow_condition_registry() -> None:
    from app.core.context import RequestContext
    from app.services.workflow import registry as wf

    assert await wf.evaluate_condition(RequestContext(), None, "t13.unregistered") is None  # type: ignore[arg-type]

    async def matches(ctx: object, db: object, *, demand_id: str) -> bool:
        return demand_id == "yes"

    wf.register_condition("t13.matches", matches)
    assert await wf.evaluate_condition(RequestContext(), None, "t13.matches", demand_id="yes") is True  # type: ignore[arg-type]
    assert await wf.evaluate_condition(RequestContext(), None, "t13.matches", demand_id="no") is False  # type: ignore[arg-type]


def test_reconciliation_registry() -> None:
    async def check(ctx: object, db: object, on: object) -> list[Finding]:
        return []

    register_reconciliation_check("t13.recon", check)  # type: ignore[arg-type]
    assert reconciliation_checks()["t13.recon"] is check
    with pytest.raises(RegistryError):
        register_reconciliation_check("t13.recon", check)  # type: ignore[arg-type]


def test_placement_registries() -> None:
    from app.services import placements_service as p

    async def resolver(ctx: object, db: object, placement_id: object) -> list[p.GateState]:
        return [p.GateState("x", "met", True, {})]

    p.register_start_gate_kind("t13_gate", p.StartGateKindDef("Gate", overridable=True, resolver=resolver))  # type: ignore[arg-type]
    assert p.start_gate_kinds()["t13_gate"].overridable is True

    async def verifier(db: object, placement_id: object, last_day: object) -> p.VerifierResult:
        return p.VerifierResult(True, {})

    p.register_offboarding_verifier("t13_verifier", verifier)  # type: ignore[arg-type]
    assert "t13_verifier" in p.offboarding_verifiers()
    with pytest.raises(RegistryError):
        p.register_start_gate_kind("t13_gate", p.StartGateKindDef("Gate", overridable=False))


def test_retention_binding_registry() -> None:
    from app.services.privacy import retention_classes
    from app.services.privacy.registry import ErasureContext

    async def fn(*a: object, **k: object) -> None:
        return None

    retention_classes.register_retention_binding(
        "t13_table", "financial", subject_resolver=fn, anchor_resolver=fn, expire_fn=fn, pii_columns=("x",)
    )
    assert retention_classes.retention_bindings()["t13_table"].pii_columns == ("x",)
    import uuid

    ctx = ErasureContext(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), "dsr")
    assert ctx.retained == {}
