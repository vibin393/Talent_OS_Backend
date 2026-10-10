# ruff: noqa: E501, D400, D415
"""Job board sync handlers (Epic 70, spec A3 / A5), registered with
``integrations_service.register_sync_handler("job_board", <job>, fn)``:

* ``publish`` / ``update`` (subject ``board_posting``): rebuild the DTO from the database (never from the job
  payload), push, link, map the board status (``pending`` keeps ``publishing``), meter ``job_board_posting`` on the
  first successful publish. A Manual adapter leaves the posting for "Copy posting pack" / "Record manually".
* ``close`` (subject ``board_posting`` for a user close; ``demand`` from the outbox events ``posting.closed`` /
  ``demand.status_changed``): closes every open Board Posting of the Installation for that demand when the demand
  left Open or the careers posting was unpublished (setting ``auto_close_on_demand_change``). Manual postings get a
  Task "Close {title} on {board} manually".
* ``mark_out_of_date`` (``posting.published``): recomputes ``content_sha256`` -> ``local_revision += 1`` (no push).
* ``status`` (hourly): status, views, applies, expiry, credits snapshot.
* ``applicants`` (Naukri, every 15 min): ``fetch_applicants`` pages -> ``ingest.ingest_one``.

The job listener turns a dead / permanent / auth-failed push into ``failed`` with the sanitised board message and
notifies the publisher (``board_posting_failed``).
"""

import uuid
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.core.events import emit
from app.core.integrations import ConnectorPermanentError, ManualActionRequired
from app.events.job_distribution import BoardPostingPublished
from app.models.demands import Demand, DemandPosting
from app.models.job_distribution import BoardCreditSnapshot, BoardPosting
from app.services import integrations_service, metering_service
from app.services.integrations_service import SyncError, SyncResult, SyncRun
from app.services.job_distribution import mapping
from app.services.job_distribution.common import (
    CLOSED_STATUSES,
    board_label,
    board_settings,
    now,
    safe_message,
    tenant_of,
)
from app.services.job_distribution.postings import open_for_demand, set_status

STATUS_MAP = {"pending": "publishing", "live": "live", "expired": "expired", "closed": "closed", "rejected": "rejected", "failed": "failed"}
DEMAND_CLOSE_REASONS = {"on_hold": "demand_on_hold", "filled": "demand_filled", "closed": "demand_closed", "cancelled": "demand_closed"}
PUSH_OPS = ("sync:publish", "sync:update", "sync:close")


async def _snapshot(db: AsyncSession, bp: BoardPosting, credits: int | None) -> None:
    if credits is None:
        return
    db.add(BoardCreditSnapshot(id=uuid.uuid4(), tenant_id=bp.tenant_id, installation_id=bp.installation_id, config_id=bp.config_id, credits_left=int(credits), observed_at=now()))


def _apply_ref(db: AsyncSession, bp: BoardPosting, ref: Any) -> None:
    if ref.url:
        bp.board_url = ref.url
    if ref.expires_at is not None:
        bp.expires_at = ref.expires_at
    if ref.views is not None:
        bp.views = int(ref.views)
    if ref.applies is not None:
        bp.applies_reported = int(ref.applies)
    target = STATUS_MAP.get(str(ref.status), bp.status)
    if target == "live" and bp.status == "expiring":
        target = "expiring"
    if target in ("rejected", "failed"):
        bp.last_error_code = "board_rejected"
        bp.last_error_message = safe_message(ref.message) or "Rejected by the board"
    set_status(db, bp, target, reason="board" if target == "closed" and not bp.close_reason else None)


async def _subject_posting(db: AsyncSession, run: SyncRun) -> BoardPosting | None:
    if run.subject is None or run.subject[0] != "board_posting":
        return None
    return (await db.execute(select(BoardPosting).where(BoardPosting.id == run.subject[1]).with_for_update())).scalar_one_or_none()


