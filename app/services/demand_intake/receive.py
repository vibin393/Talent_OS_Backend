# ruff: noqa: E501, D401, ARG001
"""Intake receive pipeline (Epic 55, A5 "receive", story 55.1).

1. Idempotency on ``(source, external_ref)`` (``Message-ID`` or the provider message id).
2. Caps: raw <= 25 MB; <= 10 attachments of <= 5 MB each (PDF, DOCX, TXT, EML extracted; PNG / JPG stored only).
3. Malware scan of everything (``core.scanning``) -- an infected part blocks the message (never processed).
4. Store the raw content and the extracted plain text as ``stored_files`` under the tenant prefix
   (``files_service.store_generated``, which scans again).
5. Text: HTML -> ``comms.html.html_to_text``; PDF / DOCX through the P1 intake extractor (10 pages, 60k chars).
6. Match the sender (``matching``), 7. status + owner, 8. emit ``DemandIntakeReceived`` (``DemandIntakeMatched`` when
   matched), 9. unmatched / ambiguous -> a Task + ``demand_intake_unmatched`` for the fallback owner.
"""

import dataclasses
import email
import email.policy
import email.utils
import uuid
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage, Message
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scanning, storage
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.core.events import emit
from app.events.demand_intake import DemandIntakeMatched, DemandIntakeReceived
from app.models.demand_intake import RAW_RETENTION_DAYS, DemandAgentSettings, DemandIntakeMessage
from app.services.demand_intake import matching

MAX_RAW_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENTS = 10
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_SOURCE_CHARS = 60_000  # the P1 intake limit
MAX_PDF_PAGES = 10
EXTRACTED_KINDS = frozenset({"pdf", "docx", "txt", "eml"})
STORED_ONLY_KINDS = frozenset({"png", "jpeg"})
_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain",
    "eml": "message/rfc822",
    "png": "image/png",
    "jpeg": "image/jpeg",
}


@dataclasses.dataclass(frozen=True, slots=True)
class ParsedPart:
    filename: str
    media_type: str
    data: bytes


@dataclasses.dataclass(frozen=True, slots=True)
class ParsedEmail:
    message_id: str | None
    from_name: str | None
    from_address: str | None
    subject: str | None
    sent_at: datetime | None
    auth_header: str | None
    body_text: str
    attachments: tuple[ParsedPart, ...]


def _now() -> datetime:
    return datetime.now(UTC)


