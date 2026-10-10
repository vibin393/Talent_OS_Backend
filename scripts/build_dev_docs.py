# ruff: noqa: E501
"""Developer docs bundle generator (Epic 74, story 74.2, spec 0.7).

Imports the app's contracts (no DB) and writes ``Frontend/src/app/developers/_generated/docs-bundle.json``:
the pinned public OpenAPI (67) + changelog + deprecations, the public event catalogue, the scope catalogue, the MCP
exposures (73) with the tool title / description / execution class / input schema, the connector manifest schema and
ports (69), the rate tiers (66) and the signing vectors + verifier samples.

    python scripts/build_dev_docs.py            # write the bundle
    python scripts/build_dev_docs.py --check    # fail when the committed bundle is stale or coverage is incomplete
"""

import json
import sys
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
BUNDLE = BACKEND.parent / "Frontend" / "src" / "app" / "developers" / "_generated" / "docs-bundle.json"
VERIFIERS = BACKEND / "tests" / "webhooks" / "fixtures" / "verifiers"
# Undocumented items that predate the coverage gate (Epic 67 pinned operations without a description). Shrink-only:
# anything new that lacks documentation fails the gate. See docs_baseline.json next to this script.
BASELINE = BACKEND / "scripts" / "docs_baseline.json"


def _execution_class(spec: Any, exposure: Any) -> str:
    scope = str(getattr(exposure, "mcp_scope", "") or "")
    if scope.endswith("propose"):
        return "proposes"
    if scope.endswith("draft") or getattr(spec, "egress", False):
        return "creates"
    risk = str(getattr(spec, "risk", "read"))
    return "reads" if "read" in risk.lower() else "creates"


def build() -> dict[str, Any]:
    import yaml

    from app.core import hooks

    hooks.autodiscover("app.services")
    import importlib

    from app.core import public_api_deprecations, public_events, rate_tiers

    exposure = importlib.import_module("app.core.agentic.exposure")
    tools = importlib.import_module("app.core.agentic.tools")
    from app.core.oauth_scopes import scopes
    from app.core.settings import get_settings
    from app.services.marketplace import sdk

    openapi = json.loads((BACKEND / "openapi-public-v1.json").read_text(encoding="utf-8"))
    changelog = yaml.safe_load((BACKEND / "app" / "core" / "public_api_changelog.yaml").read_text(encoding="utf-8"))
    events = []
    for key, spec in sorted(public_events.catalogue().items()):
        events.append({
            "type": key, "version": spec.version, "subject_type": spec.subject_type, "scopes": list(spec.scopes),
            "description": spec.description, "example": dict(spec.example or {}), "schema": spec.data_model.model_json_schema(),
        })  # fmt: skip
    scope_rows = [
        {"key": s.key, "label": s.label, "description": s.description, "purpose": s.purpose_text, "sensitive": s.sensitive,
         "resources": sorted(s.resources), "api_key_allowed": s.api_key_allowed}
        for s in sorted(scopes().values(), key=lambda s: s.key)
    ]  # fmt: skip
    mcp = []
    registry = tools.tools()
    for key, exp in sorted(exposure.exposures("mcp").items()):
        spec = registry.get(key)
        if spec is None:
            continue
        mcp.append({
            "name": key, "title": spec.title, "description": spec.description, "group": exp.group,
            "execution_class": _execution_class(spec, exp), "scopes": [s for s in [exp.mcp_scope, *exp.resource_scopes] if s],
            "max_results": exp.max_results, "input_schema": spec.input_model.model_json_schema(),
        })  # fmt: skip
    verifiers: dict[str, str] = {}
    if VERIFIERS.exists():
        for f in sorted(VERIFIERS.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                verifiers[f.relative_to(VERIFIERS).as_posix()] = f.read_text(encoding="utf-8-sig")
    s = get_settings()
    return {
        "version": 1,
        "openapi": openapi,
        "changelog": changelog,
        "deprecations": [getattr(d, "__dict__", d) for d in getattr(public_api_deprecations, "DEPRECATIONS", [])],
        "events": events,
        "scopes": scope_rows,
        "mcp": {"protocol_versions": list(s.mcp_supported_protocol_versions), "tools": mcp},
        "manifest_schema": sdk.manifest_schema(),
        "ports": sdk.ports(),
        "rate_tiers": [dict(t.__dict__) if hasattr(t, "__dict__") else {f: getattr(t, f) for f in t.__slots__} for t in rate_tiers.TIERS.values()],
        "signing": {"vectors": json.loads((BACKEND / "tests" / "core" / "signing_vectors.json").read_text(encoding="utf-8")), "verifiers": verifiers},
    }


def coverage(bundle: dict[str, Any]) -> list[str]:
    """Every undocumented item (operation summary/description, parameter description, event description/example, MCP
    tool description/execution class, scope label/description/purpose)."""
    problems: list[str] = []
    for path, ops in (bundle["openapi"].get("paths") or {}).items():
        for method, op in ops.items():
            if not isinstance(op, dict):
                continue
            oid = op.get("operationId") or f"{method} {path}"
            if not op.get("summary"):
                problems.append(f"operation {oid}: summary")
            if not op.get("description"):
                problems.append(f"operation {oid}: description")
            for p in op.get("parameters") or []:
                if not p.get("description"):
                    problems.append(f"operation {oid}: parameter {p.get('name')}")
    for e in bundle["events"]:
        if not e["description"]:
            problems.append(f"event {e['type']}: description")
        if not e["example"]:
            problems.append(f"event {e['type']}: example")
    for t in bundle["mcp"]["tools"]:
        if not t["description"] or not t["execution_class"]:
            problems.append(f"mcp tool {t['name']}: description")
    for sc in bundle["scopes"]:
        for f in ("label", "description", "purpose"):
            if not sc[f]:
                problems.append(f"scope {sc['key']}: {f}")
    return problems


def baseline() -> set[str]:
    return set(json.loads(BASELINE.read_text(encoding="utf-8"))) if BASELINE.exists() else set()


def render(bundle: dict[str, Any]) -> str:
    return json.dumps(bundle, indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n"


def main() -> int:
    bundle = build()
    text = render(bundle)
    new = sorted(set(coverage(bundle)) - baseline())
    if "--check" in sys.argv:
        stale = not BUNDLE.exists() or BUNDLE.read_text(encoding="utf-8") != text
        if stale:
            print("docs-bundle.json is stale: run python scripts/build_dev_docs.py")
        for p in new:
            print(f"undocumented: {p}")
        return 1 if stale or new else 0
    if "--write-baseline" in sys.argv:
        BASELINE.write_text(json.dumps(sorted(coverage(bundle)), indent=1) + "\n", encoding="utf-8")
    BUNDLE.parent.mkdir(parents=True, exist_ok=True)
    BUNDLE.write_text(text, encoding="utf-8")
    print(f"wrote {BUNDLE} ({len(text) // 1024} KB); undocumented beyond baseline: {len(new)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
