# ruff: noqa: E501
"""HRMS sync erasure handler ``hrms_sync.records`` (order 73), Epic 71 (spec A10, FR-410).

``hrms_sync_records`` hold ids, statuses and sanitised messages only. On a Worker's erasure the employee code and the
last provider message are cleared; ids and statuses stay (statutory employment records are kept by law in the HR
system -- no downstream erasure request is sent; the DSR record states the reason). ``connector_links`` are handled by
the foundation handler (order 70). A top-level module so ``privacy`` never imports the Phase 5 package.
"""

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hrms_sync import HrmsSyncRecord
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

RETENTION_NOTE = "Employment/tax records kept by law in the HR system"


class HrmsSyncRecordsHandler:
    name = "hrms_sync.records"
    order = 73
    tables = ("hrms_sync_records",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"records": 0}
        ids = await subject_ids(db, ctx.candidate_id)
        result = await db.execute(
            update(HrmsSyncRecord).where(HrmsSyncRecord.candidate_id.in_(ids)).values(employee_code=None, last_message=None, field_hashes={})
        )
        await db.flush()
        return {"records": int(getattr(result, "rowcount", 0) or 0)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        rows = (await db.execute(select(HrmsSyncRecord).where(HrmsSyncRecord.candidate_id == ctx.candidate_id))).scalars()
        items = [
            {"placement_id": str(r.placement_id), "status": r.status, "employee_code": r.employee_code, "hrms_status": r.hrms_status, "last_pushed_at": r.last_pushed_at.isoformat() if r.last_pushed_at else None}
            for r in rows
        ]
        return {"hrms_sync_records": items, "note": RETENTION_NOTE} if items else None

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await subject_ids(db, ctx.candidate_id)
        left = (
            await db.execute(select(HrmsSyncRecord.id).where(HrmsSyncRecord.candidate_id.in_(ids), HrmsSyncRecord.employee_code.is_not(None)).limit(1))
        ).first()
        return ["HRMS employee codes remain"] if left else []


register_erasure_handler(HrmsSyncRecordsHandler())
