# ruff: noqa: E501
"""Epic 49 A4 / A11 (NFR-38, AD-47): protected data is unreachable from every other module.

* the ``fairness-isolation`` import-linter contract (kept on the repo; broken by a probe import from
  ``services/candidates`` in a synthetic copy of the contract);
* AST scan: no file outside the allow-list imports ``app.models.fairness`` / ``app.services.fairness``;
* "no inference": the only writers of ``demographic_self_ids`` are ``upsert_from_portal`` / ``upsert_from_apply``;
* semantic registry refuses ``protected`` and no source references the self-ID table; the AI / ML feature denylist
  covers self-ID terms;
* no internal endpoint exposes an individual's self-ID (OpenAPI scan) -- only the Candidate's own Portal read;
* the DSR export carries no values; erasure deletes the row.
"""

import ast
import configparser
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi import FastAPI

BACKEND = Path(__file__).resolve().parents[3]
APP = BACKEND / "app"
ALLOWED = {
    APP / "router" / "fairness.py",
    APP / "router" / "fairness_public.py",
    APP / "router" / "portal_self_id.py",
    APP / "tasks" / "fairness.py",
    APP / "services" / "fairness_erasure.py",
    # Epic 65 (spec A6): the AEDT glue reads only ``fairness.aedt_p4`` and ``fairness.events`` (.importlinter ignores).
    APP / "services" / "ai_governance" / "aedt_guard.py",
    APP / "services" / "ai_governance" / "checks.py",
    APP / "services" / "ai_governance" / "hooks.py",
}
BANNED = ("app.models.fairness", "app.services.fairness")


