# ruff: file-ignore[line-too-long]
"""Mobility erasure handler ``mobility.records`` (56), Epic 41 A6 (FR-258, AD-34).

The subject's rows are deleted: internal transfers, internal application overlays, the Employee link (a User linked
to the erased Candidate) and mobility profiles (worker profile of the Candidate; user profile of a linked User).
The P1 Application and any Placement created by a transfer belong to P1 / P2 and follow their own erasure.
"""

from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mobility import InternalTransfer, MobilityApplication, MobilityEmployeeLink, MobilityProfile
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


async def _linked_users(db: AsyncSession, ids: list[Any]) -> list[Any]:
    return list((await db.execute(select(MobilityEmployeeLink.user_id).where(MobilityEmployeeLink.candidate_id.in_(ids)))).scalars())


def _count(res: Any) -> int:
    return int(getattr(res, "rowcount", 0) or 0)


class MobilityHandler:
    name = "mobility.records"
    order = 56
    tables: tuple[str, ...] = ("mobility_applications", "internal_transfers", "mobility_employee_links", "mobility_profiles")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        users = await _linked_users(db, ids)
        counts: dict[str, int] = {}
        counts["internal_transfers"] = _count(await db.execute(delete(InternalTransfer).where(InternalTransfer.candidate_id.in_(ids))))
        counts["mobility_applications"] = _count(
            await db.execute(delete(MobilityApplication).where(MobilityApplication.candidate_id.in_(ids)))
        )
        profile_cond = (MobilityProfile.subject_kind == "worker") & MobilityProfile.subject_id.in_(ids)
        if users:
            profile_cond = or_(profile_cond, (MobilityProfile.subject_kind == "user") & MobilityProfile.subject_id.in_(users))
        counts["mobility_profiles"] = _count(await db.execute(delete(MobilityProfile).where(profile_cond)))
        counts["mobility_employee_links"] = _count(
            await db.execute(delete(MobilityEmployeeLink).where(MobilityEmployeeLink.candidate_id.in_(ids)))
        )
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        apps = list((await db.execute(select(MobilityApplication).where(MobilityApplication.candidate_id == ctx.candidate_id))).scalars())
        transfers = list((await db.execute(select(InternalTransfer).where(InternalTransfer.candidate_id == ctx.candidate_id))).scalars())
        profile = (
            await db.execute(
                select(MobilityProfile).where(MobilityProfile.subject_kind == "worker", MobilityProfile.subject_id == ctx.candidate_id)
            )
        ).scalar_one_or_none()
        if not apps and not transfers and profile is None:
            return None
        return {
            "internal_applications": [
                {"demand_id": str(a.demand_id), "status": a.status, "submitted_at": a.submitted_at, "rejection_reason": a.rejection_reason}
                for a in apps
            ],
            "internal_transfers": [{"effective_date": t.effective_date, "role": t.to_role_title} for t in transfers],
            "mobility_profile": {"grade": profile.grade, "location": profile.location} if profile else None,
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings: list[str] = []
        for model, label in ((MobilityApplication, "internal applications"), (InternalTransfer, "internal transfers"), (MobilityEmployeeLink, "employee links")):
            if (await db.execute(select(model.id).where(model.candidate_id.in_(ids)).limit(1))).first() is not None:
                findings.append(f"{label} remain")
        return findings


register_erasure_handler(MobilityHandler())
