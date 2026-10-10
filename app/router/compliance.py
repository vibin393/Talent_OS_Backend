"""Compliance requirements & credentials (Epic 21, FR-110/115/116).

Permissions: ``compliance:read`` (dashboard, lists, checklists), ``compliance:configure`` (credential types,
requirements, settings, evaluation runs, manual recompute), ``credential:write`` (record, renew),
``credential:verify`` (verify, redaction review; human only), ``compliance:waive`` (waive a checklist item;
human only), ``identity:view`` (unmask, pending Aadhaar images). Each endpoint calls one service function.
"""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.compliance import (
    ComplianceDashboardOut,
    ComplianceSettingsOut,
    ComplianceSettingsUpdate,
    CredentialIn,
    CredentialOut,
    CredentialTypeIn,
    CredentialTypeOut,
    CredentialTypeUpdate,
    EvaluationRunOut,
    PlacementComplianceOut,
    RedactionIn,
    RenewIn,
    RequirementIn,
    RequirementOut,
    RequirementUpdate,
    UnmaskOut,
    VerifyIn,
    WaiveIn,
)
from app.services import files_service
from app.services.compliance import checklist, credentials, dashboard, recompute, requirements
from app.services.compliance.common import not_found

router = APIRouter(prefix="/compliance", tags=["compliance"])
credentials_router = APIRouter(prefix="/credentials", tags=["compliance"])
candidates_router = APIRouter(prefix="/candidates", tags=["compliance"])
placements_router = APIRouter(prefix="/placements", tags=["compliance"])
DB = TenantDBSessionDep

Read = Annotated[Principal, Depends(require("compliance:read"))]
Configure = Annotated[Principal, Depends(require("compliance:configure"))]
Write = Annotated[Principal, Depends(require("credential:write"))]
Verify = Annotated[Principal, Depends(require("credential:verify"))]
Waive = Annotated[Principal, Depends(require("compliance:waive"))]
NO_STORE = {"Cache-Control": "no-store"}


async def _file(file: UploadFile | None) -> tuple[bytes, str | None] | None:
    if file is None:
        return None
    return await files_service.read_upload(file), file.filename


async def _one(db: Any, credential_id: uuid.UUID) -> CredentialOut:
    c = await credentials.load(db, credential_id)
    return (await credentials.outs(db, [c]))[0]


# ================================================================ configuration
@router.get("/credential-types", response_model=list[CredentialTypeOut])
async def list_credential_types(_: Read, db: DB, include_archived: bool = False) -> Any:
    return await requirements.list_types(db, include_archived=include_archived)


@router.post("/credential-types", response_model=CredentialTypeOut, status_code=status.HTTP_201_CREATED)
async def create_credential_type(body: CredentialTypeIn, _: Configure, db: DB) -> Any:
    return await requirements.create_type(get_context(), db, body)


@router.patch("/credential-types/{type_id}", response_model=CredentialTypeOut)
async def update_credential_type(type_id: uuid.UUID, body: CredentialTypeUpdate, _: Configure, db: DB) -> Any:
    return await requirements.update_type(get_context(), db, type_id, body)


@router.get("/requirements", response_model=list[RequirementOut])
async def list_requirements(
    _: Read, db: DB, client_id: uuid.UUID | None = None, include_archived: bool = False
) -> Any:
    return await requirements.list_requirements(db, client_id=client_id, include_archived=include_archived)


@router.post("/requirements", response_model=RequirementOut, status_code=status.HTTP_201_CREATED)
async def create_requirement(body: RequirementIn, _: Configure, db: DB) -> Any:
    return await requirements.create_requirement(get_context(), db, body)


