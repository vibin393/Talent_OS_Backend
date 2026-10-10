"""Epic 53 (53.3 AC, spec 0.14): no registered agent tool can reach a human-only (★) transition.

* Every tool's declared ``transition`` must not be human-only (``workflow.catalogue.is_human_only``); ``forbidden`` is not
  a registrable risk class, and every documented ★ action (``FORBIDDEN_ACTIONS``) has no tool performing it.
* Every core tool handler executed under an ``agent`` actor leaves ``require_human`` either uncalled or raising
  (the eval corpus drives the handlers; this suite checks the static guarantees).
* ``PENDING_P4_STAR`` (shrink-only) lists owners' tools that cannot be exercised yet.
"""

import inspect

from app.core.agentic import FORBIDDEN_ACTIONS, ToolRegistryError, tools
from app.core.agentic.tools import register_tool
from app.core.hooks import autodiscover
from app.services.workflow import catalogue

PENDING_P4_STAR: dict[str, str] = {}


def test_no_tool_performs_a_human_only_transition() -> None:
    autodiscover("app.services")
    offenders = [
        spec.key
        for spec in tools().values()
        if spec.transition is not None and catalogue.is_human_only(*spec.transition)
    ]
    assert offenders == []
    performed = {spec.transition for spec in tools().values() if spec.transition is not None}
    assert not performed & set(FORBIDDEN_ACTIONS)


def test_forbidden_risk_cannot_be_registered() -> None:
    autodiscover("app.services")
    spec = next(iter(tools().values()))
    import dataclasses

    try:
        register_tool(dataclasses.replace(spec, key="zz.forbidden_probe", risk="forbidden"))
    except ToolRegistryError:
        return
    raise AssertionError("a forbidden tool was registered")


def test_core_tool_modules_never_import_star_modules() -> None:
    autodiscover("app.services")
    star = (
        "app.services.pipeline.submissions", "app.services.pipeline.feedback", "app.services.offers.status",
        "app.services.offers.response", "app.services.interviews.outcomes", "app.services.demands.lifecycle",
    )  # fmt: skip
    for spec in tools().values():
        module = inspect.getmodule(spec.handler)
        source = inspect.getsource(module) if module is not None else ""
        assert not any(name in source for name in star), spec.key
    assert all(key in tools() for key in PENDING_P4_STAR) or not PENDING_P4_STAR
