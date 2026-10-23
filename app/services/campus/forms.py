# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Drive registration form schema and answer validation (Epic 43 story 43.2, FR-218).

``{fields: [{key, label, type, required, options?, min?, max?, help?}]}``. The standard keys are always present and
typed -- ``program`` (select from the Institution's programs), ``branch``, ``cgpa`` (decimal 0-10, 2 dp),
``percentage`` (decimal 0-100), ``backlogs`` (int >= 0), ``graduation_year`` (int) -- and are denormalised into typed
registration columns for the ``campus.registration`` semantic source. Up to 20 custom fields; ``file`` fields are
stored through P1 files (scanned, <= 5 MB). Keys or labels naming protected characteristics are refused (422
``protected_field``); identity fields (name, email, phone) are collected separately.
"""

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from app.core.errors import ValidationFailed
from app.schemas.campus import FormField, FormSchema

MAX_CUSTOM = 20
MAX_TEXT = 1000
PROTECTED = frozenset(
    {"caste", "religion", "race", "ethnicity", "gender", "age", "dob", "disability", "marital_status"}
)
_PROTECTED_WORDS = re.compile(
    r"\b(caste|religion|race|ethnicity|gender|age|dob|date of birth|birth ?date|disability|marital|sub-?caste)\b",
    re.IGNORECASE,
)
RESERVED = frozenset({"full_name", "name", "email", "phone"})
STANDARD: tuple[FormField, ...] = (
    FormField(key="program", label="Program", type="select", required=True, standard=True),
    FormField(key="branch", label="Branch / specialisation", type="select", required=False, standard=True),
    FormField(key="cgpa", label="CGPA (out of 10)", type="decimal", required=False, min=Decimal(0), max=Decimal(10), standard=True),
    FormField(key="percentage", label="Aggregate percentage", type="decimal", required=False, min=Decimal(0), max=Decimal(100), standard=True),
    FormField(key="backlogs", label="Active backlogs", type="number", required=True, min=Decimal(0), max=Decimal(99), standard=True),
    FormField(key="graduation_year", label="Graduation year", type="number", required=True, min=Decimal(2000), max=Decimal(2100), standard=True),
)
STANDARD_KEYS = tuple(f.key for f in STANDARD)


def _protected(f: FormField) -> bool:
    if f.key in PROTECTED or any(part in PROTECTED for part in f.key.split("_")):
        return True
    return bool(_PROTECTED_WORDS.search(f.label))


def normalize(raw: FormSchema | dict[str, Any] | None) -> dict[str, Any]:
    """The stored form: the six standard fields first (label / required / help editable, type fixed), then up to
    20 custom fields. 422 on protected, reserved, duplicate or malformed fields."""
    try:
        schema = raw if isinstance(raw, FormSchema) else FormSchema.model_validate(raw or {"fields": []})
    except ValidationError as exc:
        raise ValidationFailed("The registration form is invalid", code="invalid_form", details={"errors": exc.errors(include_url=False, include_context=False)}) from exc
    given = {f.key: f for f in schema.fields}
    if len(given) != len(schema.fields):
        raise ValidationFailed("Each form field needs a unique key", code="duplicate_field")
    out: list[dict[str, Any]] = []
    for std in STANDARD:
        f = given.get(std.key)
        merged = std.model_copy(
            update={
                "label": f.label if f else std.label,
                "required": (f.required if f else std.required) or std.key == "program",
                "help": f.help if f else None,
            }
        )
        out.append(merged.model_dump(mode="json", exclude_none=True))
    custom = [f for f in schema.fields if f.key not in STANDARD_KEYS]
    if len(custom) > MAX_CUSTOM:
        raise ValidationFailed(f"At most {MAX_CUSTOM} custom fields", code="too_many_fields")
    for f in custom:
        path = {f"fields.{f.key}": "Not allowed"}
        if _protected(f):
            raise ValidationFailed(
                f"The field '{f.label}' asks for a protected characteristic and cannot be collected",
                code="protected_field",
                details={"fields": path},
            )
        if f.key in RESERVED:
            raise ValidationFailed(f"'{f.key}' is collected automatically", code="reserved_field", details={"fields": path})
        if f.type in ("select", "multiselect") and not f.options:
            raise ValidationFailed(f"'{f.label}' needs options", code="options_required", details={"fields": path})
        out.append(f.model_copy(update={"standard": False}).model_dump(mode="json", exclude_none=True))
    return {"fields": out}


def fields_of(form: dict[str, Any]) -> list[FormField]:
    return [FormField.model_validate(f) for f in normalize(form)["fields"]]


def branches_by_program(programs: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {str(p.get("name")): list(p.get("branches") or []) for p in programs or [] if p.get("name")}


def _decimal(v: Any) -> Decimal | None:
    if isinstance(v, bool) or v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        d = Decimal(str(v).strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def validate_answers(
    form: dict[str, Any],
    answers: dict[str, Any],
    programs: list[dict[str, Any]],
    file_keys: set[str] | None = None,
    *,
    partial: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate ``answers`` against the drive form. Returns ``(clean answers, typed standard columns)``; raises 422
    ``invalid_answers`` with ``details.fields = {key: message}`` listing every bad field."""
    fields = fields_of(form)
    by_key = {f.key: f for f in fields}
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}
    files = file_keys or set()
    for key in answers:
        if key not in by_key:
            errors[key] = "Unknown field"
    branch_map = branches_by_program(programs)
    for f in fields:
        raw = answers.get(f.key)
        empty = raw is None or (isinstance(raw, str) and not raw.strip()) or raw == []
        if f.type == "file":
            if f.key in files:
                clean[f.key] = "__file__"
            elif f.required and not partial:
                errors[f.key] = "Attach a file"
            continue
        if empty:
            if f.required and not partial:
                errors[f.key] = "Required"
            continue
        value: Any = raw
        if f.type in ("text",):
            value = str(raw).strip()
            if len(value) > MAX_TEXT:
                errors[f.key] = f"At most {MAX_TEXT} characters"
                continue
        elif f.type == "decimal":
            d = _decimal(raw)
            if d is None:
                errors[f.key] = "Enter a number"
                continue
            value = d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        elif f.type == "number":
            d = _decimal(raw)
            if d is None or d != d.to_integral_value():
                errors[f.key] = "Enter a whole number"
                continue
            value = int(d)
        elif f.type == "bool":
            if not isinstance(raw, bool):
                errors[f.key] = "Choose yes or no"
                continue
        elif f.type == "date":
            try:
                value = date.fromisoformat(str(raw)).isoformat()
            except ValueError:
                errors[f.key] = "Enter a date (YYYY-MM-DD)"
                continue
        elif f.type == "select":
            value = str(raw).strip()
            options = list(branch_map) if f.key == "program" and branch_map else (
                branch_map.get(str(answers.get("program") or "").strip(), []) if f.key == "branch" else (f.options or [])
            )
            if options and value not in options:
                errors[f.key] = "Choose one of the listed options"
                continue
            if len(value) > 200:
                errors[f.key] = "Too long"
                continue
        elif f.type == "multiselect":
            if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
                errors[f.key] = "Choose from the list"
                continue
            if any(x not in (f.options or []) for x in raw):
                errors[f.key] = "Choose from the listed options"
                continue
            value = list(dict.fromkeys(raw))
        if f.type in ("decimal", "number") and isinstance(value, (int, Decimal)):
            if f.min is not None and value < f.min:
                errors[f.key] = f"At least {f.min}"
                continue
            if f.max is not None and value > f.max:
                errors[f.key] = f"At most {f.max}"
                continue
        clean[f.key] = value
    if errors:
        raise ValidationFailed("Check the highlighted answers", code="invalid_answers", details={"fields": errors})
    typed = {
        "program": clean.get("program"),
        "branch": clean.get("branch"),
        "cgpa": clean.get("cgpa"),
        "percentage": clean.get("percentage"),
        "backlogs": clean.get("backlogs"),
        "graduation_year": clean.get("graduation_year"),
    }
    stored = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in clean.items()}
    return stored, typed
