# ruff: file-ignore[line-too-long]
"""Board Postings: preview, multi-post, update, refresh, close, retry, abandon and the manual path (Epic 70, stories
70.2 / 70.5; spec A3, A6).

* Only the approved JD version of a live careers posting is published; the posting's ``show_*`` flags apply to every
  board; biased-term checks run again before each publish (409 ``bias_flags_unacknowledged``).
* Publishing, updating, refreshing and closing are user actions (``posting:publish_external``; ★ ``require_human``
  except the approver of an MCP Proposal). Each board gets its own Board Posting and an idempotent connector job
  (key ``job_board:publish:board_posting:<id>:<revision>``) on queue ``ecosystem``.
* Boards whose required fields are not filled are returned as ``blocked`` and left untouched.
"""

import uuid
from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.integrations import find_manifest
from app.core.sequences import next_human_id
from app.events.job_distribution import BoardPostingClosed, BoardPostingStatusChanged
from app.models.demands import Demand, DemandPosting
from app.models.job_distribution import BoardCreditSnapshot, BoardPosting, BoardSet
from app.schemas.job_distribution import (
    BoardFieldOut,
    BoardPostingOut,
    BoardPreviewOut,
    FieldOptionOut,
    MultiPostIn,
    MultiPostPreviewOut,
    PackFieldOut,
    PackOut,
    PublishIn,
    PublishOut,
    RecordIn,
)
from app.services import integrations_service
from app.services.job_distribution import mapping
from app.services.job_distribution.common import (
    CLOSED_STATUSES,
    OPEN_STATUSES,
    PUBLISH_PERMISSION,
    board_installation,
    board_label,
    get_board_posting,
    installed_boards,
    now,
    user_id_of,
)

__all__ = [
    "abandon",
    "board_posting_out",
    "close",
    "enqueue",
    "get",
    "list_for_demand",
    "mark_closed",
    "pack",
    "preview",
    "publish",
    "record",
    "refresh",
    "retry",
    "set_status",
    "update",
]


# ================================================================ helpers
def board_posting_out(bp: BoardPosting) -> BoardPostingOut:
    out = BoardPostingOut.model_validate(bp)
    return out.model_copy(
        update={
            "board_label": board_label(bp.board_key),
            "out_of_date": bp.status in ("live", "expiring") and bp.revision < bp.local_revision,
            "blocked_fields": list(bp.blocked_fields or []),
        }
    )


def set_status(db: AsyncSession, bp: BoardPosting, status: str, *, reason: str | None = None) -> None:
    """Change ``status`` and emit ``BoardPostingStatusChanged`` (+ ``BoardPostingClosed`` when it closes)."""
    old = bp.status
    if old == status:
        return
    bp.status = status
    bp.last_status_at = now()
    if status in ("closed", "expired", "abandoned"):
        bp.closed_at = bp.closed_at or now()
        if reason:
            bp.close_reason = reason
    emit(db, BoardPostingStatusChanged(tenant_id=bp.tenant_id, board_posting_id=bp.id, demand_id=bp.demand_id, posting_id=bp.posting_id, board_key=bp.board_key, from_status=old, to_status=status))
    if status == "closed":
        emit(db, BoardPostingClosed(tenant_id=bp.tenant_id, board_posting_id=bp.id, demand_id=bp.demand_id, posting_id=bp.posting_id, board_key=bp.board_key, status=status))


async def _demand_for(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, permission: str = PUBLISH_PERMISSION) -> Demand:
    from app.services.demands.common import ensure_can, get_visible

    demand = await get_visible(ctx, db, demand_id)
    if permission != "demand:read":
        await ensure_can(ctx, db, demand, permission)
    return demand


async def _bp_for(ctx: RequestContext, db: AsyncSession, board_posting_id: uuid.UUID, permission: str = PUBLISH_PERMISSION) -> BoardPosting:
    bp = await get_board_posting(db, board_posting_id, for_update=permission != "demand:read")
    try:
        await _demand_for(ctx, db, bp.demand_id, permission)
    except NotFound:
        raise NotFound("Board posting not found", code="board_posting_not_found") from None
    return bp


def _human(ctx: RequestContext, approved_proposal: bool) -> None:
    if not approved_proposal:
        require_human(actor_from_ctx(ctx))


