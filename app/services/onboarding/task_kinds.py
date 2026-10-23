# ruff: file-ignore[line-too-long, unused-function-argument]
"""Onboarding task kinds (Epic 23 A5, P2-10.2, FR-122 / FR-123): one handler per kind.

Each Worker kind implements ``submit`` (validate the payload, write sensitive values through placements /
documents, keep only masked references in ``data``) and ``on_approve`` (runs in the decision transaction; a
failure rolls the decision back). Bank / PAN / identity numbers never reach onboarding tables (AD-35).
"""

import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.models.onboarding import OnboardingCase, OnboardingTask
from app.services.onboarding.common import as_uuid

PERSONAL_FIELDS = (
    "phone",
    "current_address",
    "city",
    "state",
    "date_of_birth",
    "father_or_spouse_name",
    "permanent_address",
)
DECLARATION_KEYS = ("section_80c", "section_80d", "hra_rent", "home_loan_interest", "other")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PHONE_RE = re.compile(r"^\+?[0-9 ()-]{6,20}$")


def _invalid(message: str, code: str = "invalid_payload", field: str | None = None) -> ValidationFailed:
    return ValidationFailed(message, code=code, details={"field": field} if field else None)


def _text(payload: dict[str, Any], key: str, *, required: bool = False, max_len: int = 300) -> str | None:
    raw = payload.get(key)
    value = str(raw).strip() if raw is not None else ""
    if required and not value:
        raise _invalid("This field is required", "field_required", key)
    if len(value) > max_len:
        raise _invalid("This value is too long", "invalid_payload", key)
    return value or None


# ================================================================ submit
async def submit_personal(
    ctx: RequestContext, db: AsyncSession, case: OnboardingCase, task: OnboardingTask, payload: dict[str, Any]
) -> None:
    data: dict[str, Any] = {}
    for key in PERSONAL_FIELDS:
        value = _text(payload, key, required=key in ("date_of_birth", "current_address"), max_len=500)
        if value is not None:
            data[key] = value
    if not _DATE_RE.match(data.get("date_of_birth", "")):
        raise _invalid("Enter the date of birth as YYYY-MM-DD", "invalid_date", "date_of_birth")
    if "phone" in data and not _PHONE_RE.match(data["phone"]):
        raise _invalid("Enter a valid phone number", "invalid_phone", "phone")
    task.data = data


async def submit_bank(
    ctx: RequestContext, db: AsyncSession, case: OnboardingCase, task: OnboardingTask, payload: dict[str, Any]
) -> None:
    from app.schemas.placements_api import BankAccountIn
    from app.services import placements_service

    try:
        data = BankAccountIn(**{k: payload.get(k) for k in BankAccountIn.model_fields if k in payload})
    except ValidationError as exc:
        raise _invalid("Check the bank details", "invalid_bank_details") from exc
    ident = await placements_service.worker_payment_identity(ctx, db, task.candidate_id)
    if ident.bank_active:  # a second account goes through the bank_detail_change approval (AD-24)
        row = await placements_service.request_bank_change(ctx, db, task.candidate_id, data, "onboarding")
        change = True
    else:
        row = await placements_service.record_bank_details(ctx, db, task.candidate_id, data, "onboarding")
        change = False
    task.data = {
        "bank_account_id": str(row.id),
        "last4": row.account_number_last4,
        "ifsc": row.ifsc,
        "change_requested": change,
    }