def _part_text(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if not isinstance(payload, bytes):
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def parse_mime(raw: bytes) -> ParsedEmail:
    """Parse an RFC 822 message. Bodies: the first ``text/plain`` part, else ``text/html`` converted to text."""
    from app.services.comms.html import html_to_text

    if len(raw) > MAX_RAW_BYTES:
        raise ValidationFailed("The email is larger than 25 MB", code="email_too_large")
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    from_name, from_address = matching.parse_address(str(msg.get("From") or ""))
    sent_at: datetime | None = None
    if msg.get("Date"):
        try:
            sent_at = email.utils.parsedate_to_datetime(str(msg.get("Date")))
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            sent_at = None
    plain: list[str] = []
    html: list[str] = []
    parts: list[ParsedPart] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = part.get_filename()
        ctype = part.get_content_type().lower()
        if disposition == "attachment" or filename or ctype == "message/rfc822":
            data = part.get_payload(decode=True)
            if ctype == "message/rfc822" and not isinstance(data, bytes):
                inner = part.get_payload()
                inner_msg = inner[0] if isinstance(inner, list) and inner else inner
                data = inner_msg.as_bytes() if isinstance(inner_msg, Message) else b""
            if isinstance(data, bytes):
                parts.append(ParsedPart((filename or "attachment")[:255], ctype, data))
            continue
        if ctype == "text/plain":
            plain.append(_part_text(part))
        elif ctype == "text/html":
            html.append(_part_text(part))
    body = "\n\n".join(p for p in plain if p.strip()) or html_to_text("\n".join(html))
    subject = str(msg.get("Subject") or "").strip() or None
    message_id = str(msg.get("Message-ID") or "").strip() or None
    auth = msg.get_all("Authentication-Results")
    return ParsedEmail(
        message_id=message_id[:300] if message_id else None, from_name=(from_name or None), from_address=from_address,
        subject=subject[:500] if subject else None, sent_at=sent_at,
        auth_header="; ".join(str(a) for a in auth) if auth else None, body_text=body.strip(), attachments=tuple(parts),
    )  # fmt: skip


def build_eml(*, from_addr: str, to: str, subject: str, body: str, message_id: str | None = None,
              attachments: tuple[tuple[str, str, bytes], ...] = (), auth_results: str | None = None,
              html: bool = False) -> bytes:  # fmt: skip
    """Compose an RFC 822 message (tests, the Fake provider and demos; never sent anywhere)."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id or email.utils.make_msgid(domain="fake.test")
    msg["Date"] = email.utils.format_datetime(_now())
    if auth_results:
        msg["Authentication-Results"] = auth_results
    if html:
        msg.set_content(body, subtype="html")
    else:
        msg.set_content(body)
    for filename, media_type, data in attachments:
        maintype, _, subtype = media_type.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
    return msg.as_bytes()


def kind_of(part: ParsedPart) -> str | None:
    from app.services.files_service import sniff_kind

    sniffed = sniff_kind(part.data)
    if sniffed in ("pdf", "docx", "png", "jpeg"):
        return sniffed
    name = part.filename.lower()
    if part.media_type == "message/rfc822" or name.endswith(".eml"):
        return "eml"
    if part.media_type.startswith("text/plain") or name.endswith(".txt"):
        return "txt"
    return None


def _pdf_pages(data: bytes) -> int:
    import io

    from pypdf import PdfReader

    try:
        return len(PdfReader(io.BytesIO(data)).pages)
    except Exception:
        return 0


def extract_part_text(kind: str, data: bytes) -> tuple[str | None, str | None]:
    """``(text, problem)``; ``problem`` in ``too_long`` / ``unreadable``."""
    if kind == "txt":
        return data.decode("utf-8", errors="replace")[:MAX_SOURCE_CHARS], None
    if kind == "eml":
        try:
            inner = parse_mime(data)
        except ValidationFailed:
            return None, "too_long"
        return inner.body_text[:MAX_SOURCE_CHARS], None
    from app.services.candidates.extraction import extract_text

    if kind == "pdf" and _pdf_pages(data) > MAX_PDF_PAGES:
        return None, "too_long"
    result = extract_text(data, kind)
    if result.status != "ok" or not result.text:
        return None, "unreadable"
    return result.text[:MAX_SOURCE_CHARS], None


# ================================================================ storage
async def _store(
    ctx: RequestContext,
    db: AsyncSession,
    message_id: uuid.UUID,
    data: bytes,
    filename: str,
    content_type: str,
) -> uuid.UUID:
    from app.services import files_service

    tenant_id = ctx.tenant_id
    assert tenant_id is not None
    row = await files_service.store_generated(
        ctx, db, data, key=storage.new_key(tenant_id), filename=filename, content_type=content_type,
        owner_entity_type="demand_intake_message", owner_entity_id=message_id,
    )  # fmt: skip
    return row.id


@dataclasses.dataclass
class Prepared:
    blocked_reason: str | None
    body_text: str
    attachments: list[dict[str, Any]]
    attachment_texts: list[tuple[str, str, str]]  # (filename, media_type, text)


async def _prepare(
    ctx: RequestContext, db: AsyncSession, message_id: uuid.UUID, parsed: ParsedEmail
) -> tuple[Prepared, uuid.UUID | None]:
    """Scan every part, store clean ones, extract their text. Infected -> nothing processed."""
    attachments: list[dict[str, Any]] = []
    texts: list[tuple[str, str, str]] = []
    if len(parsed.attachments) > MAX_ATTACHMENTS:
        return Prepared("too_many_attachments", parsed.body_text, [], []), None
    scans = []
    for part in parsed.attachments:
        if len(part.data) > MAX_ATTACHMENT_BYTES:
            scans.append(None)
            continue
        result = await scanning.scan(part.data, part.filename)
        scans.append(result.status)
        if result.status == "infected":
            logger.warning("Demand intake message {}: an attachment failed the malware scan", message_id)
            return Prepared("infected_attachment", parsed.body_text, [], []), None
        if result.status == "error":
            return Prepared("scan_unavailable", parsed.body_text, [], []), None
    text_file_id: uuid.UUID | None = None
    if parsed.body_text:
        text_file_id = await _store(
            ctx, db, message_id, parsed.body_text.encode("utf-8"), "body.txt", "text/plain; charset=utf-8"
        )
    for part, scan in zip(parsed.attachments, scans, strict=True):
        kind = kind_of(part)
        entry: dict[str, Any] = {
            "file_id": None, "filename": part.filename, "media_type": part.media_type, "size": len(part.data),
            "scan_status": scan or "skipped_too_large", "text_file_id": None,
        }  # fmt: skip
        if scan is None or kind is None:
            entry["scan_status"] = "skipped_too_large" if scan is None else "unsupported"
            attachments.append(entry)
            continue
        entry["media_type"] = _CONTENT_TYPES[kind]
        entry["file_id"] = str(
            await _store(ctx, db, message_id, part.data, part.filename, _CONTENT_TYPES[kind])
        )
        if kind in EXTRACTED_KINDS:
            text, problem = extract_part_text(kind, part.data)
            if text:
                texts.append((part.filename, _CONTENT_TYPES[kind], text))
                entry["text_file_id"] = str(
                    await _store(
                        ctx,
                        db,
                        message_id,
                        text.encode("utf-8"),
                        f"{part.filename}.txt",
                        "text/plain; charset=utf-8",
                    )
                )
            elif problem:
                entry["text_status"] = problem
        attachments.append(entry)
    return Prepared(None, parsed.body_text, attachments, texts), text_file_id


# ================================================================ ingest
async def existing(db: AsyncSession, source: str, external_ref: str | None) -> DemandIntakeMessage | None:
    if not external_ref:
        return None
    return (
        await db.execute(
            select(DemandIntakeMessage).where(
                DemandIntakeMessage.source == source, DemandIntakeMessage.external_ref == external_ref
            )
        )
    ).scalar_one_or_none()


@dataclasses.dataclass(frozen=True, slots=True)
class Manual:
    client_id: uuid.UUID
    contact_id: uuid.UUID | None
    owner_user_id: uuid.UUID


async def ingest(
    ctx: RequestContext,
    db: AsyncSession,
    settings: DemandAgentSettings,
    *,
    source: str,
    parsed: ParsedEmail,
    raw: bytes | None,
    raw_filename: str = "message.eml",
    raw_content_type: str = "message/rfc822",
    external_ref: str | None,
    created_by_kind: str,
    created_by_id: uuid.UUID | None,
    manual: Manual | None = None,
) -> tuple[DemandIntakeMessage, bool]:
    """Store, scan, match and emit one message. Returns ``(message, duplicate)``."""
    found = await existing(db, source, external_ref)
    if found is not None:
        return found, True
    message_id = uuid.uuid4()
    now = _now()
    raw_file_id: uuid.UUID | None = None
    raw_blocked: str | None = None
    if raw is not None:
        scan = await scanning.scan(raw, raw_filename)
        if scan.status == "infected":
            raw_blocked = "infected_attachment"
        elif scan.status == "error":
            raw_blocked = "scan_unavailable"
        else:
            raw_file_id = await _store(ctx, db, message_id, raw, raw_filename, raw_content_type)
    prepared, text_file_id = (
        (Prepared(raw_blocked, parsed.body_text, [], []), None)
        if raw_blocked
        else await _prepare(ctx, db, message_id, parsed)
    )
    row = DemandIntakeMessage(
        id=message_id, tenant_id=settings.tenant_id, source=source, external_ref=external_ref,
        from_address=parsed.from_address, from_name=parsed.from_name[:200] if parsed.from_name else None,
        subject=parsed.subject, received_at=now, raw_file_id=raw_file_id, body_text_file_id=text_file_id,
        body_preview=(prepared.body_text[:500] or None) if not prepared.blocked_reason else None,
        attachments=prepared.attachments, status="received", created_by_kind=created_by_kind,
        created_by_id=created_by_id, draft_demand_ids=[], expires_at=now + timedelta(days=RAW_RETENTION_DAYS),
        retry_count=0,
    )  # fmt: skip
    if prepared.blocked_reason:
        if raw_file_id is not None:  # an infected part: keep only a tombstone of the raw message
            from app.services import files_service

            await files_service.purge_file(db, raw_file_id)
        row.status = "blocked" if prepared.blocked_reason == "infected_attachment" else "failed"
        row.status_reason = prepared.blocked_reason
        row.owner_user_id = settings.fallback_owner_id
        db.add(row)
        await db.flush()
        emit(db, _event(row))
        return row, False
    if manual is not None:
        row.status = "matched"
        row.match_method = "manual"
        row.matched_client_id = manual.client_id
        row.matched_contact_id = manual.contact_id
        row.owner_user_id = manual.owner_user_id
        row.auth_result = "unknown"
    else:
        result = await matching.match_sender(
            db,
            settings,
            from_address=parsed.from_address,
            auth_header=parsed.auth_header,
            body_text=parsed.body_text,
        )
        apply_match(row, result, settings)
    db.add(row)
    await db.flush()
    if row.status in ("unmatched", "ambiguous"):
        await follow_up_unmatched(ctx, db, row, settings)
    emit(db, _event(row))
    return row, False


def apply_match(
    row: DemandIntakeMessage, result: matching.MatchResult, settings: DemandAgentSettings
) -> None:
    row.status = result.status
    row.status_reason = result.reason
    row.auth_result = result.auth_result
    row.match_method = result.method
    row.matched_client_id = result.client_id
    row.matched_contact_id = result.contact_id
    row.forwarded_by_user_id = result.forwarded_by_user_id
    row.owner_user_id = result.owner_user_id if result.status == "matched" else settings.fallback_owner_id


def _event(row: DemandIntakeMessage) -> DemandIntakeReceived:
    cls = DemandIntakeMatched if row.status == "matched" else DemandIntakeReceived
    return cls(
        tenant_id=row.tenant_id, message_id=row.id, source=row.source, status=row.status,
        matched_client_id=row.matched_client_id, owner_user_id=row.owner_user_id, attempt=row.retry_count,
    )  # fmt: skip


def matched_event(row: DemandIntakeMessage) -> DemandIntakeReceived:
    return _event(row)


async def _fallback_recipient(db: AsyncSession, settings: DemandAgentSettings) -> uuid.UUID | None:
    from app.services import directory_service, entity_refs

    if (
        settings.fallback_owner_id is not None
        and settings.fallback_owner_id in await entity_refs.active_user_ids(db, [settings.fallback_owner_id])
    ):
        return settings.fallback_owner_id
    role_id = await directory_service.role_id_by_key(db, "tenant_admin")
    admins = sorted(await directory_service.user_ids_with_role(db, role_id)) if role_id else []
    active = sorted(await entity_refs.active_user_ids(db, admins))
    return active[0] if active else None


async def follow_up_unmatched(
    ctx: RequestContext, db: AsyncSession, row: DemandIntakeMessage, settings: DemandAgentSettings
) -> None:
    """A Task "Match intake email to a client" + the ``demand_intake_unmatched`` notification (fallback owner)."""
    from app.schemas.tasks import TaskCreate
    from app.services import notifications_service as ns
    from app.services import tasks_service

    owner = await _fallback_recipient(db, settings)
    if owner is None:
        return
    data = TaskCreate.model_construct(
        title="Match intake email to a client",
        notes=f"An intake email could not be matched to a client ({row.status}: {row.status_reason or 'unknown'}). "
        f"Open the Intake inbox: /demands/intake?message={row.id}",
        owner_id=owner, entity_type=None, entity_id=None, due_at=_now() + timedelta(hours=24), due_all_day=False,
        priority="normal", reminder_at=None,
    )  # fmt: skip
    async with db.begin_nested():
        task = await tasks_service.create_system_task(
            RequestContext(tenant_id=row.tenant_id), db, data, source="agent"
        )
    row.task_id = task.id
    domain = matching.domain_of(row.from_address) or "an unknown sender"
    content = ns.Content(
        title=f"An intake email from {domain} couldn't be matched to a client",
        link_path=f"/demands/intake?message={row.id}",
    )
    await ns.deliver(
        db,
        "demand_intake_unmatched",
        [owner],
        content,
        event_id=f"intake_unmatched:{row.id}:{row.retry_count}",
    )
    await db.flush()


__all__ = [
    "MAX_ATTACHMENTS",
    "MAX_ATTACHMENT_BYTES",
    "MAX_RAW_BYTES",
    "Manual",
    "ParsedEmail",
    "ParsedPart",
    "apply_match",
    "build_eml",
    "existing",
    "extract_part_text",
    "follow_up_unmatched",
    "ingest",
    "kind_of",
    "matched_event",
    "parse_mime",
]