async def _live_source(db: AsyncSession, demand: Demand) -> mapping.Source:
    posting = (await db.execute(select(DemandPosting).where(DemandPosting.demand_id == demand.id))).scalar_one_or_none()
    src = await mapping.load_source(db, posting.id) if posting is not None else None
    if src is None or not mapping.is_live(src):
        raise ConflictError("Publish the job on your careers site first", code="posting_not_live")
    return src


async def _board_keys(db: AsyncSession, demand: Demand, data: MultiPostIn) -> tuple[list[str], bool]:
    """(keys, explicit). Explicit keys, else the board set, else the default set for the employment type, else every
    installed board."""
    if data.board_keys:
        return list(dict.fromkeys(data.board_keys)), True
    if data.board_set_id is not None:
        bs = await db.get(BoardSet, data.board_set_id)
        if bs is None:
            raise NotFound("Board set not found", code="board_set_not_found")
        return list(bs.board_keys), True
    sets = list((await db.execute(select(BoardSet).where(BoardSet.is_default.is_(True)))).scalars())
    match = next((s for s in sets if s.employment_type == demand.employment_type), None) or next((s for s in sets if s.employment_type is None), None)
    if match is not None:
        return list(match.board_keys), False
    return [str(i.connector_key) for i, _ in await installed_boards(db)], False


