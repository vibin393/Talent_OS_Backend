# ruff: file-ignore[non-imperative-mood, line-too-long]
"""Public API v1 contract pin (Epic 67, FR-347, spec A5). Pure: no ``app.services`` imports.

* ``contract_document(app)`` = the public OpenAPI document plus ``x-required-scopes`` on every operation (from the
  ``require_scope`` dependencies).
* ``breaking_changes(pinned, current)``: removed paths / operations, removed or retyped response fields, new required
  request fields or parameters, narrowed enums and added required scopes -- each fails CI.
* ``additions(pinned, current)``: new paths and new schema fields, which ``public_api_changelog.yaml`` must cover.
"""

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.core.public_api import is_public_route, public_openapi

PIN = Path(__file__).resolve().parents[2] / "openapi-public-v1.json"
CHANGELOG = Path(__file__).resolve().parent / "public_api_changelog.yaml"
METHODS = ("get", "post", "put", "patch", "delete")


def _route_scopes(route: APIRoute) -> list[str]:
    found: list[str] = []
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        found.extend(getattr(dep.call, "required_scopes", ()) or ())
        stack.extend(dep.dependencies)
    return sorted(set(found))


def contract_document(app: FastAPI) -> dict[str, Any]:
    doc = public_openapi(app)
    for route in app.routes:
        if not is_public_route(route):
            continue
        assert isinstance(route, APIRoute)
        item = doc.get("paths", {}).get(route.path_format)
        if item is None:
            continue
        for method in route.methods or ():
            op = item.get(method.lower())
            if op is not None:
                op["x-required-scopes"] = _route_scopes(route)
    doc.pop("servers", None)
    return doc


def load_pin() -> dict[str, Any]:
    return json.loads(PIN.read_text(encoding="utf-8")) if PIN.exists() else {"paths": {}, "components": {"schemas": {}}}


def write_pin(doc: dict[str, Any]) -> None:
    PIN.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _schemas(doc: dict[str, Any]) -> dict[str, Any]:
    return dict((doc.get("components") or {}).get("schemas") or {})


def _type_of(prop: dict[str, Any]) -> Any:
    if "$ref" in prop:
        return prop["$ref"]
    if "anyOf" in prop:
        return sorted(json.dumps(_type_of(p), sort_keys=True) for p in prop["anyOf"])
    if prop.get("type") == "array":
        return ["array", _type_of(prop.get("items") or {})]
    return prop.get("type")


def _request_schemas(doc: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    for item in (doc.get("paths") or {}).values():
        for method in METHODS:
            op = item.get(method)
            if not op:
                continue
            for media in ((op.get("requestBody") or {}).get("content") or {}).values():
                ref = (media.get("schema") or {}).get("$ref", "")
                if ref:
                    names.add(ref.rsplit("/", 1)[-1])
    return names


def breaking_changes(pinned: dict[str, Any], current: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    old_paths, new_paths = pinned.get("paths") or {}, current.get("paths") or {}
    for path, item in old_paths.items():
        if path not in new_paths:
            problems.append(f"removed path {path}")
            continue
        for method in METHODS:
            old_op, new_op = item.get(method), new_paths[path].get(method)
            if old_op is None:
                continue
            if new_op is None:
                problems.append(f"removed operation {method.upper()} {path}")
                continue
            added_scopes = set(new_op.get("x-required-scopes") or []) - set(old_op.get("x-required-scopes") or [])
            if added_scopes:
                problems.append(f"{method.upper()} {path}: added required scope(s) {sorted(added_scopes)}")
            old_params = {(p.get("in"), p.get("name")) for p in old_op.get("parameters") or []}
            for p in new_op.get("parameters") or []:
                if p.get("required") and (p.get("in"), p.get("name")) not in old_params:
                    problems.append(f"{method.upper()} {path}: new required parameter {p.get('name')}")
    old_s, new_s = _schemas(pinned), _schemas(current)
    requests = _request_schemas(current)
    for name, old in old_s.items():
        new = new_s.get(name)
        if new is None:
            problems.append(f"removed schema {name}")
            continue
        old_props, new_props = old.get("properties") or {}, new.get("properties") or {}
        for prop, spec in old_props.items():
            if prop not in new_props:
                problems.append(f"{name}.{prop} removed")
            elif _type_of(spec) != _type_of(new_props[prop]):
                problems.append(f"{name}.{prop} retyped")
        if name in requests:
            newly_required = set(new.get("required") or []) - set(old.get("required") or [])
            for prop in sorted(newly_required):
                problems.append(f"{name}.{prop} became a required request field")
        if "enum" in old and not set(old["enum"]) <= set(new.get("enum") or []):
            problems.append(f"{name}: enum narrowed")
        for prop, spec in old_props.items():
            if "enum" in spec and prop in new_props and not set(spec["enum"]) <= set(new_props[prop].get("enum") or []):
                problems.append(f"{name}.{prop}: enum narrowed")
    return problems


def additions(pinned: dict[str, Any], current: dict[str, Any]) -> tuple[list[str], list[str]]:
    old_paths, new_paths = pinned.get("paths") or {}, current.get("paths") or {}
    paths = sorted(p for p in new_paths if p not in old_paths)
    fields: list[str] = []
    old_s = _schemas(pinned)
    for name, schema in _schemas(current).items():
        old_props = (old_s.get(name) or {}).get("properties") or {}
        fields.extend(f"{name}.{p}" for p in (schema.get("properties") or {}) if p not in old_props)
    return paths, sorted(fields)


def changelog_covers(entries: list[dict[str, Any]], paths: list[str], fields: list[str]) -> list[str]:
    """The added paths / fields no changelog entry mentions (``"*"`` covers everything, for the v1 launch entry)."""
    listed_paths = {p for e in entries for p in e.get("paths") or []}
    listed_fields = {f for e in entries for f in e.get("fields") or []}
    missing = [] if "*" in listed_paths else [p for p in paths if p not in listed_paths]
    if "*" not in listed_fields:
        missing += [f for f in fields if f not in listed_fields]
    return missing


__all__ = ["CHANGELOG", "PIN", "additions", "breaking_changes", "changelog_covers", "contract_document", "load_pin", "write_pin"]