# ================================================================ publish / update
async def push(ctx: RequestContext, db: AsyncSession, run: SyncRun) -> SyncResult:
    bp = await _subject_posting(db, run)
    if bp is None or bp.status in CLOSED_STATUSES:
        return SyncResult(skipped=1)
    src = await mapping.load_source(db, bp.posting_id)
    if src is None or not mapping.is_live(src):
        bp.last_error_code, bp.last_error_message = "posting_not_live", "The careers posting is no longer live"
        set_status(db, bp, "failed")
        return SyncResult(failed=1, errors=(SyncError("board_posting", bp.id, bp.board_ref, "posting_not_live", "posting not live"),))
    revision = bp.local_revision
    dto = mapping.to_dto(src, bp.board_key, bp.id, revision, dict(bp.board_fields or {}))
    first = bp.board_ref is None
    try:
        ref = await (run.adapter.publish(dto) if first else run.adapter.update(bp.board_ref, dto))
    except ManualActionRequired as manual:
        bp.last_error_code, bp.last_error_message = "manual_action_required", safe_message(manual.instructions)
        return SyncResult(skipped=1)
    except ConnectorPermanentError as exc:
        bp.last_error_code, bp.last_error_message = exc.code[:60], safe_message(str(exc)) or "The board refused the posting"
        set_status(db, bp, "failed")
        _notify_failed(bp)
        return SyncResult(failed=1, errors=(SyncError("board_posting", bp.id, bp.board_ref, exc.code, bp.last_error_message),))
    bp.board_ref = ref.board_ref
    await integrations_service.link(db, bp.installation_id, "board_posting", bp.id, ref.board_ref, external_url=ref.url, local_version=str(revision))
    bp.revision = revision
    bp.content_sha256 = mapping.content_sha256(dto)
    bp.last_error_code = bp.last_error_message = None
    was_live = bp.status in ("live", "expiring")
    _apply_ref(db, bp, ref)
    await _snapshot(db, bp, ref.credits_left)
    if first:
        bp.published_at = now()
        await metering_service.record(ctx, "job_board_posting", installation_id=bp.installation_id)
    if bp.status == "live" and not was_live:
        emit(db, BoardPostingPublished(tenant_id=bp.tenant_id, board_posting_id=bp.id, demand_id=bp.demand_id, posting_id=bp.posting_id, board_key=bp.board_key, status=bp.status))
    audit.record("job_board.pushed", "board_posting", bp.id, session=db, board_key=bp.board_key, revision=revision, status=bp.status)
    await db.flush()
    return SyncResult(processed=1, created=1 if first else 0, updated=0 if first else 1)


# ================================================================ close
async def _manual_close_task(db: AsyncSession, bp: BoardPosting, title: str) -> None:
    from app.schemas.tasks import TaskCreate
    from app.services import tasks_service

    demand = await db.get(Demand, bp.demand_id)
    owner = bp.published_by or (demand.owner_id if demand else None)
    if owner is None:
        return
    data = TaskCreate.model_construct(
        title=f"Close {title} on {board_label(bp.board_key)} manually"[:200],
        notes="This job is no longer open in Talent OS. Close it on the board by hand.",
        owner_id=owner, entity_type="board_posting", entity_id=bp.id, due_at=None, due_all_day=False, priority="normal", reminder_at=None,
    )  # fmt: skip
    try:
        async with db.begin_nested():
            await tasks_service.create_system_task(RequestContext(tenant_id=bp.tenant_id), db, data, source="agent")
    except Exception:  # pragma: no cover - logged; closing still happens
        logger.exception("Manual close Task for board posting {} not created", bp.id)


