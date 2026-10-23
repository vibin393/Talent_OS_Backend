"""AI settings (``/api/v1/ai``): settings (``ai:admin``), feature toggles (read: any user), usage
(``ai:admin``) and the AI action log (``ai_log:read``, Epic 12)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.ai import AISettingsOut, AISettingsUpdate
from app.services import ai
from app.services.ai.admin import AIActionFilters

router = APIRouter(prefix="/ai", tags=["ai"])

AIAdmin = Annotated[Principal, Depends(require("ai:admin"))]


@router.get("/settings", response_model=AISettingsOut)
async def get_ai_settings(_: AIAdmin, db: TenantDBSessionDep) -> AISettingsOut:
    return await ai.get_settings_out(get_context(), db)


@router.put("/settings", response_model=AISettingsOut)
async def update_ai_settings(body: AISettingsUpdate, _: AIAdmin, db: TenantDBSessionDep) -> AISettingsOut:
    return await ai.update_settings(get_context(), db, body)


# ================================================================ Epic 12: toggles, usage, AI action log
AILogRead = Annotated[Principal, Depends(require("ai_log:read"))]


@router.get("/features", response_model=dict[str, bool])
async def get_ai_features(_: CurrentUserDep, db: TenantDBSessionDep) -> dict[str, bool]:
    return await ai.admin.features(get_context(), db)


@router.put("/features", response_model=dict[str, bool])
async def put_ai_features(body: dict[str, bool], _: AIAdmin, db: TenantDBSessionDep) -> dict[str, bool]:
    return await ai.admin.update_features(get_context(), db, body)


@router.get("/usage", response_model=dict[str, Any])
async def get_ai_usage(
    _: AIAdmin, db: TenantDBSessionDep, month: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None
) -> dict[str, Any]:
    return await ai.admin.usage(get_context(), db, month)


@router.get("/actions", response_model=Page[dict[str, Any]])
async def list_ai_actions(
    _: AILogRead,
    db: TenantDBSessionDep,
    filters: Annotated[AIActionFilters, Depends()],
    params: Annotated[PageParams, Depends()],
) -> Page[dict[str, Any]]:
    return await ai.admin.list_actions(get_context(), db, filters, params)


@router.get("/actions/export.csv", response_class=Response)
async def export_ai_actions(
    _: AILogRead, db: TenantDBSessionDep, filters: Annotated[AIActionFilters, Depends()]
) -> Response:
    export = await ai.admin.export_csv(get_context(), db, filters)
    return Response(
        content=export.content,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="ai-actions.csv"',
            "X-Export-Rows": str(export.rows),
            "X-Export-Truncated": "true" if export.truncated else "false",
        },
    )


@router.get("/actions/{action_id}", response_model=dict[str, Any])
async def get_ai_action(action_id: uuid.UUID, _: AILogRead, db: TenantDBSessionDep) -> dict[str, Any]:
    return await ai.admin.get_action(get_context(), db, action_id)


# ================================================================ Epic 35 (FR-182): commercial document extraction
from app.core.permissions import require_any  # ruff: ignore[module-import-not-at-top-of-file]
from app.schemas.ai import (  # ruff: ignore[module-import-not-at-top-of-file]
    CommercialExtractIn,
    CommercialExtractOut,
)

CommercialWrite = Annotated[Principal, Depends(require_any("contract:write", "po:write"))]


@router.post("/extract/commercial", response_model=CommercialExtractOut)
async def extract_commercial(body: CommercialExtractIn, _: CommercialWrite, db: TenantDBSessionDep) -> CommercialExtractOut:
    """Draft only (AD-6): the extracted fields are returned and kept in ``ai_actions``, nothing else is saved."""
    from app.services.ai import extraction

    return await extraction.extract_commercial_document(get_context(), db, body.file_id, body.doc_kind)
