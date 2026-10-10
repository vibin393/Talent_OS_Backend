# ruff: noqa: E501
"""Internal mobility (Epic 41, FR-209..FR-212): ``/api/v1/mobility/*``.

* Board / self-service -- **no permission key**: any active internal User (``board_enabled``): ``/mobility/openings*``,
  ``/mobility/me/profile``, ``/mobility/openings/{id}/apply``, ``/mobility/my-applications``,
  ``/mobility/applications/{id}/withdraw`` (own application only).
* Settings, eligibility rules, employee grades -- ``mobility:manage``.
* Recruiter views -- ``/mobility/applications?demand_id=`` and ``/by-application/{id}`` (demand read scope).
* Transfer confirmation (*) -- ``mobility:transfer`` and a human actor (403 ``human_only_action``).

Each endpoint calls one service function.
"""

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Query, UploadFile, status

from app.core.context import get_context
from app.dependencies.auth import CurrentUserDep, TenantDBSessionDep
from app.schemas.mobility import (
    ApplyIn,
    InternalApplicationOut,
    MobilitySettingsOut,
    MobilitySettingsPatch,
    MyApplicationOut,
    OpeningDetailOut,
    OpeningPage,
    OpeningRulesIn,
    OpeningRulesOut,
    ProfileImportOut,
    ProfileIn,
    ProfileOut,
    ProfilePreviewOut,
    TransferConfirmIn,
    TransferContextOut,
)
from app.services import files_service
from app.services.mobility import applications, board, employees, portal, settings, transfers
from app.services.mobility.common import require_user

router = APIRouter(prefix="/mobility", tags=["mobility"])
DB = TenantDBSessionDep
MAX_CSV_MB = 2


# ================================================================ settings / rules / profiles (mobility:manage)
@router.get("/settings", response_model=MobilitySettingsOut)
async def get_settings(_: CurrentUserDep, db: DB) -> Any:
    return await settings.get_settings(get_context(), db)


@router.patch("/settings", response_model=MobilitySettingsOut)
async def update_settings(body: MobilitySettingsPatch, _: CurrentUserDep, db: DB) -> Any:
    return await settings.update_settings(get_context(), db, body)


@router.get("/demands/{demand_id}/rules", response_model=OpeningRulesOut)
async def get_rules(demand_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await board.get_rules(get_context(), db, demand_id)


@router.put("/demands/{demand_id}/rules", response_model=OpeningRulesOut)
async def put_rules(demand_id: uuid.UUID, body: OpeningRulesIn, _: CurrentUserDep, db: DB) -> Any:
    return await board.put_rules(get_context(), db, demand_id, body)


@router.get("/profiles", response_model=list[ProfileOut])
async def list_profiles(_: CurrentUserDep, db: DB, subject_kind: Literal["user", "worker"] | None = None) -> Any:
    return await employees.list_profiles(get_context(), db, subject_kind)


@router.put("/profiles", response_model=ProfileOut)
async def upsert_profile(body: ProfileIn, _: CurrentUserDep, db: DB) -> Any:
    return await employees.upsert_profile(get_context(), db, body)


@router.post("/profiles/import", response_model=ProfileImportOut)
async def import_profiles(_: CurrentUserDep, db: DB, file: Annotated[UploadFile, File()], validate_only: bool = True) -> Any:
    content = await files_service.read_upload(file, MAX_CSV_MB)
    return await employees.import_profiles(get_context(), db, content, validate_only=validate_only)


# ================================================================ board (any active internal User)
@router.get("/openings", response_model=OpeningPage)
async def openings(
    principal: CurrentUserDep,
    db: DB,
    q: Annotated[str | None, Query(max_length=200)] = None,
    location: Annotated[str | None, Query(max_length=200)] = None,
    include_ineligible: bool = False,
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Any:
    require_user(get_context())
    return await portal.user_board(principal, db, q=q, location=location, include_ineligible=include_ineligible, page=page, page_size=page_size)


@router.get("/openings/{demand_id}", response_model=OpeningDetailOut)
async def opening(demand_id: uuid.UUID, principal: CurrentUserDep, db: DB) -> Any:
    require_user(get_context())
    return await portal.user_opening(principal, db, demand_id)


@router.get("/me/profile", response_model=ProfilePreviewOut)
async def my_profile(principal: CurrentUserDep, db: DB) -> Any:
    require_user(get_context())
    return await portal.user_profile(get_context(), principal, db)


@router.post("/openings/{demand_id}/apply", response_model=MyApplicationOut, status_code=status.HTTP_201_CREATED)
async def apply(demand_id: uuid.UUID, body: ApplyIn, principal: CurrentUserDep, db: DB) -> Any:
    require_user(get_context())
    return await portal.user_apply(get_context(), principal, db, demand_id, body)


@router.get("/my-applications", response_model=list[MyApplicationOut])
async def my_applications(principal: CurrentUserDep, db: DB) -> Any:
    require_user(get_context())
    return await portal.user_applications(principal, db)


@router.post("/applications/{mobility_application_id}/withdraw", response_model=MyApplicationOut)
async def withdraw(mobility_application_id: uuid.UUID, principal: CurrentUserDep, db: DB) -> Any:
    require_user(get_context())
    return await portal.user_withdraw(get_context(), principal, db, mobility_application_id)


# ================================================================ recruiter / manager views
@router.get("/applications", response_model=list[InternalApplicationOut])
async def list_applications(demand_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await applications.list_for_demand(get_context(), db, demand_id)


@router.get("/applications/by-application/{application_id}", response_model=InternalApplicationOut)
async def by_application(application_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await applications.by_application(get_context(), db, application_id)


@router.get("/applications/{mobility_application_id}/transfer-context", response_model=TransferContextOut)
async def transfer_context(mobility_application_id: uuid.UUID, _: CurrentUserDep, db: DB) -> Any:
    return await transfers.context(get_context(), db, mobility_application_id)


@router.post("/applications/{mobility_application_id}/confirm-transfer", response_model=InternalApplicationOut)
async def confirm_transfer(mobility_application_id: uuid.UUID, body: TransferConfirmIn, _: CurrentUserDep, db: DB) -> Any:
    return await transfers.confirm(get_context(), db, mobility_application_id, body)