async def credits_left(db: AsyncSession, installation_id: uuid.UUID) -> int | None:
    return (
        await db.execute(
            select(BoardCreditSnapshot.credits_left)
            .where(BoardCreditSnapshot.installation_id == installation_id)
            .order_by(BoardCreditSnapshot.observed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def open_posting(db: AsyncSession, posting_id: uuid.UUID, board_key: str, *, lock: bool = False) -> BoardPosting | None:
    stmt = select(BoardPosting).where(
        BoardPosting.posting_id == posting_id, BoardPosting.board_key == board_key, BoardPosting.status.not_in(CLOSED_STATUSES)
    )
    if lock:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


def _salary_text(dto: Any) -> str | None:
    s = dto.salary
    if s is None:
        return None
    lo = f"{s.min_amount:,.0f}" if s.min_amount is not None else ""
    hi = f"{s.max_amount:,.0f}" if s.max_amount is not None else ""
    return f"{s.currency} {lo}{' - ' if lo and hi else ''}{hi} per {s.period}".strip()


def _experience_text(dto: Any) -> str | None:
    lo, hi = dto.experience_min_years, dto.experience_max_years
    if lo is None and hi is None:
        return None
    return f"{int(lo or 0)}-{int(hi)} years" if hi is not None else f"{int(lo or 0)}+ years"


def _excerpt(html: str, limit: int = 400) -> str:
    from app.services.portal.common import plain_text

    text = plain_text(html)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def _plan(ctx: RequestContext, db: AsyncSession, demand: Demand, data: MultiPostIn) -> tuple[mapping.Source, list[dict[str, Any]], list[dict[str, Any]]]:
    """Per-board plan: ``{key, inst, cfg, existing, bp_id, fields, dto, blocked, schema, preview}``."""
    from app.services.demands.jd import bias_matches

    src = await _live_source(db, demand)
    bias = [m.model_dump() for m in await bias_matches(db, src.jd.content)]
    keys, explicit = await _board_keys(db, demand, data)
    plans: list[dict[str, Any]] = []
    for key in keys:
        pair = await board_installation(db, key)
        if pair is None:
            if explicit:
                raise ConflictError(f"{board_label(key)} is not installed", code="board_not_installed", details={"board_key": key})
            continue
        inst, cfg = pair
        adapter = await integrations_service.adapter_for(ctx, db, inst.id, config_id=cfg.id)
        schema = await mapping.field_schema(adapter, inst.id)
        existing = await open_posting(db, src.posting.id, key)
        fields = {**await mapping.mapped_fields(db, key, src), **(existing.board_fields if existing else {}), **{k: v for k, v in (data.board_fields.get(key) or {}).items() if v not in (None, "")}}
        bp_id = existing.id if existing is not None else uuid.uuid4()
        dto = mapping.to_dto(src, key, bp_id, (existing.local_revision if existing else 1), fields)
        blocked = mapping.blocked(schema, fields)
        labels = {f.key: f.label for f in schema.fields}
        preview = BoardPreviewOut(
            board_key=key, label=board_label(key), installation_id=inst.id, title=dto.title, company=dto.company_name,
            location=", ".join(p for p in (dto.location.city, dto.location.region, dto.location.country) if p) if dto.location else None,
            experience=_experience_text(dto), salary=_salary_text(dto), apply_url=dto.apply_url,
            description_excerpt=_excerpt(dto.description_html),
            fields=[BoardFieldOut(key=f.key, label=f.label, required=f.required, kind=f.kind, options=[FieldOptionOut(value=v, label=lbl) for v, lbl in f.options]) for f in schema.fields],
            values={k: v for k, v in fields.items() if k in labels}, blocked_fields=blocked, blocked_labels=[labels.get(b, b) for b in blocked],
            credits_left=await credits_left(db, inst.id), will_publish=not blocked,
        )  # fmt: skip
        plans.append({"key": key, "inst": inst, "cfg": cfg, "existing": existing, "bp_id": bp_id, "fields": fields, "dto": dto, "blocked": blocked, "preview": preview})
    return src, bias, plans


# ================================================================ preview / publish
async def preview(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: MultiPostIn) -> MultiPostPreviewOut:
    demand = await _demand_for(ctx, db, demand_id)
    src, bias, plans = await _plan(ctx, db, demand, data)
    return MultiPostPreviewOut(demand_id=demand.id, posting_id=src.posting.id, bias_matches=bias, boards=[p["preview"] for p in plans])


async def enqueue(ctx: RequestContext, db: AsyncSession, bp: BoardPosting, job_key: str, *, suffix: str = "") -> None:
    """Queue the board push of ``bp`` (idempotent per Board Posting + revision)."""
    from app.models.oauth import AppInstallation
    from app.services.integrations.runner import enqueue_sync

    inst = await db.get(AppInstallation, bp.installation_id)
    manifest = find_manifest(bp.board_key)
    spec = manifest.sync_job(job_key) if manifest is not None else None
    if inst is None or manifest is None or spec is None:
        raise ConflictError("The board is not installed", code="board_not_installed")
    key = f"job_board:{job_key}:board_posting:{bp.id}:{bp.local_revision}{suffix}"
    await enqueue_sync(ctx, db, inst, manifest, spec, config_id=bp.config_id, trigger="manual", subject=("board_posting", bp.id), seq=bp.local_revision, key=key)


async def publish(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, data: PublishIn, *, approved_proposal: bool = False) -> PublishOut:
    _human(ctx, approved_proposal)
    demand = await _demand_for(ctx, db, demand_id)
    src, bias, plans = await _plan(ctx, db, demand, data)
    if bias and not data.acknowledge_bias_flags:
        raise ConflictError("The JD contains flagged wording; review or acknowledge it", code="bias_flags_unacknowledged", details={"matches": bias})
    published: list[BoardPosting] = []
    blocked: list[BoardPreviewOut] = []
    user_id = user_id_of(ctx)
    for plan in plans:
        if plan["blocked"]:
            blocked.append(plan["preview"])
            continue
        sha = mapping.content_sha256(plan["dto"])
        bp = await open_posting(db, src.posting.id, plan["key"], lock=True)
        if bp is None:
            bp = BoardPosting(
                id=plan["bp_id"], tenant_id=demand.tenant_id, human_id=await next_human_id(db, "board_posting", "BP", width=6),
                demand_id=demand.id, posting_id=src.posting.id, installation_id=plan["inst"].id, config_id=plan["cfg"].id,
                board_key=plan["key"], status="draft", revision=0, local_revision=1, content_sha256=sha, jd_version_id=src.jd.id,
                board_fields=dict(plan["fields"]), blocked_fields=[], published_by=user_id,
            )  # fmt: skip
            db.add(bp)
            await db.flush()
        elif bp.status in ("live", "expiring", "publishing") and bp.revision >= bp.local_revision and bp.content_sha256 == sha and bp.board_fields == plan["fields"]:
            published.append(bp)  # idempotent replay: nothing to send
            continue
        else:
            if bp.content_sha256 != sha or bp.board_fields != plan["fields"]:
                bp.local_revision += 1 if bp.revision >= bp.local_revision else 0
            bp.content_sha256, bp.jd_version_id, bp.board_fields, bp.blocked_fields = sha, src.jd.id, dict(plan["fields"]), []
            bp.published_by = user_id or bp.published_by
        job = "update" if bp.board_ref else "publish"
        if bp.status not in ("live", "expiring"):
            set_status(db, bp, "publishing")
        bp.last_error_code = bp.last_error_message = None
        await db.flush()
        await enqueue(ctx, db, bp, job)
        audit.record("job_board.publish_requested", "board_posting", bp.id, session=db, board_key=bp.board_key, revision=bp.local_revision)
        published.append(bp)
    return PublishOut(published=[board_posting_out(b) for b in published], blocked=blocked)


# ================================================================ reads
async def list_for_demand(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> list[BoardPostingOut]:
    await _demand_for(ctx, db, demand_id, "demand:read")
    rows = (await db.execute(select(BoardPosting).where(BoardPosting.demand_id == demand_id).order_by(BoardPosting.created_at))).scalars()
    return [board_posting_out(r) for r in rows]


async def get(ctx: RequestContext, db: AsyncSession, board_posting_id: uuid.UUID) -> BoardPostingOut:
    return board_posting_out(await _bp_for(ctx, db, board_posting_id, "demand:read"))


# ================================================================ update / refresh / close / retry / abandon
async def update(ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID]) -> list[BoardPostingOut]:
    """"Update boards": push the current content of out-of-date live postings (nothing changes on a board before)."""
    require_human(actor_from_ctx(ctx))
    out = []
    for bp_id in ids:
        bp = await _bp_for(ctx, db, bp_id)
        if bp.status in ("live", "expiring") and bp.board_ref and bp.revision < bp.local_revision and not bp.manual:
            await enqueue(ctx, db, bp, "update")
            audit.record("job_board.update_requested", "board_posting", bp.id, session=db, revision=bp.local_revision)
        out.append(board_posting_out(bp))
    return out


async def refresh(ctx: RequestContext, db: AsyncSession, board_posting_id: uuid.UUID) -> BoardPostingOut:
    """Re-publish as a new revision (Naukri refresh / Indeed re-ingest); consumes credits -> user action only."""
    require_human(actor_from_ctx(ctx))
    bp = await _bp_for(ctx, db, board_posting_id)
    if bp.status not in ("live", "expiring") or not bp.board_ref or bp.manual:
        raise ConflictError("Only a live board posting can be refreshed", code="board_posting_not_live")
    bp.local_revision += 1 if bp.revision >= bp.local_revision else 0
    await db.flush()
    await enqueue(ctx, db, bp, "update", suffix=":refresh")
    audit.record("job_board.refresh_requested", "board_posting", bp.id, session=db)
    return board_posting_out(bp)


async def close(ctx: RequestContext, db: AsyncSession, board_posting_id: uuid.UUID) -> BoardPostingOut:
    require_human(actor_from_ctx(ctx))
    bp = await _bp_for(ctx, db, board_posting_id)
    if bp.status in CLOSED_STATUSES:
        return board_posting_out(bp)
    if not bp.board_ref or bp.manual or bp.status in ("draft", "failed", "rejected"):
        set_status(db, bp, "closed", reason="user")
    else:
        bp.close_reason = "user"
        await db.flush()
        await enqueue(ctx, db, bp, "close", suffix=":close")
    audit.record("job_board.close_requested", "board_posting", bp.id, session=db)
    await db.flush()
    return board_posting_out(bp)


async def retry(ctx: RequestContext, db: AsyncSession, board_posting_id: uuid.UUID) -> BoardPostingOut:
    require_human(actor_from_ctx(ctx))
    bp = await _bp_for(ctx, db, board_posting_id)
    if bp.status not in ("failed", "rejected"):
        raise ConflictError("Only a failed board posting can be retried", code="board_posting_not_failed")
    set_status(db, bp, "publishing")
    bp.last_error_code = bp.last_error_message = None
    await db.flush()
    await enqueue(ctx, db, bp, "update" if bp.board_ref else "publish", suffix=f":retry:{int(now().timestamp() * 1000)}")
    return board_posting_out(bp)


async def abandon(ctx: RequestContext, db: AsyncSession, board_posting_id: uuid.UUID) -> BoardPostingOut:
    require_human(actor_from_ctx(ctx))
    bp = await _bp_for(ctx, db, board_posting_id)
    if bp.status in CLOSED_STATUSES:
        return board_posting_out(bp)
    set_status(db, bp, "abandoned", reason="user")
    await db.flush()
    return board_posting_out(bp)


# ================================================================ manual path
def _pack_allowed(bp: BoardPosting) -> bool:
    return bp.board_key == "manual" or bp.manual or bp.status in ("failed", "abandoned", "rejected", "draft", "publishing") or bp.last_error_code == "manual_action_required"


async def pack(ctx: RequestContext, db: AsyncSession, board_posting_id: uuid.UUID) -> PackOut:
    from app.services.connectors.job_board.manual import copy_paste_pack

    bp = await _bp_for(ctx, db, board_posting_id)
    if not _pack_allowed(bp):
        raise ConflictError("The posting pack is offered for manual and failed postings", code="pack_not_available")
    src = await mapping.load_source(db, bp.posting_id)
    if src is None:
        raise NotFound("Posting not found", code="posting_not_found")
    dto = mapping.to_dto(src, bp.board_key, bp.id, bp.local_revision, dict(bp.board_fields or {}))
    data = copy_paste_pack(dto)
    labels = {"location": "Location", "work_mode": "Work mode", "employment_type": "Employment type", "experience": "Experience", "skills": "Key skills", "openings": "Openings", "salary": "Salary", "company": "Company"}
    fields = [PackFieldOut(label=lbl, value=str(data[k])) for k, lbl in labels.items() if data.get(k) not in (None, "")]
    fields += [PackFieldOut(label=k.replace("_", " ").capitalize(), value=str(v)) for k, v in (bp.board_fields or {}).items() if v not in (None, "")]
    return PackOut(board_key=bp.board_key, title=dto.title, description_text=str(data["description"]), description_html=dto.description_html, fields=fields, apply_url=dto.apply_url)


async def record(ctx: RequestContext, db: AsyncSession, board_posting_id: uuid.UUID, data: RecordIn) -> BoardPostingOut:
    """"Record manually": the recruiter posted it by hand -> ``manual``, ``live``, link written (reporting keeps working)."""
    require_human(actor_from_ctx(ctx))
    bp = await _bp_for(ctx, db, board_posting_id)
    if bp.status in CLOSED_STATUSES:
        raise ConflictError("This board posting is closed", code="board_posting_closed")
    try:
        await integrations_service.link(db, bp.installation_id, "board_posting", bp.id, data.board_ref, external_url=data.board_url, local_version=str(bp.local_revision))
    except ConflictError:
        raise ValidationFailed("This board reference is already recorded for another posting", code="board_ref_in_use", details={"fields": {"board_ref": "Already used"}}) from None
    bp.manual = True
    bp.board_ref, bp.board_url = data.board_ref, data.board_url
    bp.published_at = datetime.combine(data.posted_on, time(9, 0), tzinfo=UTC)
    bp.expires_at = datetime.combine(data.expires_on, time(23, 59), tzinfo=UTC) if data.expires_on else None
    bp.revision = bp.local_revision
    bp.cost_note = data.cost_note or bp.cost_note
    bp.last_error_code = bp.last_error_message = None
    bp.published_by = bp.published_by or user_id_of(ctx)
    set_status(db, bp, "live")
    await db.flush()
    audit.record("job_board.recorded_manually", "board_posting", bp.id, session=db, board_key=bp.board_key)
    return board_posting_out(bp)


async def mark_closed(ctx: RequestContext, db: AsyncSession, board_posting_id: uuid.UUID) -> BoardPostingOut:
    require_human(actor_from_ctx(ctx))
    bp = await _bp_for(ctx, db, board_posting_id)
    if bp.status not in CLOSED_STATUSES:
        set_status(db, bp, "closed", reason=bp.close_reason or "user")
        await db.flush()
        audit.record("job_board.marked_closed", "board_posting", bp.id, session=db)
    return board_posting_out(bp)


async def open_for_demand(db: AsyncSession, demand_id: uuid.UUID, installation_id: uuid.UUID | None = None) -> list[BoardPosting]:
    stmt = select(BoardPosting).where(BoardPosting.demand_id == demand_id, BoardPosting.status.in_(OPEN_STATUSES))
    if installation_id is not None:
        stmt = stmt.where(BoardPosting.installation_id == installation_id)
    return list((await db.execute(stmt.with_for_update())).scalars())