def _declarations(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _invalid("Declarations must be amounts by section", field="declarations")
    out: dict[str, str] = {}
    for key, value in raw.items():
        if key not in DECLARATION_KEYS:
            raise _invalid("Unknown declaration section", field=f"declarations.{key}")
        if value in (None, ""):
            continue
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise _invalid("Enter an amount", "invalid_amount", f"declarations.{key}") from exc
        if amount < 0 or amount > Decimal("100000000"):
            raise _invalid("Enter a valid amount", "invalid_amount", f"declarations.{key}")
        out[key] = str(amount.quantize(Decimal("0.01")))
    return out


async def submit_tax(
    ctx: RequestContext, db: AsyncSession, case: OnboardingCase, task: OnboardingTask, payload: dict[str, Any]
) -> None:
    from app.services import placements_service

    pan = _text(payload, "pan", required=True, max_len=10) or ""
    profile = await placements_service.record_pan(
        ctx, db, task.candidate_id, pan, _text(payload, "pan_name", max_len=200)
    )
    mode = (task.config or {}).get("declaration_mode", "either")
    regime = payload.get("regime")
    declarations = _declarations(payload.get("declarations"))
    has_form = regime in ("old", "new")
    has_upload = bool(task.document_ids)
    if regime is not None and not has_form:
        raise _invalid("Pick the old or the new tax regime", "invalid_regime", "regime")
    if mode == "form" and not has_form:
        raise _invalid("Pick your tax regime", "field_required", "regime")
    if mode == "upload" and not has_upload:
        raise _invalid("Upload your investment declaration", "document_required", "files")
    if mode == "either" and not (has_form or has_upload):
        raise _invalid("Fill in the declaration or upload it", "field_required", "regime")
    task.data = {
        "pan_last4": profile.pan_last4,
        "regime": regime if has_form else None,
        "declarations": declarations if has_form else {},
        "declaration_uploaded": has_upload,
    }


async def submit_emergency(
    ctx: RequestContext, db: AsyncSession, case: OnboardingCase, task: OnboardingTask, payload: dict[str, Any]
) -> None:
    name = _text(payload, "name", required=True, max_len=200)
    phone = _text(payload, "phone", required=True, max_len=40) or ""
    if not _PHONE_RE.match(phone):
        raise _invalid("Enter a valid phone number", "invalid_phone", "phone")
    task.data = {"name": name, "relation": _text(payload, "relation", max_len=100), "phone": phone}


async def submit_document(
    ctx: RequestContext, db: AsyncSession, case: OnboardingCase, task: OnboardingTask, payload: dict[str, Any]
) -> None:
    if not task.document_ids:
        raise _invalid("Upload the document first", "document_required", "files")
    data: dict[str, Any] = {"document_count": len(task.document_ids)}
    if (task.config or {}).get("aadhaar"):
        last4 = str(payload.get("aadhaar_last4") or "").strip()
        if not re.fullmatch(r"\d{4}", last4):
            raise _invalid(
                "Enter the last 4 digits of the Aadhaar number", "invalid_aadhaar_last4", "aadhaar_last4"
            )
        if not payload.get("masked_image"):
            raise _invalid("Capture the masked Aadhaar image", "masked_image_required", "masked_image")
        data.update({"last4": last4, "redaction_status": "pending_review"})
    task.data = data


async def submit_client_form(
    ctx: RequestContext, db: AsyncSession, case: OnboardingCase, task: OnboardingTask, payload: dict[str, Any]
) -> None:
    fields = (task.config or {}).get("fields") or []
    values: dict[str, Any] = {}
    for f in fields:
        name, kind = f["name"], f.get("type")
        raw = payload.get(name)
        missing = raw is None or raw == "" or raw == []
        if missing:
            if f.get("required"):
                raise _invalid("This field is required", "field_required", name)
            continue
        if kind == "number":
            try:
                values[name] = str(Decimal(str(raw)))
            except InvalidOperation as exc:
                raise _invalid("Enter a number", "invalid_number", name) from exc
        elif kind == "date":
            if not _DATE_RE.match(str(raw)):
                raise _invalid("Enter a date as YYYY-MM-DD", "invalid_date", name)
            values[name] = str(raw)
        elif kind == "choice":
            options = [str(o) for o in f.get("options") or []]
            if str(raw) not in options:
                raise _invalid("Pick one of the options", "invalid_choice", name)
            values[name] = str(raw)
        elif kind == "boolean":
            if not isinstance(raw, bool):
                raise _invalid("Answer yes or no", "invalid_boolean", name)
            values[name] = raw
        elif kind == "file":
            values[name] = [str(x) for x in (raw if isinstance(raw, list) else [raw]) if as_uuid(x)]
        else:
            values[name] = str(raw)[:2000]
    task.data = {"values": values}


async def submit_not_by_form(
    ctx: RequestContext, db: AsyncSession, case: OnboardingCase, task: OnboardingTask, payload: dict[str, Any]
) -> None:
    raise ConflictError(
        {
            "esign": "Sign the document in the signing ceremony",
            "policy_ack": "Read the policy and acknowledge it",
            "credential": "Provide the credential in My Work > Credentials",
        }.get(task.kind, "This task is not completed by a form"),
        code="not_submittable",
    )


SUBMIT = {
    "personal_details": submit_personal,
    "bank_details": submit_bank,
    "tax_details": submit_tax,
    "emergency_contact": submit_emergency,
    "document_upload": submit_document,
    "client_form": submit_client_form,
    "esign": submit_not_by_form,
    "policy_ack": submit_not_by_form,
    "credential": submit_not_by_form,
}


# ================================================================ on approve
async def approve_personal(
    ctx: RequestContext, db: AsyncSession, task: OnboardingTask, extra: dict[str, Any]
) -> None:
    """Candidate fields (current location) are proposed and applied when the reviewer may edit the candidate."""
    from loguru import logger

    from app.services.candidates.pipeline_port import update_candidate_fields

    city = (task.data or {}).get("city")
    if not city:
        return
    try:
        async with db.begin_nested():
            await update_candidate_fields(ctx, db, task.candidate_id, {"current_location": city})
    except Exception:
        logger.info("candidate location not applied from onboarding task {}", task.id)


async def approve_bank(
    ctx: RequestContext, db: AsyncSession, task: OnboardingTask, extra: dict[str, Any]
) -> None:
    from app.services import placements_service

    data = task.data or {}
    if data.get("change_requested"):
        raise ConflictError("This bank change is decided in the approval inbox", code="bank_change_pending")
    bank_id = as_uuid(data.get("bank_account_id"))
    if bank_id is None:
        raise ConflictError("No bank details were submitted", code="bank_details_missing")
    await placements_service.activate_bank_details(ctx, db, bank_id)


async def approve_tax(
    ctx: RequestContext, db: AsyncSession, task: OnboardingTask, extra: dict[str, Any]
) -> None:
    from app.services import placements_service

    if extra.get("pan_name_checked") is not True:
        raise ValidationFailed(
            "Confirm that the PAN name matches the Worker's name", code="pan_name_check_required"
        )
    await placements_service.confirm_pan(ctx, db, task.candidate_id)


async def approve_emergency(
    ctx: RequestContext, db: AsyncSession, task: OnboardingTask, extra: dict[str, Any]
) -> None:
    from app.services import placements_service

    data = task.data or {}
    await placements_service.update_profile(
        ctx,
        db,
        task.candidate_id,
        {
            "emergency_name": data.get("name"),
            "emergency_relation": data.get("relation"),
            "emergency_phone": data.get("phone"),
        },
    )


async def approve_document(
    ctx: RequestContext, db: AsyncSession, task: OnboardingTask, extra: dict[str, Any]
) -> None:
    if (task.config or {}).get("aadhaar") and (task.data or {}).get("redaction_status") != "confirmed":
        raise ConflictError(
            "A reviewer must confirm the Aadhaar image is masked first", code="redaction_review_required"
        )


async def approve_noop(
    ctx: RequestContext, db: AsyncSession, task: OnboardingTask, extra: dict[str, Any]
) -> None:
    return


ON_APPROVE = {
    "personal_details": approve_personal,
    "bank_details": approve_bank,
    "tax_details": approve_tax,
    "emergency_contact": approve_emergency,
    "document_upload": approve_document,
}


def on_approve(kind: str) -> Any:
    return ON_APPROVE.get(kind, approve_noop)


def uploadable(task: OnboardingTask) -> bool:
    return task.kind in ("document_upload", "tax_details") or (
        task.kind == "client_form"
        and any(f.get("type") == "file" for f in (task.config or {}).get("fields") or [])
    )


def upload_category(task: OnboardingTask) -> str:
    if task.kind == "tax_details":
        return "tax_declaration"
    if task.kind == "document_upload":
        return str((task.config or {}).get("document_category") or "other")[:40]
    return "client_form"


def new_id() -> uuid.UUID:
    return uuid.uuid4()
