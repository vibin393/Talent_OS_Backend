"""FR-80 (Epic 14): portal responses never carry forbidden fields.

* Every response model registered with ``register_portal_schema`` is walked recursively (nested models, lists,
  unions, dicts) for its principal kinds' forbidden fields.
* Every response model defined in an ``app/schemas/*_portal.py`` module is registered (request models --
  ``extra="forbid"`` -- are exempt).
* The framework itself: a fixture model exposing ``margin_pct`` to a Client User fails, naming model and
field.
"""

import importlib
import inspect
import pkgutil
from typing import Union

from pydantic import BaseModel, ConfigDict

import app.schemas
from app.core import field_visibility as fv


def _portal_modules() -> list[object]:
    return [
        importlib.import_module(f"app.schemas.{m.name}")
        for m in pkgutil.iter_modules(app.schemas.__path__)
        if m.name.endswith("_portal")
    ]


def test_registered_portal_schemas_expose_no_forbidden_field() -> None:
    _portal_modules()
    registered = fv.registered_schemas()
    assert registered, "no portal schema registered"
    leaks = {
        f"{model.__module__}.{model.__name__}[{kind}]": hits
        for model, kinds in registered.items()
        for kind in kinds
        if (hits := fv.violations(model, kind))
    }
    assert leaks == {}


def test_every_portal_response_model_is_registered() -> None:
    registered = fv.registered_schemas()
    missing: list[str] = []
    for module in _portal_modules():
        for name, obj in vars(module).items():
            if not (inspect.isclass(obj) and issubclass(obj, BaseModel)) or obj.__module__ != module.__name__:  # type: ignore[attr-defined]
                continue
            if obj.model_config.get("extra") == "forbid":
                continue  # request bodies
            if obj not in registered:
                missing.append(f"{module.__name__}.{name}")  # type: ignore[attr-defined]
    assert missing == []


# ---------------------------------------------------------------- the framework itself
class _Rates(BaseModel):
    margin_pct: float


class _Leaky(BaseModel):
    name: str
    rates: list[_Rates]


class _Clean(BaseModel):
    name: str


class _Nested(BaseModel):
    child: Union[_Clean, None] = None  # noqa: UP007 - exercise typing.Union
    by_key: dict[str, list[_Rates]] = {}


def test_violations_name_model_and_field() -> None:
    hits = fv.violations(_Leaky, "client_user")
    assert hits == ["_Leaky.rates>_Rates.margin_pct"]
    assert fv.violations(_Clean, "client_user") == []
    assert fv.violations(_Nested, "client_user") == ["_Nested.by_key>_Rates.margin_pct"]
    # Vendors may see margin? No -- forbidden for every external principal.
    assert fv.violations(_Leaky, "vendor_user")
    assert fv.violations(_Leaky, "worker")


def test_stems_and_marker() -> None:
    assert fv.is_forbidden("client_user", "vendor_rate")
    assert fv.is_forbidden("client_user", "bank_account_number")
    assert fv.is_forbidden("client_user", "notes") and not fv.is_forbidden("client_user", "notes_count")
    assert not fv.is_forbidden("client_user", "bill_rate")
    assert fv.is_forbidden("vendor_user", "bill_rate_inr")
    assert not fv.is_forbidden("worker", "pay_rate")

    class _Reveal(BaseModel):
        client_name: fv.ClientNameRevealed

    class _Plain(BaseModel):
        client_name: str

    assert fv.violations(_Reveal, "vendor_user") == []
    assert fv.violations(_Plain, "vendor_user") == ["_Plain.client_name"]


def test_registration_and_scan_payload() -> None:
    @fv.register_portal_schema("client_user")
    class _Registered(BaseModel):
        model_config = ConfigDict(frozen=True)
        title: str

    assert "client_user" in fv.registered_schemas()[_Registered]
    payload = {"items": [{"title": "x", "margin_pct": 3}], "pay_rate": 1}
    assert fv.scan_payload(payload, "client_user") == ["$.items[0].margin_pct", "$.pay_rate"]
    assert fv.scan_payload({"client_name": "Acme"}, "vendor_user", allow=frozenset({"client_name"})) == []
