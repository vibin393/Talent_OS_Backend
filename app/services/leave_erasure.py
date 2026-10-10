"""Leave erasure handler ``time.leave`` (59), Epic 26 A9 (AD-34).

* ``leave_requests``: dates, days and statuses are kept (they explain Timesheet leave rows and the balance
  ledger);
  the free text -- ``reason``, ``proxy_reason``, ``decision_comment`` -- is nulled.
* ``leave_balance_entries`` / ``leave_balances``: retained under the ``financial`` class while the Worker has
  retained payroll / timesheet records (``ctx.retained``); otherwise the cache rows are deleted and the
  append-only ledger keeps only ids, days and dates (its ``reason`` text is cleared -- the immutability
  trigger
  allows only that column to change).
* ``worker_optional_holidays``: deleted (a preference, no financial meaning).
"""

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.time import LeaveBalance, LeaveBalanceEntry, LeaveRequest, WorkerOptionalHoliday
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class LeaveHandler:
    name = "time.leave"
    order = 59
    tables = ("leave_requests", "leave_balance_entries", "leave_balances", "worker_optional_holidays")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        retained = bool(ctx.retained.get("timesheets") or ctx.retained.get("leave_balance_entries"))
        req = await db.execute(
            update(LeaveRequest)
            .where(LeaveRequest.candidate_id.in_(ids))
            .values(reason=None, proxy_reason=None, decision_comment=None)
            .execution_options(synchronize_session=False)
        )
        ledger = await db.execute(
            update(LeaveBalanceEntry)
            .where(LeaveBalanceEntry.candidate_id.in_(ids), LeaveBalanceEntry.reason.is_not(None))
            .values(reason=None)
            .execution_options(synchronize_session=False)
        )
        picks = await db.execute(
            delete(WorkerOptionalHoliday)
            .where(WorkerOptionalHoliday.candidate_id.in_(ids))
            .execution_options(synchronize_session=False)
        )
        cache = 0
        if not retained:
            result = await db.execute(
                delete(LeaveBalance)
                .where(LeaveBalance.candidate_id.in_(ids))
                .execution_options(synchronize_session=False)
            )
            cache = int(result.rowcount or 0)  # type: ignore[attr-defined]
        return {
            "leave_requests": int(req.rowcount or 0),  # type: ignore[attr-defined]
            "ledger_reasons": int(ledger.rowcount or 0),  # type: ignore[attr-defined]
            "optional_holidays": int(picks.rowcount or 0),  # type: ignore[attr-defined]
            "balance_rows": cache,
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = list(
            (
                await db.execute(select(LeaveRequest).where(LeaveRequest.candidate_id == ctx.candidate_id))
            ).scalars()
        )
        if not rows:
            return None
        return {
            "leave_requests": [
                {
                    "reference": r.human_id,
                    "start_date": r.start_date,
                    "end_date": r.end_date,
                    "days": str(r.days),
                    "status": r.status,
                    "reason": r.reason,
                }
                for r in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        left = (
            await db.execute(
                select(LeaveRequest.id)
                .where(LeaveRequest.candidate_id.in_(ids), LeaveRequest.reason.is_not(None))
                .limit(1)
            )
        ).scalar_one_or_none()
        return ["leave_requests.reason not cleared"] if left is not None else []


register_erasure_handler(LeaveHandler())