async def _close_one(db: AsyncSession, adapter: Any, bp: BoardPosting, reason: str, title: str) -> str:
    if bp.status in CLOSED_STATUSES:
        return "skipped"
    if bp.manual or not bp.board_ref or bp.status in ("draft", "failed", "rejected", "publishing"):
        if bp.manual and bp.board_ref:
            await _manual_close_task(db, bp, title)
        set_status(db, bp, "closed", reason=reason)
        return "closed"
    try:
        ref = await adapter.close(bp.board_ref)
    except ManualActionRequired:
        await _manual_close_task(db, bp, title)
        set_status(db, bp, "closed", reason=reason)
        return "closed"
    await _snapshot(db, bp, ref.credits_left)
    set_status(db, bp, "closed", reason=reason)
    return "closed"


async def close(ctx: RequestContext, db: AsyncSession, run: SyncRun) -> SyncResult:  # noqa: ARG001
    closed = 0
    if run.subject is not None and run.subject[0] == "board_posting":
        bp = await _subject_posting(db, run)
        if bp is None:
            return SyncResult(skipped=1)
        demand = await db.get(Demand, bp.demand_id)
        outcome = await _close_one(db, run.adapter, bp, bp.close_reason or "user", demand.title if demand else "the job")
        await db.flush()
        return SyncResult(processed=1, updated=1 if outcome == "closed" else 0)
    if run.subject is None or run.subject[0] != "demand":
        return SyncResult(skipped=1)
    settings = await board_settings(db)
    if not settings.auto_close_on_demand_change:
        return SyncResult(skipped=1)
    demand = await db.get(Demand, run.subject[1])
    if demand is None:
        return SyncResult(skipped=1)
    posting = (await db.execute(select(DemandPosting).where(DemandPosting.demand_id == demand.id))).scalar_one_or_none()
    if demand.status in DEMAND_CLOSE_REASONS:
        reason = DEMAND_CLOSE_REASONS[demand.status]
    elif posting is None or not posting.is_live:
        reason = "posting_unpublished"
    else:
        return SyncResult(skipped=1)  # still open and live (e.g. re-opened)
    for bp in await open_for_demand(db, demand.id, run.installation_id):
        if await _close_one(db, run.adapter, bp, reason, demand.title) == "closed":
            closed += 1
    await db.flush()
    return SyncResult(processed=closed, updated=closed)


# ================================================================ mark out of date
async def mark_out_of_date(ctx: RequestContext, db: AsyncSession, run: SyncRun) -> SyncResult:  # noqa: ARG001
    if run.subject is None or run.subject[0] != "demand":
        return SyncResult(skipped=1)
    return SyncResult(updated=await refresh_revisions(db, run.subject[1], run.installation_id))


async def refresh_revisions(db: AsyncSession, demand_id: uuid.UUID, installation_id: uuid.UUID | None = None) -> int:
    """Recompute the content hash of every open Board Posting of the demand; changed -> ``local_revision += 1``."""
    changed = 0
    for bp in await open_for_demand(db, demand_id, installation_id):
        if bp.status not in ("live", "expiring"):
            continue
        src = await mapping.load_source(db, bp.posting_id)
        if src is None:
            continue
        sha = mapping.content_sha256(mapping.to_dto(src, bp.board_key, bp.id, bp.local_revision, dict(bp.board_fields or {})))
        if sha != bp.content_sha256:
            bp.content_sha256 = sha
            bp.jd_version_id = src.jd.id
            if bp.revision >= bp.local_revision:
                bp.local_revision += 1
            changed += 1
    await db.flush()
    return changed


