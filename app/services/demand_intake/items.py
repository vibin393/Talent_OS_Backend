# ruff: file-ignore[line-too-long, non-imperative-mood, unused-function-argument]
"""The intake inbox (Epic 55, story 55.1 / 55.2): list, read, assign a Client, retry, discard, the agent reads
(``get_for_agent`` / ``match_of``), the Accountable Owner, the run outcome write-back and the 180-day purge.

Permissions (router): ``demand:create`` (assign also ``client:read`` on the client). Mutations are human-only
(``require_human``: API keys may read the inbox, FR-329).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.pagination import Page, PageParams, paginate
from app.models.demand_intake import NEEDS_ATTENTION, DemandIntakeMessage
from app.schemas.demand_intake import (
    AgentAttachmentText,
    AssignClientIn,
    AttachmentOut,
    DiscardIn,
    DraftRef,
    IntakeForAgentOut,
    IntakeMessageOut,
    IntakeMessageSummaryOut,
    IntakeQuery,
    MatchOut,
)
from app.services.demand_intake import matching, receive

AGENT_TEXT_MAX = 30_000


def _now() -> datetime:
    return datetime.now(UTC)


def _not_found() -> NotFound:
    return NotFound("Intake message not found")


async def _get(db: AsyncSession, message_id: uuid.UUID, *, lock: bool = False) -> DemandIntakeMessage:
    stmt = select(DemandIntakeMessage).where(DemandIntakeMessage.id == message_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise _not_found()
    return row


async def _client_names(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    from app.services import clients_service

    return {cid: await clients_service.client_name(db, cid) for cid in ids}


def _summary(row: DemandIntakeMessage, names: dict[uuid.UUID, str | None]) -> IntakeMessageSummaryOut:
    return IntakeMessageSummaryOut(
        id=row.id, source=row.source, from_address=row.from_address, from_name=row.from_name, subject=row.subject,
        received_at=row.received_at, status=row.status, status_reason=row.status_reason,
        client_id=row.matched_client_id, client_name=names.get(row.matched_client_id) if row.matched_client_id else None,
        draft_demand_ids=list(row.draft_demand_ids or []), group_label=row.group_label,
    )  # fmt: skip


async def _match_out(db: AsyncSession, row: DemandIntakeMessage) -> MatchOut:
    from app.models.clients import Contact
    from app.services import clients_service

    contact_name = None
    if row.matched_contact_id is not None:
        contact = await db.get(Contact, row.matched_contact_id)
        contact_name = contact.name if contact is not None else None
    original = (
        matching.forwarded_original_from((await _body(db, row))[:5000])
        if row.match_method == "forwarded_contact"
        else None
    )
    return MatchOut(
        status=row.status, method=row.match_method, client_id=row.matched_client_id,
        client_name=await clients_service.client_name(db, row.matched_client_id) if row.matched_client_id else None,
        contact_id=row.matched_contact_id, contact_name=contact_name, owner_user_id=row.owner_user_id,
        explanation=matching.explanation(row.match_method, row.from_address, original),
    )  # fmt: skip


async def _body(db: AsyncSession, row: DemandIntakeMessage) -> str:
    from app.services import files_service

    if row.body_text_file_id is None:
        return ""
    f = await files_service.get_file(db, row.body_text_file_id)
    if f is None or f.deleted_at is not None:
        return ""
    return (await files_service.read_file(f)).decode("utf-8", errors="replace")


async def _drafts(db: AsyncSession, ids: list[uuid.UUID]) -> list[DraftRef]:
    from app.models.demands import Demand

    if not ids:
        return []
    rows = {d.id: d for d in (await db.execute(select(Demand).where(Demand.id.in_(ids)))).scalars()}
    return [
        DraftRef(
            id=i,
            human_id=rows[i].req_id if i in rows else None,
            title=rows[i].title if i in rows else None,
            status=rows[i].status if i in rows else None,
        )
        for i in ids
    ]


async def message_out(db: AsyncSession, row: DemandIntakeMessage) -> IntakeMessageOut:
    names = await _client_names(db, {row.matched_client_id} if row.matched_client_id else set())
    base = _summary(row, names).model_dump()
    return IntakeMessageOut(
        **base, body_preview=row.body_preview,
        attachments=[AttachmentOut(**{k: a.get(k) for k in AttachmentOut.model_fields}) for a in row.attachments or []],
        match=await _match_out(db, row), drafts=await _drafts(db, list(row.draft_demand_ids or [])),
        owner_user_id=row.owner_user_id, run_id=row.run_id, task_id=row.task_id, processed_at=row.processed_at,
        auth_result=row.auth_result,
    )  # fmt: skip


def _query(q: IntakeQuery) -> Select[tuple[DemandIntakeMessage]]:
    stmt = select(DemandIntakeMessage)
    if q.tab == "needs_attention":
        stmt = stmt.where(DemandIntakeMessage.status.in_(NEEDS_ATTENTION))
    elif q.tab == "drafted":
        stmt = stmt.where(DemandIntakeMessage.status == "drafted")
    if q.status:
        stmt = stmt.where(DemandIntakeMessage.status.in_(q.status))
    if q.client_id is not None:
        stmt = stmt.where(DemandIntakeMessage.matched_client_id == q.client_id)
    return stmt.order_by(DemandIntakeMessage.received_at.desc(), DemandIntakeMessage.id)


async def list_messages(
    ctx: RequestContext, db: AsyncSession, q: IntakeQuery, params: PageParams | None = None
) -> Page[IntakeMessageSummaryOut]:
    page = await paginate(db, _query(q), params or PageParams())
    rows: list[DemandIntakeMessage] = list(page.items)
    names = await _client_names(db, {r.matched_client_id for r in rows if r.matched_client_id})
    return Page(
        items=[_summary(r, names) for r in rows], total=page.total, page=page.page, page_size=page.page_size
    )


async def get_message(ctx: RequestContext, db: AsyncSession, message_id: uuid.UUID) -> IntakeMessageOut:
    return await message_out(db, await _get(db, message_id))


# ================================================================ person actions
async def assign_client(
    ctx: RequestContext, db: AsyncSession, message_id: uuid.UUID, data: AssignClientIn
) -> IntakeMessageOut:
    """``unmatched`` / ``ambiguous`` -> ``matched`` (``match_method=manual``); the run starts after commit."""
    from app.services import clients_service
    from app.services.demand_intake import settings as settings_mod

    require_human(actor_from_ctx(ctx))
    row = await _get(db, message_id, lock=True)
    if row.status not in ("unmatched", "ambiguous"):
        raise ConflictError(
            "Only unmatched or ambiguous messages can be assigned", code="intake_not_assignable"
        )
    await clients_service.get_client(ctx, db, data.client_id)  # 404 unless readable (client:read)
    if data.contact_id is not None and not await clients_service.contact_belongs_to_client(
        db, data.contact_id, data.client_id
    ):
        raise ValidationFailed("The contact does not belong to the client", code="invalid_contact")
    settings = await settings_mod.row(db)
    owner = (
        await matching.client_owner(db, data.client_id)
        or settings.fallback_owner_id
        or getattr(ctx.principal, "id", None)
    )
    row.status = "matched"
    row.status_reason = None
    row.match_method = "manual"
    row.matched_client_id = data.client_id
    row.matched_contact_id = data.contact_id
    row.owner_user_id = owner
    await db.flush()
    emit(db, receive.matched_event(row))
    return await message_out(db, row)


async def retry(ctx: RequestContext, db: AsyncSession, message_id: uuid.UUID) -> IntakeMessageOut:
    """``failed`` / ``no_roles`` (with a known client) -> ``matched`` again; the new attempt gets a new dedup key."""
    require_human(actor_from_ctx(ctx))
    row = await _get(db, message_id, lock=True)
    if row.status not in ("failed", "no_roles") or row.matched_client_id is None:
        raise ConflictError("This message can't be retried", code="intake_not_retryable")
    row.status = "matched"
    row.status_reason = None
    row.retry_count = (row.retry_count or 0) + 1
    await db.flush()
    emit(db, receive.matched_event(row))
    return await message_out(db, row)


async def discard(
    ctx: RequestContext, db: AsyncSession, message_id: uuid.UUID, data: DiscardIn
) -> IntakeMessageOut:
    require_human(actor_from_ctx(ctx))
    row = await _get(db, message_id, lock=True)
    if row.status in ("processing", "discarded"):
        raise ConflictError("This message can't be discarded now", code="intake_not_discardable")
    row.status = "discarded"
    row.status_reason = data.reason[:60]
    await db.flush()
    return await message_out(db, row)


# ================================================================ agent reads
def _readable_for_agent(row: DemandIntakeMessage) -> None:
    if (
        row.status in ("blocked", "unmatched", "ambiguous", "discarded", "received")
        or row.matched_client_id is None
    ):
        raise _not_found()


async def get_for_agent(
    ctx: RequestContext, db: AsyncSession, message_id: uuid.UUID, *, run_id: uuid.UUID | None = None
) -> IntakeForAgentOut:
    """``demands.get_intake``: refuses blocked / unmatched / ambiguous (``not_found``); marks the message
    ``processing`` and records the run."""
    from app.services import files_service

    row = await _get(db, message_id, lock=True)
    _readable_for_agent(row)
    if row.purged_at is not None:
        raise ConflictError("The message content was purged", code="content_purged")
    if row.status == "matched":
        row.status = "processing"
    if run_id is not None:
        row.run_id = run_id
    texts: list[AgentAttachmentText] = []
    for att in row.attachments or []:
        tid = att.get("text_file_id")
        if not tid:
            continue
        f = await files_service.get_file(db, uuid.UUID(str(tid)))
        if f is None or f.deleted_at is not None:
            continue
        text = (await files_service.read_file(f)).decode("utf-8", errors="replace")
        texts.append(
            AgentAttachmentText(
                filename=str(att.get("filename"))[:255],
                media_type=str(att.get("media_type")),
                text=text[:AGENT_TEXT_MAX],
            )
        )
    await db.flush()
    from app.services.demand_intake import settings as settings_mod

    ack = (await settings_mod.row(db)).ack_contact_on_intake
    return IntakeForAgentOut(
        id=row.id, source=row.source, subject=row.subject, received_at=row.received_at, match=await _match_out(db, row),
        body_text=(await _body(db, row))[:AGENT_TEXT_MAX], attachments=texts,
        acknowledge_contact=bool(ack and row.matched_contact_id is not None),
    )  # fmt: skip


async def match_of(ctx: RequestContext, db: AsyncSession, message_id: uuid.UUID) -> MatchOut:
    """The stored deterministic match (never matched anew from arguments)."""
    row = await _get(db, message_id)
    _readable_for_agent(row)
    return await _match_out(db, row)


async def accountable_owner(
    ctx: RequestContext, db: AsyncSession, subject: tuple[str, uuid.UUID] | None
) -> uuid.UUID | None:
    if subject is None or subject[0] != "demand_intake_message":
        return None
    row = await db.get(DemandIntakeMessage, subject[1])
    if row is None or row.status in ("unmatched", "ambiguous", "blocked", "discarded"):
        return None
    return row.owner_user_id


async def matched_client(db: AsyncSession, message_id: uuid.UUID) -> tuple[DemandIntakeMessage, uuid.UUID]:
    row = await _get(db, message_id, lock=True)
    _readable_for_agent(row)
    assert row.matched_client_id is not None
    return row, row.matched_client_id


# ================================================================ run outcome
async def finalize_from_run(
    db: AsyncSession, message_id: uuid.UUID, *, run_id: uuid.UUID, run_status: str, reason: str | None
) -> None:
    """Write the run outcome back: drafts -> ``drafted``; none -> ``no_roles`` (done / nothing to do) or ``failed``
    (with the run's reason, e.g. ``kill_switch`` -- "Retry" is then available)."""
    from app.models.agent_runtime import AgentRun

    row = await db.get(DemandIntakeMessage, message_id, with_for_update=True)
    if row is None or row.status in ("discarded", "blocked", "unmatched", "ambiguous"):
        return
    if row.run_id is not None and row.run_id != run_id:
        return  # an older attempt finishing late
    run = await db.get(AgentRun, run_id)
    outcome = (run.result or {}).get("outcome") if run is not None and isinstance(run.result, dict) else None
    row.processed_at = _now()
    if row.draft_demand_ids:
        row.status = "drafted"
        row.status_reason = None
        await _notify_drafted(db, row)
    elif run_status == "completed" and outcome in ("nothing_to_do", "done"):
        row.status = "no_roles"
        row.status_reason = None
    else:
        row.status = "failed"
        row.status_reason = (reason or run_status or "failed")[:60]
    await db.flush()


async def _notify_drafted(db: AsyncSession, row: DemandIntakeMessage) -> None:
    """``demand_drafts_created`` to the Accountable Owner (in-app + email), once per attempt."""
    from app.services import clients_service
    from app.services import notifications_service as ns

    if row.owner_user_id is None:
        return
    n = len(row.draft_demand_ids or [])
    client = await clients_service.client_name(db, row.matched_client_id) if row.matched_client_id else None
    content = ns.Content(
        title=f"Demand Agent drafted {n} demand{'s' if n != 1 else ''} from {client or 'a client'}'s email: {row.subject or '(no subject)'}"[
            :300
        ],
        link_path=f"/demands/intake?message={row.id}",
    )
    await ns.deliver(
        db,
        "demand_drafts_created",
        [row.owner_user_id],
        content,
        event_id=f"intake_drafted:{row.id}:{row.retry_count}",
    )


async def record_drafts(
    db: AsyncSession, row: DemandIntakeMessage, demand_id: uuid.UUID, client_name: str | None
) -> None:
    ids = list(row.draft_demand_ids or [])
    if demand_id not in ids:
        ids.append(demand_id)
    row.draft_demand_ids = ids
    row.status = "drafted"
    row.processed_at = _now()
    label = f"{client_name or 'Client'} · {len(ids)} role{'s' if len(ids) != 1 else ''} · {row.received_at:%d %b %Y}"
    row.group_label = label[:120]
    await db.flush()


# ================================================================ retention
async def purge_expired(db: AsyncSession, at: datetime | None = None) -> int:
    """Raw content after 180 days: files purged, ``extraction`` / ``body_preview`` nulled, status kept; the source
    quotes of demands still in ``draft`` are nulled (open demands keep them as business provenance)."""
    from app.models.demand_insights import DemandFieldSource
    from app.models.demands import Demand

    now = at or _now()
    rows = list(
        (
            await db.execute(
                select(DemandIntakeMessage)
                .where(DemandIntakeMessage.expires_at <= now, DemandIntakeMessage.purged_at.is_(None))
                .with_for_update(skip_locked=True)
                .limit(500)
            )
        ).scalars()
    )
    for row in rows:
        await purge_content(db, row)
        draft_ids = [
            d for (d,) in (
                await db.execute(select(Demand.id).where(Demand.id.in_(list(row.draft_demand_ids or [])), Demand.status == "draft"))
            ).all()
        ]  # fmt: skip
        if draft_ids:
            for src in (
                await db.execute(
                    select(DemandFieldSource).where(
                        DemandFieldSource.intake_message_id == row.id,
                        DemandFieldSource.demand_id.in_(draft_ids),
                    )
                )
            ).scalars():
                src.source_quote = None
        row.purged_at = now
    await db.flush()
    return len(rows)


async def purge_content(db: AsyncSession, row: DemandIntakeMessage) -> None:
    from app.services import files_service

    file_ids: list[Any] = [row.raw_file_id, row.body_text_file_id]
    for att in row.attachments or []:
        file_ids += [att.get("file_id"), att.get("text_file_id")]
    for fid in file_ids:
        if fid:
            await files_service.purge_file(db, uuid.UUID(str(fid)))
    row.extraction = None
    row.body_preview = None


__all__ = [
    "accountable_owner",
    "assign_client",
    "discard",
    "finalize_from_run",
    "get_for_agent",
    "get_message",
    "list_messages",
    "match_of",
    "matched_client",
    "message_out",
    "purge_content",
    "purge_expired",
    "record_drafts",
    "retry",
]


# ================================================================ upload / paste
async def create_from_upload(
    ctx: RequestContext,
    db: AsyncSession,
    *,
    client_id: uuid.UUID,
    text: str | None = None,
    upload: Any | None = None,
    file_data: bytes | None = None,
    filename: str | None = None,
) -> IntakeMessageOut:
    """ "Draft with Demand Agent": paste text or upload ``.eml`` / PDF / DOCX for a Client the user picks
    (``match_method=manual``, owner = the user). 409 ``demand_agent_disabled`` when ``agent_demand`` is off."""
    from app.core.permissions import check
    from app.services import ai, clients_service
    from app.services.demand_intake import settings as settings_mod

    require_human(actor_from_ctx(ctx))
    principal = ctx.principal
    check(principal, "demand:create")
    features = await ai.features_map(db)
    if not (features.get("agentic") and features.get("agent_demand")):
        raise ConflictError("The Demand Agent is turned off", code="demand_agent_disabled")
    await clients_service.get_client(ctx, db, client_id)
    text = text.strip() if text else None
    if upload is not None:
        from app.services.files_service import read_upload

        file_data = await read_upload(upload, 25)
        filename = getattr(upload, "filename", None)
    if (text is None) == (file_data is None):
        raise ValidationFailed("Provide either pasted text or one file", code="text_or_file_required")
    settings = await settings_mod.row(db)
    owner = getattr(principal, "id", None)
    manual = receive.Manual(client_id=client_id, contact_id=None, owner_user_id=owner)  # type: ignore[arg-type]
    if file_data is not None:
        name = (filename or "upload").strip() or "upload"
        part = receive.ParsedPart(name, "application/octet-stream", file_data)
        kind = receive.kind_of(part)
        if kind == "eml":
            parsed = receive.parse_mime(file_data)
            row, _ = await receive.ingest(
                ctx, db, settings, source="upload", parsed=parsed, raw=file_data, raw_filename=name,
                external_ref=None, created_by_kind="user", created_by_id=owner, manual=manual,
            )  # fmt: skip
        elif kind in ("pdf", "docx"):
            parsed = receive.ParsedEmail(
                message_id=None, from_name=None, from_address=None, subject=name[:500], sent_at=None, auth_header=None,
                body_text="", attachments=(part,),
            )  # fmt: skip
            row, _ = await receive.ingest(
                ctx, db, settings, source="upload", parsed=parsed, raw=None, external_ref=None,
                created_by_kind="user", created_by_id=owner, manual=manual,
            )  # fmt: skip
        else:
            raise ValidationFailed(
                "Only .eml, PDF and DOCX files are supported", code="unsupported_file_type",
                details={"allowed": ["docx", "eml", "pdf"]},
            )  # fmt: skip
    else:
        if len(text or "") > receive.MAX_SOURCE_CHARS:
            raise ValidationFailed("The text is too long (max 60,000 characters)", code="document_too_long")
        parsed = receive.ParsedEmail(
            message_id=None, from_name=None, from_address=None, subject=(text or "").splitlines()[0][:200] or "Pasted text",
            sent_at=None, auth_header=None, body_text=text or "", attachments=(),
        )  # fmt: skip
        row, _ = await receive.ingest(
            ctx, db, settings, source="paste", parsed=parsed, raw=None, external_ref=None,
            created_by_kind="user", created_by_id=owner, manual=manual,
        )  # fmt: skip
    return await message_out(db, row)