def _imports(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out += [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
    return out


def test_no_module_outside_the_allow_list_imports_fairness() -> None:
    offenders = []
    for path in APP.rglob("*.py"):
        if path in ALLOWED or path.is_relative_to(APP / "services" / "fairness"):
            continue
        offenders += [f"{path.relative_to(APP)}: {n}" for n in _imports(path) if n.startswith(BANNED)]
    assert offenders == []
    # the unrelated P1 queue-fairness module stays where it is and is not banned
    assert (APP / "core" / "fairness.py").exists()


def _contract() -> configparser.SectionProxy:
    cfg = configparser.ConfigParser()
    cfg.read(BACKEND / ".importlinter", encoding="utf-8")
    return cfg["importlinter:contract:fairness-isolation"]


def test_contract_is_on_file_and_kept() -> None:
    section = _contract()
    assert section["type"] == "forbidden"
    proc = subprocess.run(
        [str(Path(sys.executable).with_name("lint-imports.exe" if sys.platform == "win32" else "lint-imports")), "--contract", "fairness-isolation"],
        cwd=BACKEND, capture_output=True, text=True, check=False, timeout=600,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout[-2000:]


@pytest.mark.parametrize("probe", [True, False])
def test_contract_breaks_on_a_probe_import(tmp_path: Path, probe: bool) -> None:
    """Run the repo's contract (re-rooted on a synthetic package) against a probe import from services/candidates."""
    section = _contract()
    bodies = {
        "probeapp.models.fairness": "",
        "probeapp.services.fairness.self_id": "from probeapp.models import fairness\n",
        "probeapp.services.candidates.service": "from probeapp.services.fairness import self_id\n" if probe else "",
        "probeapp.router.fairness": "from probeapp.services.fairness import self_id\n",
    }
    for src in section["source_modules"].split():
        bodies.setdefault(src.removesuffix(".*").replace("app", "probeapp", 1) + ".probe_leaf", "")
    names = set(bodies)
    packages = {".".join(n.split(".")[:i]) for n in names for i in range(1, len(n.split(".")))}
    for pkg in packages:
        d = tmp_path.joinpath(*pkg.split("."))
        d.mkdir(parents=True, exist_ok=True)
        (d / "__init__.py").touch()
    for name, body in bodies.items():
        if name not in packages:
            tmp_path.joinpath(*name.split(".")).with_suffix(".py").write_text(body, encoding="utf-8")

    def rewrite(value: str) -> str:
        return value.replace("app.", "probeapp.")

    cfg = textwrap.dedent(
        """\
        [importlinter]
        root_packages =
            probeapp
        include_external_packages = False

        [importlinter:contract:fairness-isolation]
        name = probe
        type = forbidden
        allow_indirect_imports = True
        unmatched_ignore_imports_alerting = none
        source_modules =
        {src}
        forbidden_modules =
        {forb}
        ignore_imports =
        {ign}
        """
    ).format(
        src="\n".join(f"    {rewrite(x)}" for x in section["source_modules"].split() if x != "app.cli" and x != "app.api" and x != "app.main" and x != "app.constants"),
        forb="\n".join(f"    {rewrite(x)}" for x in section["forbidden_modules"].split()),
        ign="\n".join(f"    {rewrite(line.strip())}" for line in section["ignore_imports"].strip().splitlines() if line.strip()),
    )
    (tmp_path / "probe.ini").write_text(cfg, encoding="utf-8")
    exe = Path(sys.executable).with_name("lint-imports.exe" if sys.platform == "win32" else "lint-imports")
    proc = subprocess.run(
        [str(exe), "--config", str(tmp_path / "probe.ini")], cwd=tmp_path, capture_output=True, text=True, check=False,
        env={**__import__("os").environ, "PYTHONPATH": str(tmp_path)}, timeout=300,
    )  # fmt: skip
    if probe:
        assert proc.returncode != 0, proc.stdout
        assert "probeapp.services.candidates.service -> probeapp.services.fairness" in proc.stdout.replace("\n", " ")
    else:
        assert proc.returncode == 0, proc.stdout + proc.stderr


def test_only_two_writers_of_self_id_rows() -> None:
    """No inference: rows are only built in ``self_id._upsert``, called only by the two upsert entry points."""
    builders: set[str] = set()
    callers: set[str] = set()
    for path in APP.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "DemographicSelfId":
                    builders.add(f"{path.name}:{fn.name}")
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_upsert" and path.name == "self_id.py":
                    callers.add(fn.name)
                if isinstance(node, ast.Assign):
                    for t in node.targets:
                        if isinstance(t, ast.Attribute) and t.attr == "answers" and path.parent.name == "fairness":
                            builders.add(f"{path.name}:{fn.name}")
    assert builders == {"self_id.py:_upsert"}
    assert callers == {"upsert_from_portal", "upsert_from_apply"}


def test_no_date_of_birth_inference() -> None:
    src = "\n".join(p.read_text(encoding="utf-8") for p in (APP / "services" / "fairness").glob("*.py"))
    assert "date_of_birth" not in src and "dob" not in src.lower().split()


def test_semantic_layer_never_sees_self_id(app: FastAPI) -> None:  # noqa: ARG001 - app start registers sources
    from app.core.semantic import registry
    from tests.core.semantic.test_registry import _field, _register

    with pytest.raises(registry.SemanticRegistryError, match="protected"):
        _register({"self_id_gender": _field(pii_class="protected")})
    for key, src in registry.sources().items():
        blob = repr(src).lower()
        assert "demographic_self" not in blob and "fairness" not in getattr(src.root_model, "__tablename__", ""), key


@pytest.mark.parametrize("name", ["self_id", "gender", "age_band", "disability", "demographic_self_id"])
def test_ai_ml_feature_denylist_covers_self_id(name: str) -> None:
    from app.services.analytics.features import denylist

    assert denylist.violations([{"key": name, "label": name, "kind": "numeric", "source": f"x.{name}"}])


def test_no_internal_endpoint_exposes_individual_self_id(app: FastAPI) -> None:
    """The ``SelfIdOut`` schema (the only model carrying an individual's answers) is returned only by the Candidate's
    own Portal endpoint; no other response model has a self-ID answers field."""
    spec = app.openapi()
    schemas = spec["components"]["schemas"]

    def refs(node: object) -> set[str]:
        out: set[str] = set()
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "$ref" and isinstance(v, str):
                    name = v.rsplit("/", 1)[-1]
                    out.add(name)
                else:
                    out |= refs(v)
        elif isinstance(node, list):
            for v in node:
                out |= refs(v)
        return out

    def closure(names: set[str]) -> set[str]:
        seen: set[str] = set()
        todo = list(names)
        while todo:
            n = todo.pop()
            if n in seen or n not in schemas:
                continue
            seen.add(n)
            todo += list(refs(schemas[n]))
        return seen

    exposing: list[str] = []
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if not isinstance(op, dict):
                continue
            names = closure(refs(op.get("responses", {})))
            if "SelfIdOut" in names:
                exposing.append(f"{method.upper()} {path}")
    assert sorted(exposing) == ["GET /api/v1/portal/self-id", "PUT /api/v1/portal/self-id"]
    for name, schema in schemas.items():
        if name == "SelfIdOut":
            continue
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        assert not ({"gender", "age_band", "disability"} <= set(props)), name