@router.get("/requirements/{requirement_id}", response_model=RequirementOut)
async def get_requirement(requirement_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await requirements.get_requirement(db, requirement_id)


@router.patch("/requirements/{requirement_id}", response_model=RequirementOut)
async def update_requirement(requirement_id: uuid.UUID, body: RequirementUpdate, _: Configure, db: DB) -> Any:
    return await requirements.update_requirement(get_context(), db, requirement_id, body)


@router.get("/settings", response_model=ComplianceSettingsOut)
async def get_settings(_: Read, db: DB) -> Any:
    return await requirements.get_settings(db)


@router.put("/settings", response_model=ComplianceSettingsOut)
async def update_settings(body: ComplianceSettingsUpdate, _: Configure, db: DB) -> Any:
    return await requirements.update_settings(get_context(), db, body)


@router.get("/evaluation-runs/{run_id}", response_model=EvaluationRunOut)
async def get_evaluation_run(run_id: uuid.UUID, _: Configure, db: DB) -> Any:
    from app.models.compliance import ComplianceEvaluationRun
    from app.models.placements import Placement

    run = await db.get(ComplianceEvaluationRun, run_id)
    if run is None:
        raise not_found("Evaluation run")
    rows = []
    if run.newly_non_compliant:
        from sqlalchemy import select

        for p in (
            await db.execute(select(Placement).where(Placement.id.in_(run.newly_non_compliant)))
        ).scalars():
            rows.append({"placement_id": str(p.id), "human_id": p.human_id, "status": p.status})
    return EvaluationRunOut(
        id=run.id,
        requirement_id=run.requirement_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        placements_evaluated=run.placements_evaluated,
        newly_non_compliant=list(run.newly_non_compliant),
        placements=rows,
    )


@router.post("/recompute")
async def run_recompute(_: Configure, db: DB) -> Any:
    return await recompute.run(get_context(), db, force=True)


@router.get("/dashboard", response_model=ComplianceDashboardOut)
async def get_dashboard(
    _: Read, db: DB, client_id: uuid.UUID | None = None, legal_entity_id: uuid.UUID | None = None
) -> Any:
    return await dashboard.dashboard(get_context(), db, client_id=client_id, legal_entity_id=legal_entity_id)


# ================================================================ credentials
@candidates_router.get("/{candidate_id}/credentials", response_model=list[CredentialOut])
async def list_candidate_credentials(candidate_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await credentials.list_for_candidate(db, candidate_id)


@candidates_router.post(
    "/{candidate_id}/credentials", response_model=CredentialOut, status_code=status.HTTP_201_CREATED
)
async def record_credential(
    candidate_id: uuid.UUID,
    _: Write,
    db: DB,
    type_key: Annotated[str, Form(min_length=1, max_length=60)],
    number: Annotated[str | None, Form(max_length=60)] = None,
    issuer: Annotated[str | None, Form(max_length=200)] = None,
    issued_on: Annotated[date | None, Form()] = None,
    expires_on: Annotated[date | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> Any:
    data = CredentialIn(
        type_key=type_key, number=number, issuer=issuer, issued_on=issued_on, expires_on=expires_on
    )
    c = await credentials.record(get_context(), db, candidate_id, data, await _file(file))
    return (await credentials.outs(db, [c]))[0]


@credentials_router.get("", response_model=list[CredentialOut])
async def list_all_credentials(
    _: Read,
    db: DB,
    expiring_within: Annotated[int | None, Query(ge=0, le=365)] = None,
    status_: Annotated[str | None, Query(alias="status")] = None,
    type_key: Annotated[str | None, Query(alias="type")] = None,
    candidate_id: uuid.UUID | None = None,
) -> Any:
    return await credentials.list_credentials(
        db, expiring_within=expiring_within, status=status_, type_key=type_key, candidate_id=candidate_id
    )


@credentials_router.get("/{credential_id}", response_model=CredentialOut)
async def get_credential(credential_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await _one(db, credential_id)


@credentials_router.post(
    "/{credential_id}/renew", response_model=CredentialOut, status_code=status.HTTP_201_CREATED
)
async def renew_credential(
    credential_id: uuid.UUID,
    _: Write,
    db: DB,
    number: Annotated[str | None, Form(max_length=60)] = None,
    issuer: Annotated[str | None, Form(max_length=200)] = None,
    issued_on: Annotated[date | None, Form()] = None,
    expires_on: Annotated[date | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> Any:
    data = RenewIn(number=number, issuer=issuer, issued_on=issued_on, expires_on=expires_on)
    c = await credentials.renew(get_context(), db, credential_id, data, await _file(file))
    return (await credentials.outs(db, [c]))[0]


@credentials_router.post("/{credential_id}/verify", response_model=CredentialOut)
async def verify_credential(credential_id: uuid.UUID, body: VerifyIn, _: Verify, db: DB) -> Any:
    c = await credentials.verify(get_context(), db, credential_id, body.decision, body.note)
    return (await credentials.outs(db, [c]))[0]


@credentials_router.post("/{credential_id}/redaction", response_model=CredentialOut)
async def review_redaction(credential_id: uuid.UUID, body: RedactionIn, _: Verify, db: DB) -> Any:
    c = await credentials.review_redaction(get_context(), db, credential_id, body.decision)
    return (await credentials.outs(db, [c]))[0]


@credentials_router.post("/{credential_id}/unmask", response_model=UnmaskOut)
async def unmask_credential(credential_id: uuid.UUID, response: Response, _: Read, db: DB) -> Any:
    response.headers.update(NO_STORE)
    return UnmaskOut(number=await credentials.unmask(get_context(), db, credential_id))


@credentials_router.get("/{credential_id}/document", response_class=Response)
async def credential_document(credential_id: uuid.UUID, _: Read, db: DB) -> Response:
    c = await credentials.load(db, credential_id)
    data, name, ctype = await credentials.document_bytes(get_context(), db, c)
    return Response(
        content=data,
        media_type=ctype,
        headers={
            "Content-Disposition": f'inline; filename="{name}"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ================================================================ placement checklist
@placements_router.get("/{placement_id}/compliance", response_model=PlacementComplianceOut)
async def placement_compliance(placement_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await checklist.get_checklist(get_context(), db, placement_id)


@placements_router.post(
    "/{placement_id}/compliance-items/{item_id}/waive", response_model=PlacementComplianceOut
)
async def waive_item(placement_id: uuid.UUID, item_id: uuid.UUID, body: WaiveIn, _: Waive, db: DB) -> Any:
    return await checklist.waive(get_context(), db, placement_id, item_id, body.reason)
