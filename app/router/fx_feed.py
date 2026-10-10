"""Live FX feed (Epic 51, FR-255): ``/fx-feed/*``. Writes need ``fx_feed:configure``; reads also allow
``legal_entity:read``. Each endpoint calls one service function."""

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.fx_feed import FxFeedRunOut, FxFeedSettingsIn, FxFeedSettingsOut
from app.services.legal_entities import fx_feed

router = APIRouter(prefix="/fx-feed", tags=["fx-feed"])
DB = TenantDBSessionDep
Configure = Annotated[Principal, Depends(require("fx_feed:configure"))]
Read = Annotated[Principal, Depends(require_any("fx_feed:configure", "legal_entity:read"))]
_IST = timezone(timedelta(hours=5, minutes=30))


@router.get("/settings", response_model=FxFeedSettingsOut)
async def get_settings(_: Read, db: DB) -> Any:
    return await fx_feed.get_settings(get_context(), db)


@router.put("/settings", response_model=FxFeedSettingsOut)
async def put_settings(body: FxFeedSettingsIn, _: Configure, db: DB) -> Any:
    return await fx_feed.update_settings(get_context(), db, body)


@router.post("/fetch-now", response_model=FxFeedRunOut)
async def fetch_now(_: Configure, db: DB) -> Any:
    return await fx_feed.fetch(get_context(), db, datetime.now(_IST).date(), manual_trigger=True)


@router.get("/runs", response_model=list[FxFeedRunOut])
async def list_runs(_: Read, db: DB) -> Any:
    return await fx_feed.list_runs(get_context(), db)
