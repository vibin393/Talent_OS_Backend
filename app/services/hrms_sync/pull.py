# ruff: file-ignore[line-too-long, unused-function-argument]
"""HRMS status pull (Epic 71, story 71.3, spec A5): the daily ``pull_status`` sync job stores the employee code, HRMS
status and exit date on the sync record and turns disagreements into one Task per record (assignee: the Placement's
Account Manager, else its recruiter) with both values. Nothing in Talent OS changes from provider data."""

from datetime import date, datetime
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.candidates import Candidate
from app.models.hrms_sync import HrmsSyncRecord
from app.models.placements import Placement
from app.services import integrations_service
from app.services.hrms_sync.common import fix_it_task, notify, now
from app.services.hrms_sync.push import find_record

__all__ = ["MAX_PAGES", "compare", "pull_status"]

MAX_PAGES = 20
_ACTIVE = ("active", "joining_confirmed", "offboarding")
_LABEL = {"pre_joining": "Pre-joining", "joining_confirmed": "Joining confirmed", "active": "Active", "offboarding": "Offboarding", "ended": "Ended", "dropped": "Dropped"}


def _d(value: date | None) -> str:
    return value.strftime("%d %b %Y").lstrip("0") if value else "no date"


def compare(p: Placement, hrms_status: str, hrms_exit: date | None) -> str:
    """``none | status | exit_date | both`` (spec A5)."""
    if hrms_status == "exited" and p.status in _ACTIVE:
        if p.end_date is None:
            return "status"
        if hrms_exit is not None and hrms_exit != p.end_date:
            return "both" if p.status == "active" else "exit_date"
        return "none"
    if hrms_status == "active" and p.status == "ended":
        return "status"
    if hrms_status == "exited" and p.status == "ended" and hrms_exit is not None and p.end_date is not None and hrms_exit != p.end_date:
        return "exit_date"
    return "none"


def _title(name: str, p: Placement, rec: HrmsSyncRecord) -> str:
    if rec.hrms_status == "exited":
        hrms = f"exited on {_d(rec.hrms_exit_date)}" if rec.hrms_exit_date else "exited"
    else:
        hrms = f"as {rec.hrms_status or 'unknown'}"
    talent = _LABEL.get(p.status, p.status)
    if p.end_date is not None and rec.mismatch in ("exit_date", "both"):
        talent = f"{talent} (ends {_d(p.end_date)})"
    return f"HRMS shows {name} {hrms}; Talent OS shows {talent}"


async def _flag(db: AsyncSession, rec: HrmsSyncRecord, p: Placement, accountable: Any) -> None:
    cand = await db.get(Candidate, p.candidate_id)
    name = str(getattr(cand, "full_name", None) or "the worker")
    title = _title(name, p, rec)
    owner = p.account_manager_user_id or p.recruiter_user_id or accountable
    task_id = await fix_it_task(
        db, owner_id=owner, entity_type="hrms_sync", entity_id=rec.id, title=title,
        notes=f"Open placement: /placements/{p.id}. Mark resolved when Talent OS and the HR system agree.",
    )  # fmt: skip
    if task_id is not None and task_id != rec.mismatch_task_id:
        rec.mismatch_task_id = task_id
        notify(rec.tenant_id, "hrms_mismatch", {"event_id": f"hrms_mismatch:{rec.id}:{task_id}", "record_id": str(rec.id), "placement_id": str(p.id), "user_id": str(owner) if owner else None, "title": title})


async def pull_status(ctx: RequestContext, db: AsyncSession, run: Any) -> integrations_service.SyncResult:
    from app.models.oauth import AppInstallation

    inst = await db.get(AppInstallation, run.installation_id)
    accountable = inst.accountable_user_id if inst is not None else None
    raw_since = (run.cursor or {}).get("since")
    since = datetime.fromisoformat(raw_since) if raw_since else None
    started = now()
    cursor: str | None = (run.cursor or {}).get("page")
    processed = updated = unknown = 0
    for _ in range(MAX_PAGES):
        items, cursor = await run.adapter.pull_status(since, cursor)
        for dto in items:
            link = await integrations_service.find_link(db, run.installation_id, "worker", external_id=dto.employee_ref)
            rec = await find_record(db, run.installation_id, link.local_id) if link is not None else None
            if rec is None:
                unknown += 1
                continue
            processed += 1
            p = await db.get(Placement, rec.placement_id)
            if p is None:
                continue
            rec.employee_code = (dto.employee_code or rec.employee_code or "")[:60] or None
            rec.hrms_status, rec.hrms_exit_date, rec.last_pulled_at = dto.status, dto.exit_date, now()
            mismatch = compare(p, dto.status, dto.exit_date)
            if mismatch != rec.mismatch:
                updated += 1
            rec.mismatch = mismatch
            if mismatch != "none":
                await _flag(db, rec, p, accountable)
        if cursor is None:
            break
    await db.flush()
    if unknown:
        logger.info("HRMS pull for installation {}: {} unknown employee refs ignored", run.installation_id, unknown)
    next_cursor = {"since": started.isoformat(), "page": None} if cursor is None else {"since": raw_since, "page": cursor}
    return integrations_service.SyncResult(processed=processed, updated=updated, skipped=unknown, next_cursor=next_cursor)
