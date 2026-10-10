# ruff: noqa: E501
"""Epic 48 (A9): numpy and scikit-learn are imported only under ``app/services/analytics/models/`` (AST scan of the
whole analytics surface: the package, its router, tasks and erasure handler)."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[3] / "app"
MODELS_DIR = APP / "services" / "analytics" / "models"
SURFACE = [
    *(APP / "services" / "analytics").rglob("*.py"),
    APP / "router" / "analytics.py",
    APP / "tasks" / "analytics.py",
    APP / "services" / "analytics_erasure.py",
    APP / "schemas" / "analytics.py",
]
BANNED = ("numpy", "sklearn", "scipy")


def _imports(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.append(node.module)
    return out


def test_numpy_and_sklearn_only_in_models_package() -> None:
    offenders = []
    for path in SURFACE:
        if path.is_relative_to(MODELS_DIR):
            continue
        offenders += [f"{path.name}: {name}" for name in _imports(path) if name.split(".")[0] in BANNED]
    assert offenders == []


def test_models_package_does_use_them() -> None:
    used = {name.split(".")[0] for path in MODELS_DIR.glob("*.py") for name in _imports(path)}
    assert {"numpy", "sklearn"} <= used


def test_no_domain_imports_analytics() -> None:
    """``analytics`` is a top-layer reader (AD-40): no other service imports it."""
    offenders = []
    for path in (APP / "services").rglob("*.py"):
        if path.is_relative_to(APP / "services" / "analytics") or path.name == "analytics_erasure.py":
            continue
        offenders += [f"{path}: {n}" for n in _imports(path) if n.startswith("app.services.analytics")]
    assert offenders == []