# ================================================================ status
async def status(ctx: RequestContext, db: AsyncSession, run: SyncRun) -> SyncResult:  # noqa: ARG001
    rows = list(
        (
            await db.execute(
                select(BoardPosting)
                .where(BoardPosting.installation_id == run.installation_id, BoardPosting.status.in_(("publishing", "live", "expiring")), BoardPosting.board_ref.is_not(None), BoardPosting.manual.is_(False))
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    processed = failed = 0
    errors: list[SyncError] = []
    for bp in rows:
        try:
            ref = await run.adapter.status(bp.board_ref)
        except (ConnectorPermanentError, ManualActionRequired) as exc:
            failed += 1
            errors.append(SyncError("board_posting", bp.id, bp.board_ref, getattr(exc, "code", "manual"), safe_message(str(exc))))
            continue
        _apply_ref(db, bp, ref)
        if bp.status == "expired" and not bp.close_reason:
            bp.close_reason = "expired"
        await _snapshot(db, bp, ref.credits_left)
        processed += 1
    await db.flush()
    return SyncResult(processed=processed, updated=processed, failed=failed, errors=tuple(errors))


# ================================================================ applicants (poll)
async def applicants(ctx: RequestContext, db: AsyncSession, run: SyncRun) -> SyncResult:
    from app.models.oauth import AppInstallation
    from app.services.job_distribution import ingest

    inst = await db.get(AppInstallation, run.installation_id)
    board_key = str(inst.connector_key) if inst else "fake_job_board"
    since_raw = run.cursor.get("since")
    since = datetime.fromisoformat(since_raw) if isinstance(since_raw, str) and since_raw else None
    cursor: str | None = run.cursor.get("page") if isinstance(run.cursor.get("page"), str) else None
    created = linked = failed = processed = 0
    latest = since
    for _ in range(20):  # page budget per run
        items, cursor = await run.adapter.fetch_applicants(since, cursor)
        for item in items:
            outcome = await ingest.ingest_one(ctx, db, run.installation_id, board_key, item)
            processed += 1
            created += outcome == "created"
            linked += outcome == "linked"
            failed += outcome == "failed"
            if latest is None or item.applied_at > latest:
                latest = item.applied_at
        if not cursor:
            break
    next_cursor = {"since": latest.isoformat() if latest else None, "page": cursor}
    return SyncResult(processed=processed, created=created, updated=linked, failed=failed, next_cursor=next_cursor)


# ================================================================ failures (job listener)
def _notify_failed(bp: BoardPosting) -> None:
    from app.services.job_distribution.notify import fan_out

    if bp.published_by is not None:
        fan_out(bp.tenant_id, "board_posting_failed", {
            "event_id": f"board_posting_failed:{bp.id}:{bp.local_revision}", "board_posting_id": str(bp.id),
            "user_id": str(bp.published_by), "board": board_label(bp.board_key), "message": bp.last_error_message or "",
            "demand_id": str(bp.demand_id),
        })  # fmt: skip


async def on_job_outcome(ctx: RequestContext, db: AsyncSession, job: Any, outcome: str, exc: BaseException | None) -> None:  # noqa: ARG001
    if job.op not in PUSH_OPS or outcome not in ("dead", "permanent", "auth_failed"):
        return
    payload = dict(job.payload or {})
    if payload.get("subject_type") != "board_posting" or not payload.get("subject_id"):
        return
    try:
        if tenant_of(db) != job.tenant_id:
            return
    except Exception:  # pragma: no cover
        return
    bp = await db.get(BoardPosting, uuid.UUID(str(payload["subject_id"])))
    if bp is None or bp.status in CLOSED_STATUSES:
        return
    code = getattr(exc, "code", None) or ("auth_failed" if outcome == "auth_failed" else "retries_exhausted")
    bp.last_error_code = str(code)[:60]
    bp.last_error_message = safe_message(str(exc) if exc else "") or "The board did not accept the posting"
    if job.op != "sync:close":
        set_status(db, bp, "failed")
    await db.flush()
    _notify_failed(bp)


def register() -> None:
    for key, fn in (("publish", push), ("update", push), ("close", close), ("mark_out_of_date", mark_out_of_date), ("status", status), ("applicants", applicants)):
        integrations_service.register_sync_handler("job_board", key, fn)


__all__ = ["ConflictError", "applicants", "close", "mark_out_of_date", "on_job_outcome", "push", "refresh_revisions", "register", "status"]
