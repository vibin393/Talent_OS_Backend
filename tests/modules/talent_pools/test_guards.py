"""Epic 38 boundaries: no raw SQL in segments / talent_pools; matching and candidates never import them."""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[3] / "app"
OWNED = [APP / "services" / "segments", APP / "services" / "talent_pools"]


def _py(paths: list[Path]) -> list[Path]:
    return [f for p in paths for f in sorted(p.rglob("*.py"))]


def test_no_text_with_non_literal_argument() -> None:
    offenders = []
    for f in _py(OWNED):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) in {
                "text",
                "literal_column",
            }:
                if not all(isinstance(a, ast.Constant) for a in node.args):
                    offenders.append(f"{f.name}:{node.lineno}")
    assert offenders == []


def test_matching_and_candidates_never_import_pools_or_segments() -> None:
    sources = _py([APP / "services" / "matching", APP / "services" / "candidates"])
    bad = []
    for f in sources:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            if any(
                m.startswith(("app.services.talent_pools", "app.services.segments", "app.services.pools_service"))
                for m in mods
            ):
                bad.append(f.name)
    assert bad == []
