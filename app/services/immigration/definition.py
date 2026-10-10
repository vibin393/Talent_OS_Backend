# ruff: noqa: E501
"""Visa Type definition rules (Epic 46 A1, AD-52). Shared by the platform templates and tenant versions.

Pure functions: no I/O. Error codes: ``visa_definition_invalid`` (shape), ``visa_stage_category_invalid``,
``visa_stage_duplicate``, ``visa_expiry_primary_invalid``, ``visa_alert_offsets_invalid``.
"""

from typing import Any

from pydantic import ValidationError

from app.core.errors import ValidationFailed
from app.schemas.immigration import StageDef, VisaTypeDefinition

STAGE_CATEGORIES = ("preparing", "filed", "pending_decision")
CATEGORY_LABELS = {"preparing": "Preparing", "filed": "Filed", "pending_decision": "Pending decision"}
DEFAULT_OFFSETS = (120, 90, 60, 30)


def _fail(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details or None)


def _unique(keys: list[str], what: str) -> None:
    seen: set[str] = set()
    for k in keys:
        if k in seen:
            raise _fail(
                "visa_stage_duplicate" if what == "stage" else "visa_definition_invalid",
                f"Duplicate {what} key: {k}",
                key=k,
            )
        seen.add(k)


def validate(raw: dict[str, Any] | VisaTypeDefinition) -> VisaTypeDefinition:
    """Parse and check a definition; returns the normalised model (offsets sorted descending)."""
    if isinstance(raw, VisaTypeDefinition):
        d = raw
    else:
        try:
            d = VisaTypeDefinition.model_validate(raw)
        except ValidationError as exc:
            first = exc.errors()[0] if exc.errors() else {}
            loc = ".".join(str(p) for p in first.get("loc", ()))
            raise _fail(
                "visa_definition_invalid",
                f"The visa type definition is not valid ({loc}: {first.get('msg', 'invalid')})",
                field=loc,
            ) from None
    for s in d.stages:
        if s.category not in STAGE_CATEGORIES:
            raise _fail(
                "visa_stage_category_invalid",
                f"Stage {s.key} has an unknown category {s.category!r} (use preparing, filed or pending_decision)",
                stage=s.key,
            )
    _unique([s.key for s in d.stages], "stage")
    _unique([c.key for c in d.checklist], "checklist item")
    _unique([f.key for f in d.expiry_fields], "expiry field")
    _unique([k.key for k in d.key_dates], "key date")
    if sum(1 for f in d.expiry_fields if f.primary) != 1:
        raise _fail("visa_expiry_primary_invalid", "Mark exactly one expiry field as primary")
    offsets = d.alert_offsets or list(DEFAULT_OFFSETS)
    if any(o < 1 or o > 365 for o in offsets) or len(set(offsets)) != len(offsets):
        raise _fail("visa_alert_offsets_invalid", "Alert offsets must be unique and between 1 and 365 days")
    return d.model_copy(update={"alert_offsets": sorted(offsets, reverse=True)})


def stage(d: VisaTypeDefinition, key: str | None) -> StageDef | None:
    if key is None:
        return None
    return next((s for s in d.stages if s.key == key), None)


def stage_index(d: VisaTypeDefinition, key: str | None) -> int:
    return next((i for i, s in enumerate(d.stages) if s.key == key), -1)


def primary_expiry(d: VisaTypeDefinition) -> Any:
    return next(f for f in d.expiry_fields if f.primary)


def dump(d: VisaTypeDefinition) -> dict[str, Any]:
    return d.model_dump(mode="json", exclude_none=True)
