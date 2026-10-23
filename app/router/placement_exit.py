# ruff: file-ignore[line-too-long]
"""Extension, conversion & offboarding (Epic 31, FR-162..FR-166).

Permissions: ``placement:read`` (reads, ending-soon, previews), ``extension:write`` (propose, recheck, exception,
links, recorded client response, withdraw), ``extension:confirm`` (★ confirm), ``conversion:write``,
``offboarding:write`` (start, early termination, items, feedback, links, cancel), ``offboarding:close`` (★ close),
``offboarding:configure`` (templates, exit settings). Rates, margin and offered compensation are masked without
``compensation:view``. Mounted **before** the Epic 19 placements router so ``/placements/ending-soon`` wins over
``/placements/{id}``. ``public_router`` serves the two Secure Link purposes (GET never mutates).
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, status

from app.core.context import get_context
from app.core.idempotency import IdempotencyGuard, idempotency
from app.core.permissions import COMPENSATION_FIELDS, Principal, mask_compensation, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.placements_exit import (
    CancelIn,
    ConfirmIn,
    ConversionIn,
    ConversionOut,
    EarlyTerminationIn,
    EndingSoonRow,
    ExceptionRequestIn,
    ExitSettingsOut,
    ExitSettingsUpdate,
    ExtensionIn,
    ExtensionOut,
    ExtensionPreviewOut,
    FeedbackIn,
    FeePreviewIn,
    FeePreviewOut,
    ItemCompleteIn,
    ItemWaiveIn,
    LinkOut,
    OffboardingCaseDetail,
    OffboardingCaseOut,
    OffboardingStartIn,
    RecordedResponseIn,
    TemplateIn,
    TemplateOut,
    TemplateUpdate,
    WithdrawIn,
)
from app.schemas.placements_exit_portal import (
    ExtensionRespondIn,
    OffboardingConfirmIn,
    PublicActionResult,
    PublicExtensionView,
    PublicOffboardingView,
)
from app.services.placements import (
    conversion,
    early_termination,
    end_alerts,
    exit_portal,
    exit_settings,
    extensions,
    offboarding,
)
from app.services.placements.common import COMPENSATION_EXTRA

router = APIRouter(tags=["placement-exit"])
public_router = APIRouter(prefix="/public/links", tags=["public"])
DB = TenantDBSessionDep

Read = Annotated[Principal, Depends(require("placement:read"))]
ExtWrite = Annotated[Principal, Depends(require("extension:write"))]
ExtConfirm = Annotated[Principal, Depends(require("extension:confirm"))]
ConvWrite = Annotated[Principal, Depends(require("conversion:write"))]
OffWrite = Annotated[Principal, Depends(require("offboarding:write"))]
OffClose = Annotated[Principal, Depends(require("offboarding:close"))]
OffConfigure = Annotated[Principal, Depends(require("offboarding:configure"))]
_FIELDS = COMPENSATION_FIELDS | COMPENSATION_EXTRA | frozenset({"offered_compensation", "est_amount"})


def _m(data: Any, principal: Principal) -> Any:
    return mask_compensation(data, principal, _FIELDS)


async def _detail(db: Any, case: Any, principal: Principal) -> Any:
    return _m(await offboarding.detail_out(db, case), principal)


# ---------------------------------------------------------------- ending soon
@router.get("/placements/ending-soon", response_model=list[EndingSoonRow])
async def ending_soon(_: Read, db: DB, within_days: Annotated[int | None, Query(ge=1, le=365)] = None) -> Any:
    return await end_alerts.ending_soon(get_context(), db, within_days)


# ---------------------------------------------------------------- extensions
@router.get("/placements/{placement_id}/extensions", response_model=list[ExtensionOut])
async def list_extensions(placement_id: uuid.UUID, p: Read, db: DB) -> Any:
    return _m(await extensions.list_for_placement(get_context(), db, placement_id), p)


@router.post("/placements/{placement_id}/extensions/preview", response_model=ExtensionPreviewOut)
async def preview_extension(placement_id: uuid.UUID, data: ExtensionIn, p: ExtWrite, db: DB) -> Any:
    return _m(await extensions.preview(get_context(), db, placement_id, data), p)


@router.post(
    "/placements/{placement_id}/extensions", response_model=ExtensionOut, status_code=status.HTTP_201_CREATED
)
async def propose_extension(placement_id: uuid.UUID, data: ExtensionIn, p: ExtWrite, db: DB) -> Any:
    return _m(await extensions.propose(get_context(), db, placement_id, data), p)


@router.get("/extensions/{extension_id}", response_model=ExtensionOut)
async def get_extension(extension_id: uuid.UUID, p: Read, db: DB) -> Any:
    return _m(await extensions.get(get_context(), db, extension_id), p)


@router.post("/extensions/{extension_id}/recheck", response_model=ExtensionOut)
async def recheck_extension(extension_id: uuid.UUID, p: ExtWrite, db: DB) -> Any:
    return _m(await extensions.recheck(get_context(), db, extension_id), p)


@router.post("/extensions/{extension_id}/request-exception", response_model=ExtensionOut)
async def request_exception(extension_id: uuid.UUID, data: ExceptionRequestIn, p: ExtWrite, db: DB) -> Any:
    return _m(await extensions.request_exception(get_context(), db, extension_id, data.reason), p)


@router.post("/extensions/{extension_id}/client-link", response_model=LinkOut)
async def extension_link(extension_id: uuid.UUID, _: ExtWrite, db: DB) -> Any:
    return await extensions.client_link(get_context(), db, extension_id)


@router.post("/extensions/{extension_id}/client-response", response_model=ExtensionOut)
async def record_client_response(
    extension_id: uuid.UUID, data: RecordedResponseIn, p: ExtWrite, db: DB
) -> Any:
    return _m(await extensions.record_response(get_context(), db, extension_id, data), p)


@router.post("/extensions/{extension_id}/confirm", response_model=ExtensionOut)
async def confirm_extension(extension_id: uuid.UUID, data: ConfirmIn, p: ExtConfirm, db: DB) -> Any:
    return _m(
        await extensions.confirm_internal(get_context(), db, extension_id, data.send_extension_letter), p
    )


@router.post("/extensions/{extension_id}/withdraw", response_model=ExtensionOut)
async def withdraw_extension(extension_id: uuid.UUID, data: WithdrawIn, p: ExtWrite, db: DB) -> Any:
    return _m(await extensions.withdraw_out(get_context(), db, extension_id, data.reason), p)


# ---------------------------------------------------------------- conversion
@router.get("/placements/{placement_id}/conversion", response_model=ConversionOut | None)
async def get_conversion(placement_id: uuid.UUID, p: Read, db: DB) -> Any:
    row = await conversion.get_for_placement(get_context(), db, placement_id)
    return _m(row, p) if row is not None else None


@router.post("/placements/{placement_id}/conversion/fee-preview", response_model=FeePreviewOut)
async def fee_preview(placement_id: uuid.UUID, data: FeePreviewIn, _: ConvWrite, db: DB) -> Any:
    return await conversion.preview(get_context(), db, placement_id, data)


@router.post(
    "/placements/{placement_id}/conversion", response_model=ConversionOut, status_code=status.HTTP_201_CREATED
)
async def record_conversion(
    placement_id: uuid.UUID,
    data: ConversionIn,
    p: ConvWrite,
    db: DB,
    idem: Annotated[IdempotencyGuard, Depends(idempotency())],
) -> Any:
    async def run() -> Any:
        row = _m(await conversion.record(get_context(), db, placement_id, data), p)
        return ConversionOut.model_validate(row).model_dump(mode="json")  # replayable JSON (Decimals as strings)

    return await idem.run(run)


# ---------------------------------------------------------------- offboarding
@router.post(
    "/placements/{placement_id}/offboarding",
    response_model=OffboardingCaseDetail,
    status_code=status.HTTP_201_CREATED,
)
async def start_offboarding(placement_id: uuid.UUID, data: OffboardingStartIn, p: OffWrite, db: DB) -> Any:
    from app.services.placements.lists import get_or_404

    ctx = get_context()
    await get_or_404(ctx, db, placement_id)
    if data.reason == "converted":
        from app.core.errors import ValidationFailed

        raise ValidationFailed("Record a conversion instead", code="use_conversion")
    case = await offboarding.initiate(
        ctx,
        db,
        placement_id,
        data.reason,
        data.last_working_day,
        notice_date=data.notice_date,
        reason_note=data.reason_note,
        template_id=data.template_id,
    )
    return await _detail(db, case, p)


@router.post(
    "/placements/{placement_id}/early-termination",
    response_model=OffboardingCaseDetail,
    status_code=status.HTTP_201_CREATED,
)
async def early_terminate(placement_id: uuid.UUID, data: EarlyTerminationIn, p: OffWrite, db: DB) -> Any:
    case = await early_termination.terminate(get_context(), db, placement_id, data)
    return await _detail(db, case, p)


@router.get("/placements/{placement_id}/offboarding", response_model=OffboardingCaseDetail | None)
async def placement_case(placement_id: uuid.UUID, p: Read, db: DB) -> Any:
    case = await offboarding.latest_for_placement(get_context(), db, placement_id)
    return await _detail(db, case, p) if case is not None else None


@router.get("/offboarding-cases", response_model=list[OffboardingCaseOut])
async def list_cases(
    _: Read,
    db: DB,
    status_: Annotated[Literal["open", "closed", "cancelled", "all"], Query(alias="status")] = "open",
) -> Any:
    return await offboarding.list_cases(get_context(), db, status_)


@router.get("/offboarding-cases/{case_id}", response_model=OffboardingCaseDetail)
async def get_case(case_id: uuid.UUID, p: Read, db: DB) -> Any:
    return await _detail(db, await offboarding.get_detail(get_context(), db, case_id), p)


@router.post("/offboarding-cases/{case_id}/refresh", response_model=OffboardingCaseDetail)
async def refresh_case(case_id: uuid.UUID, p: OffWrite, db: DB) -> Any:
    ctx = get_context()
    await offboarding.get_detail(ctx, db, case_id)
    case = await offboarding.get_case(db, case_id, lock=True)
    await offboarding.refresh_case(ctx, db, case)
    return await _detail(db, case, p)


@router.post("/offboarding-cases/{case_id}/close", response_model=OffboardingCaseDetail)
async def close_case(case_id: uuid.UUID, p: OffClose, db: DB) -> Any:
    ctx = get_context()
    await offboarding.get_detail(ctx, db, case_id)
    return await _detail(db, await offboarding.close(ctx, db, case_id), p)


@router.post("/offboarding-cases/{case_id}/cancel", response_model=OffboardingCaseDetail)
async def cancel_case(case_id: uuid.UUID, data: CancelIn, p: OffWrite, db: DB) -> Any:
    return await _detail(db, await offboarding.cancel(get_context(), db, case_id, data.reason), p)


@router.post("/offboarding-cases/{case_id}/feedback", response_model=OffboardingCaseDetail)
async def record_feedback(case_id: uuid.UUID, data: FeedbackIn, p: OffWrite, db: DB) -> Any:
    return await _detail(db, await offboarding.record_feedback(get_context(), db, case_id, data.answers), p)


@router.post("/offboarding-cases/{case_id}/client-link", response_model=LinkOut)
async def case_link(case_id: uuid.UUID, _: OffWrite, db: DB) -> Any:
    return await offboarding.client_link(get_context(), db, case_id)


@router.post("/offboarding-items/{item_id}/complete", response_model=OffboardingCaseDetail)
async def complete_item(item_id: uuid.UUID, data: ItemCompleteIn, p: OffWrite, db: DB) -> Any:
    case = await offboarding.complete_item(
        get_context(),
        db,
        item_id,
        evidence_file_id=data.evidence_file_id,
        document_id=data.document_id,
        note=data.note,
    )
    return await _detail(db, case, p)


@router.post("/offboarding-items/{item_id}/waive", response_model=OffboardingCaseDetail)
async def waive_item(item_id: uuid.UUID, data: ItemWaiveIn, p: OffWrite, db: DB) -> Any:
    return await _detail(db, await offboarding.waive_item(get_context(), db, item_id, data.reason), p)


# ---------------------------------------------------------------- settings / templates
@router.get("/offboarding/settings", response_model=ExitSettingsOut)
async def get_settings(_: OffConfigure, db: DB) -> Any:
    return await exit_settings.get_settings(get_context(), db)


@router.patch("/offboarding/settings", response_model=ExitSettingsOut)
async def update_settings(data: ExitSettingsUpdate, _: OffConfigure, db: DB) -> Any:
    return await exit_settings.update_settings(get_context(), db, data)


@router.get("/offboarding/templates", response_model=list[TemplateOut])
async def list_templates(_: Read, db: DB) -> Any:
    return await exit_settings.list_templates(get_context(), db)


@router.post("/offboarding/templates", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
async def create_template(data: TemplateIn, _: OffConfigure, db: DB) -> Any:
    return await exit_settings.create_template(get_context(), db, data)


@router.patch("/offboarding/templates/{template_id}", response_model=TemplateOut)
async def update_template(template_id: uuid.UUID, data: TemplateUpdate, _: OffConfigure, db: DB) -> Any:
    return await exit_settings.update_template(get_context(), db, template_id, data)


# ---------------------------------------------------------------- public Secure Links
@public_router.get("/{token}/extension", response_model=PublicExtensionView)
async def public_extension(token: str) -> Any:
    return await exit_portal.extension_view(token)


@public_router.post("/{token}/extension", response_model=PublicActionResult)
async def public_extension_respond(token: str, data: ExtensionRespondIn) -> Any:
    return await exit_portal.extension_respond(token, data)


@public_router.get("/{token}/offboarding", response_model=PublicOffboardingView)
async def public_offboarding(token: str) -> Any:
    return await exit_portal.offboarding_view(token)


@public_router.post("/{token}/offboarding", response_model=PublicActionResult)
async def public_offboarding_respond(token: str, data: OffboardingConfirmIn) -> Any:
    return await exit_portal.offboarding_respond(token, data)
