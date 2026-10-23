"""FR-80 field visibility framework (Epic 14, AD-26): what external principals must never receive.

* ``FORBIDDEN_FIELDS`` lists, per portal principal kind, the forbidden field-name stems. A stem ending in
``*``
  matches every field starting with it (``margin*`` -> ``margin_pct``); others match exactly.
* Every response model of a portal (``app/schemas/*_portal.py``) is registered with ``register_portal_schema``
  (decorator). ``tests/portal/test_field_visibility.py`` walks every registered model recursively (nested
  models, lists, unions, dicts) and fails on a forbidden field; a second test asserts every model defined in a
  ``*_portal.py`` module is registered.
* ``ClientNameRevealed`` is the only way a vendor schema may carry ``client_name`` (FR-80: shown only when the
  tenant chose to reveal the client for that demand).
* ``scan_payload`` is the dev/test response guard used by portal routers: it scans JSON keys of an outgoing
  payload and returns the offending paths.
"""

import json
import types
import typing
from collections.abc import Callable, Coroutine, Iterable, Mapping
from typing import Annotated, Any, Literal, NewType, get_args, get_origin

from fastapi import Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app.core.settings import get_settings

PrincipalKind = Literal["client_user", "vendor_user", "worker"]

FORBIDDEN_FIELDS: Mapping[PrincipalKind, frozenset[str]] = {
    "client_user": frozenset(
        {
            "pay_rate*",
            "vendor_rate*",
            "burden_pct",
            "vendor_burden_pct",
            "margin*",
            "bank_*",
            "pan*",
            "aadhaar*",
            "passport*",
            "notes",
            "note",
            "match_score*",
            "vendor_id",
            "vendor_name",
            "vendor*",
        }
    ),
    "vendor_user": frozenset(
        {"bill_rate*", "margin*", "client_feedback*", "feedback_comment", "client_name", "other_vendor*"}
    ),
    "worker": frozenset({"bill_rate*", "vendor_rate*", "margin*"}),
}

# Epic 48 (FR-243, A5): predictions never reach a portal principal (Client, Vendor, Worker): no
# prediction, attrition, drop-risk or time-to-fill field in any portal schema.
PREDICTION_FIELD_STEMS: frozenset[str] = frozenset(
    {"prediction*", "attrition*", "drop_risk*", "time_to_fill*"}
)
FORBIDDEN_FIELDS = {kind: stems | PREDICTION_FIELD_STEMS for kind, stems in FORBIDDEN_FIELDS.items()}

# Marker type: a vendor schema may expose ``client_name`` only through this type (FR-80 reveal).
ClientNameRevealed = NewType("ClientNameRevealed", str)
# (principal kind, field name) pairs allowed when the field's annotation is a marker type.
_MARKER_EXEMPTIONS: Mapping[tuple[str, str], object] = {("vendor_user", "client_name"): ClientNameRevealed}

_REGISTRY: dict[type[BaseModel], set[str]] = {}


def register_portal_schema[M: type[BaseModel]](
    principal_kind: PrincipalKind | Iterable[PrincipalKind],
) -> Callable[[M], M]:
    """Register a portal response model: ``@register_portal_schema("client_user")`` on every ``*_portal.py``
    response model."""
    kinds = {principal_kind} if isinstance(principal_kind, str) else set(principal_kind)
    unknown = kinds - set(FORBIDDEN_FIELDS)
    if unknown:
        msg = f"unknown portal principal kind(s): {sorted(unknown)}"
        raise ValueError(msg)

    def decorator(model: M) -> M:
        _REGISTRY.setdefault(model, set()).update(kinds)
        return model

    return decorator


def registered_schemas() -> dict[type[BaseModel], frozenset[str]]:
    return {model: frozenset(kinds) for model, kinds in _REGISTRY.items()}


def is_forbidden(principal_kind: str, field: str) -> bool:
    name = field.lower()
    for stem in FORBIDDEN_FIELDS.get(principal_kind, frozenset()):  # type: ignore[call-overload]
        if stem.endswith("*"):
            if name.startswith(stem[:-1]):
                return True
        elif name == stem:
            return True
    return False


def _exempt(principal_kind: str, field: str, annotation: Any) -> bool:
    marker = _MARKER_EXEMPTIONS.get((principal_kind, field))
    return marker is not None and _contains_type(annotation, marker)


def _contains_type(annotation: Any, target: object) -> bool:
    if annotation is target:
        return True
    return any(_contains_type(arg, target) for arg in get_args(annotation))


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    """Pydantic models reachable from an annotation (lists, dicts, unions, Optional, Annotated)."""
    found: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    origin = get_origin(annotation)
    if origin is Annotated:
        return _nested_models(get_args(annotation)[0])
    if origin in (typing.Union, types.UnionType) or origin is not None:
        for arg in get_args(annotation):
            found.extend(_nested_models(arg))
    return found


def violations(model: type[BaseModel], principal_kind: str) -> list[str]:
    """``Model.field`` (dotted through nested models) entries that a ``principal_kind`` must never receive."""
    out: list[str] = []
    seen: set[type[BaseModel]] = set()

    def walk(current: type[BaseModel], prefix: str) -> None:
        if current in seen:
            return
        seen.add(current)
        for name, info in current.model_fields.items():
            exposed = info.serialization_alias or info.alias or name
            for candidate in {name, exposed}:
                if is_forbidden(principal_kind, candidate) and not _exempt(
                    principal_kind, candidate, info.annotation
                ):
                    out.append(f"{prefix}{current.__name__}.{candidate}")
            for nested in _nested_models(info.annotation):
                walk(nested, f"{prefix}{current.__name__}.{name}>")

    walk(model, "")
    return sorted(set(out))


def scan_payload(
    payload: Any, principal_kind: str, path: str = "$", *, allow: frozenset[str] = frozenset()
) -> list[str]:
    """JSON paths of forbidden keys in an outgoing payload (dev/test response guard). ``allow`` lists keys a
    route may legitimately send (e.g. a revealed ``client_name``)."""
    hits: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            here = f"{path}.{key}"
            if isinstance(key, str) and key not in allow and is_forbidden(principal_kind, key):
                hits.append(here)
            hits.extend(scan_payload(value, principal_kind, here, allow=allow))
    elif isinstance(payload, list | tuple):
        for i, value in enumerate(payload):
            hits.extend(scan_payload(value, principal_kind, f"{path}[{i}]", allow=allow))
    return hits


class ForbiddenFieldLeak(RuntimeError):  # ruff: ignore[error-suffix-on-exception-name] - a programming error, not a domain error
    """Raised by the dev/test response guard when a portal response carries a forbidden field."""


def portal_guard_route(principal_kind: PrincipalKind, *, allow: Iterable[str] = ()) -> type[APIRoute]:
    """``APIRouter(route_class=portal_guard_route("client_user"))``: outside production / staging every JSON
    response of the router is scanned and a forbidden key raises ``ForbiddenFieldLeak`` (FR-80)."""
    allowed = frozenset(allow)

    class PortalGuardRoute(APIRoute):
        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            original = super().get_route_handler()

            async def handler(request: Request) -> Response:
                response = await original(request)
                if (
                    get_settings().app_env in {"development", "test"}
                    and response.media_type == "application/json"
                ):
                    body = getattr(response, "body", b"")
                    if body:
                        hits = scan_payload(json.loads(body), principal_kind, allow=allowed)
                        if hits:
                            msg = f"{principal_kind} response of {request.url.path} exposes {hits}"
                            raise ForbiddenFieldLeak(msg)
                return response

            return handler

    return PortalGuardRoute
