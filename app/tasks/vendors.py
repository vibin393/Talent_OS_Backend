"""Vendors Celery tasks (Epic 17; AD-9: ``tenant_id`` first, idempotent; queue ``maintenance`` for the beat,
``interactive`` for after-commit reactions).

* ``vendors.check_documents_all`` (beat, daily) -> ``vendors.check_documents(tenant_id)``: expiring / expired
  vendor documents, suspension after the grace period.
* ``vendors.work(tenant_id, action, ref_id)``: after a submission commits -- review SLA clock, stop the
  release's
  first-submission clock, DPDP notice link + email, resume parse into a draft; ``resume_revised``.
* ``vendors.demand_closed`` / ``demand_changed`` / ``stage_changed`` / ``interview_changed`` /
  ``placement_changed``: event reactions (system actor, internal session).
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.core.celery import celery_app
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.tasks.runtime import enqueue, run, tenant_ids

QUEUE = "interactive"


def _ctx(tenant_id: uuid.UUID) -> RequestContext:
    return RequestContext(tenant_id=tenant_id, actor_kind="system")


# ---------------------------------------------------------------- daily document check (maintenance)
async def _check_documents(tenant_id: uuid.UUID) -> dict[str, Any]:
    from app.services.vendors import records

    async with tenant_session(tenant_id) as db:
        return await records.check_documents(_ctx(tenant_id), db)


@celery_app.task(name="vendors.check_documents", acks_late=True, queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def check_documents(tenant_id: str) -> dict[str, Any]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _check_documents(tid), tenant_id=tid))


@celery_app.task(name="vendors.check_documents_all", queue="maintenance")  # type: ignore[misc,untyped-decorator,unused-ignore]
def check_documents_all() -> int:
    ids: list[uuid.UUID] = run(tenant_ids)
    for tid in ids:
        enqueue(check_documents, str(tid))
    return len(ids)


# ---------------------------------------------------------------- submission work
async def _after_submission(tenant_id: uuid.UUID, submission_id: uuid.UUID) -> dict[str, Any]:
    from app.models.vendors import VendorSubmission
    from app.services import sla_service
    from app.services.vendors import notice

    ctx = _ctx(tenant_id)
    out: dict[str, Any] = {}
    async with tenant_session(tenant_id) as db:
        sub = (
            await db.execute(select(VendorSubmission).where(VendorSubmission.id == submission_id))
        ).scalar_one_or_none()
        if sub is None:
            return {"missing": True}
        await sla_service.stop_clock(ctx, db, "vendor_release", sub.vendor_release_id, "first_submission")
        if sub.status in ("submitted", "under_review"):
            try:
                clock = await sla_service.start_clock(
                    ctx, db, "vendor_submission", sub.id, "review", scope={"vendor_id": str(sub.vendor_id)}
                )
            except ValueError:
                clock = None
            if clock is not None:
                sub.review_due_at = clock.due_at
        out["notice"] = await notice.issue_and_email(ctx, db, sub.id)
    out["parsed"] = await _parse_resume(tenant_id, submission_id)
    return out


async def _parse_resume(tenant_id: uuid.UUID, submission_id: uuid.UUID) -> bool:
    """Parse the resume into a candidate draft (P1 AI rules); a failure never blocks the submission."""
    from app.core import storage
    from app.models.files import StoredFile
    from app.models.vendors import VendorSubmission
    from app.services.candidates import parsing

    try:
        async with tenant_session(tenant_id) as db:
            sub = (
                await db.execute(
                    select(VendorSubmission).where(VendorSubmission.id == submission_id).with_for_update()
                )
            ).scalar_one_or_none()
            if sub is None or sub.resume_file_id is None:
                return False
            f = await db.get(StoredFile, sub.resume_file_id)
            if f is None or f.deleted_at is not None:
                return False
            data = await storage.get_storage().get(f.storage_key)
            if sub.status == "accepted" and sub.candidate_id is not None:
                await parsing.ingest_file(
                    _ctx(tenant_id), db, file_id=f.id, data=data, candidate_id=sub.candidate_id
                )
                return True
            ing = await parsing.ingest_file(_ctx(tenant_id), db, file_id=f.id, data=data, candidate_id=None)
            sub.resume_draft_id = ing.resume.id
            return True
    except Exception:
        logger.warning("Vendor submission {} resume parse failed", submission_id)
        return False


async def _work(tenant_id: uuid.UUID, action: str, ref_id: uuid.UUID) -> dict[str, Any]:
    if action == "after_submission":
        return await _after_submission(tenant_id, ref_id)
    if action == "resume_revised":
        return {"parsed": await _parse_resume(tenant_id, ref_id)}
    return {"unknown": action}


@celery_app.task(name="vendors.work", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def work(tenant_id: str, action: str, ref_id: str) -> dict[str, Any]:
    tid = uuid.UUID(str(tenant_id))
    return dict(run(lambda: _work(tid, action, uuid.UUID(ref_id)), tenant_id=tid))


# ---------------------------------------------------------------- event reactions
async def _demand_closed(tenant_id: uuid.UUID, demand_id: uuid.UUID) -> int:
    from app.services.vendors import releases

    async with tenant_session(tenant_id) as db:
        return await releases.close_for_demand(_ctx(tenant_id), db, demand_id)


@celery_app.task(name="vendors.demand_closed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def demand_closed(tenant_id: str, demand_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _demand_closed(tid, uuid.UUID(demand_id)), tenant_id=tid))


async def _demand_changed(tenant_id: uuid.UUID, demand_id: uuid.UUID) -> int:
    from app.services.vendors import releases

    async with tenant_session(tenant_id) as db:
        return await releases.refresh_snapshots(_ctx(tenant_id), db, demand_id)


@celery_app.task(name="vendors.demand_changed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def demand_changed(tenant_id: str, demand_id: str) -> int:
    tid = uuid.UUID(str(tenant_id))
    return int(run(lambda: _demand_changed(tid, uuid.UUID(demand_id)), tenant_id=tid))


async def _stage_changed(tenant_id: uuid.UUID, application_id: uuid.UUID, to_key: str) -> bool:
    from app.services.vendors import progress

    async with tenant_session(tenant_id) as db:
        return await progress.on_stage_changed(_ctx(tenant_id), db, application_id, to_key)


@celery_app.task(name="vendors.stage_changed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def stage_changed(tenant_id: str, application_id: str, to_key: str) -> bool:
    tid = uuid.UUID(str(tenant_id))
    return bool(run(lambda: _stage_changed(tid, uuid.UUID(application_id), to_key), tenant_id=tid))


async def _interview_changed(tenant_id: uuid.UUID, application_id: uuid.UUID) -> bool:
    from app.services.vendors import progress

    async with tenant_session(tenant_id) as db:
        return await progress.on_interview_changed(_ctx(tenant_id), db, application_id)


@celery_app.task(name="vendors.interview_changed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def interview_changed(tenant_id: str, application_id: str) -> bool:
    tid = uuid.UUID(str(tenant_id))
    return bool(run(lambda: _interview_changed(tid, uuid.UUID(application_id)), tenant_id=tid))


async def _placement_changed(tenant_id: uuid.UUID, placement_id: uuid.UUID, kind: str) -> bool:
    from app.services.vendors import progress

    async with tenant_session(tenant_id) as db:
        return await progress.on_placement(
            _ctx(tenant_id), db, placement_id, dropped=kind == "PlacementDropped"
        )


@celery_app.task(name="vendors.placement_changed", acks_late=True)  # type: ignore[misc,untyped-decorator,unused-ignore]
def placement_changed(tenant_id: str, placement_id: str, kind: str) -> bool:
    tid = uuid.UUID(str(tenant_id))
    return bool(run(lambda: _placement_changed(tid, uuid.UUID(placement_id), kind), tenant_id=tid))
