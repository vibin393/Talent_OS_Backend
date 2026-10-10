# ruff: noqa: E501
"""Candidate / Worker self-service: "Where your data went" (Epic 79 story 79.5, FR-408).

Lists the Installations the signed-in subject was exposed to -- display name, publisher, purpose(s), destination
country, first / last shared. No other subjects and no internal ids are revealed.
"""

from fastapi import APIRouter
from sqlalchemy import select

from app.dependencies.candidate import CandidateDBDep, CandidateDep
from app.models.ecosystem import SubjectExposure
from app.models.oauth import AppInstallation
from app.schemas.ecosystem_governance import DataRecipientOut

router = APIRouter(prefix="/portal/privacy", tags=["portal-privacy"])


@router.get("/data-recipients", response_model=list[DataRecipientOut])
async def data_recipients(principal: CandidateDep, db: CandidateDBDep) -> list[DataRecipientOut]:
    rows = (
        await db.execute(
            select(SubjectExposure, AppInstallation)
            .join(
                AppInstallation,
                (AppInstallation.id == SubjectExposure.installation_id)
                & (AppInstallation.tenant_id == SubjectExposure.tenant_id),
                isouter=True,
            )
            .where(
                SubjectExposure.subject_type == "candidate",
                SubjectExposure.subject_id == principal.candidate_id,
                SubjectExposure.installation_id.isnot(None),
            )
        )
    ).all()
    out: list[DataRecipientOut] = []
    for exposure, inst in rows:
        config = (getattr(inst, "config", {}) or {}) if inst is not None else {}
        out.append(
            DataRecipientOut(
                installation=(inst.display_name if inst is not None else "A connected system"),
                publisher=("platform" if inst is not None and inst.kind == "connector" else "partner") if inst else None,
                purposes=list(config.get("purposes", []) or []),
                destination_country=config.get("destination_country"),
                first_at=exposure.first_at,
                last_at=exposure.last_at,
            )
        )
    return out


__all__ = ["router"]
