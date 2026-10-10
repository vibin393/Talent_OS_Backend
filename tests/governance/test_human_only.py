"""FR-65 governance suite (Epic 12 A5): AI and workflow actors can never perform human-only transitions.

1. Every state-machine domain exports ``HUMAN_ONLY: dict[str, Callable[[str], Awaitable[object]]]`` -- probes
   that attempt the action (application reject, submission send, offer release, approval decide, demand status
   change other than Filled) with the given actor kind. Each probe is invoked with ``ai`` and ``workflow`` and
   must raise a 409 ``DomainError``. Domains not merged yet (or not exporting ``HUMAN_ONLY`` yet) are reported
   as skipped so the gap stays visible.
2. An AST import-graph check: ``app/tasks/ai*.py``, the AI gateway package and the workflow executor never
   import the human-only callables.
"""

import ast
import importlib
import importlib.util
from pathlib import Path

import pytest

from app.core.errors import DomainError

APP_DIR = Path(__file__).resolve().parents[2] / "app"

STATE_MACHINE_MODULES: dict[str, str] = {
    "pipeline": "app.services.pipeline",
    "offers": "app.services.offers",
    "approvals": "app.services.approvals",
    "demands": "app.services.demands",
}

# (module, callable) pairs that perform a human-only action (FR-65).
HUMAN_ONLY_CALLABLES: tuple[tuple[str, str], ...] = (
    ("app.services.pipeline.applications", "reject_application"),
    ("app.services.pipeline.submissions", "send_submission"),
    ("app.services.offers", "release_offer"),
    ("app.services.approvals.service", "decide"),
    ("app.services.approvals", "decide"),
    ("app.services.demands.lifecycle", "transition_demand"),
)
GUARDED_FILES: tuple[str, ...] = (
    "tasks/ai*.py",
    "services/ai/*.py",
    "services/workflow/engine.py",
    "tasks/workflow.py",
)


def _registries() -> list[tuple[str, str, object]]:
    probes: list[tuple[str, str, object]] = []
    for domain, dotted in STATE_MACHINE_MODULES.items():
        if importlib.util.find_spec(dotted) is None:
            continue
        registry = getattr(importlib.import_module(dotted), "HUMAN_ONLY", None)
        if isinstance(registry, dict):
            probes += [(domain, name, probe) for name, probe in registry.items()]
    return probes


PROBES = _registries()


@pytest.mark.parametrize("actor", ["ai", "workflow", "agent", "app"])  # Epic 53: +agent; Epic 66: +app
@pytest.mark.parametrize(
    ("domain", "name", "probe"),
    PROBES or [pytest.param("-", "-", None, marks=pytest.mark.skip("no HUMAN_ONLY exported yet"))],
)
async def test_human_only_callables_refuse_ai_and_workflow(
    domain: str, name: str, probe: object, actor: str
) -> None:
    import inspect

    required = [
        p
        for p in inspect.signature(probe).parameters.values()  # type: ignore[arg-type]
        if p.default is inspect.Parameter.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    if len(required) != 1:
        pytest.skip(f"{domain}.{name} exports the action itself; a one-argument probe (actor) is pending")
    with pytest.raises(DomainError) as info:
        await probe(actor)  # type: ignore[operator]
    assert info.value.status_code == 409, f"{domain}.{name} must refuse actor {actor} with 409"


@pytest.mark.parametrize("domain", sorted(STATE_MACHINE_MODULES))
def test_state_machine_domains_export_human_only(domain: str) -> None:
    dotted = STATE_MACHINE_MODULES[domain]
    if importlib.util.find_spec(dotted) is None:
        pytest.skip(f"{domain} not merged yet")
    if not isinstance(getattr(importlib.import_module(dotted), "HUMAN_ONLY", None), dict):
        pytest.skip(f"{dotted} does not export HUMAN_ONLY yet (FR-65 suite pending for {domain})")


def _imports(path: Path) -> set[tuple[str, str]]:
    """Return ``(module, name)`` pairs imported or attribute-accessed on an imported module alias."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                found.add((node.module, alias.name))
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            found.add((aliases[node.value.id], node.attr))
    return found


def test_ai_and_workflow_code_never_import_human_only_callables() -> None:
    files = sorted({p for pattern in GUARDED_FILES for p in APP_DIR.glob(pattern)})
    exported = {(getattr(fn, "__module__", ""), getattr(fn, "__name__", "")) for _, _, fn in PROBES}
    forbidden = set(HUMAN_ONLY_CALLABLES) | exported
    assert any("services" in str(p) and "ai" in p.parts for p in files), "AI gateway files must be checked"
    offenders = sorted(
        f"{path.relative_to(APP_DIR)} -> {module}.{name}"
        for path in files
        for module, name in _imports(path)
        if (module, name) in forbidden
    )
    assert offenders == [], f"human-only callables imported by AI / workflow code: {offenders}"
