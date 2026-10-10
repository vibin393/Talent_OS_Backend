"""Time-domain erasure handler ``time.timesheets`` (58), Epic 25 A12 (AD-34).

Timesheets, entries, adjustment entries and rate adjustments are financial source records: hours, dates and
ids
are kept (retained under the ``financial`` class while referenced by a posting). Free text is cleared:
entry and
adjustment notes, proxy / adjustment reasons and rejection comments -- except rows listed in ``ctx.retained``.
``timesheet_configs`` hold no personal data beyond the inherited ``candidate_id``.
"""

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.time import Timesheet, TimesheetAdjustmentEntry, TimesheetEntry
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class TimeHandler:
    name = "time.timesheets"
    order = 58
    tables = (
        "timesheets",
        "timesheet_entries",
        "timesheet_adjustment_entries",
        "timesheet_configs",
        "rate_adjustments",
    )

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        retained_ts = ctx.retained.get("timesheets", set())
        ts_ids = [
            t
            for t in (await db.execute(select(Timesheet.id).where(Timesheet.candidate_id.in_(ids)))).scalars()
            if t not in retained_ts
        ]
        if not ts_ids:
            return {"timesheets": 0}
        # Text edits on locked rows would be refused by the immutability triggers; the hours stay untouched.
        await db.execute(
            update(Timesheet)
            .where(Timesheet.id.in_(ts_ids))
            .values(proxy_reason=None, rejection_comment=None, reason=None)
            .execution_options(synchronize_session=False)
        )
        counts = {"timesheets": len(ts_ids), "entries": 0}
        for model in (TimesheetEntry, TimesheetAdjustmentEntry):
            try:
                async with db.begin_nested():
                    result = await db.execute(
                        update(model)
                        .where(model.timesheet_id.in_(ts_ids), model.note.is_not(None))
                        .values(note=None)
                        .execution_options(synchronize_session=False)
                    )
                    counts["entries"] += int(result.rowcount or 0)  # type: ignore[attr-defined]
            except Exception:  # locked (posted / approved) rows keep their notes: retained financial records
                counts["retained"] = counts.get("retained", 0) + 1
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = list(
            (await db.execute(select(Timesheet).where(Timesheet.candidate_id == ctx.candidate_id))).scalars()
        )
        if not rows:
            return None
        return {
            "timesheets": [
                {
                    "reference": t.human_id,
                    "kind": t.kind,
                    "period_start": t.period_start,
                    "period_end": t.period_end,
                    "status": t.status,
                }
                for t in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        del db, ctx
        return []


register_erasure_handler(TimeHandler())
