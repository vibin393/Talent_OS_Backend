# ruff: noqa: E501
"""Render and deliver report files (Epic 47 story 47.4, FR-239).

* XLSX / CSV through the P1 export writer (``XlsxWriter``; ``csv_line`` / ``csv_cell`` formula escaping, UTF-8
  BOM); PDF through ``documents.render.render_markdown`` (title, run time, masked-field note, a table). A PDF
  holds at most ``PDF_MAX_ROWS`` rows: beyond that it carries the first rows plus a note pointing to the XLSX.
* Files are stored with ``files_service.store_generated`` under a tenant key (``storage.new_key``) (malware scan,
  ``stored_files`` row owned by ``report_run``).
* Delivery (``core.mailer``): an **attachment** when the file is <= ``attachment_max_mb`` and carries no
  ``compensation`` / ``identity`` / ``personal`` column; otherwise an **expiring link**
  (``GET /report-runs/{id}/download``, valid ``link_ttl_days``, only for that recipient) plus the
  ``report_delivered`` notification.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.mailer import EmailAttachment, EmailMessage, send_email
from app.core.settings import get_settings
from app.models.report_builder import ReportRun
from app.services.reporting.export import BOM, CONTENT_TYPES, XlsxWriter, csv_line

__all__ = [
    "LINK_CLASSES",
    "PDF_MAX_ROWS",
    "content_type",
    "deliver",
    "delivery_kind",
    "download_path",
    "filename",
    "render",
    "store",
]

PDF_MAX_ROWS = 5_000
LINK_CLASSES = frozenset({"compensation", "identity", "personal"})
_CONTENT_TYPES = {**CONTENT_TYPES, "pdf": "application/pdf"}


def content_type(fmt: str) -> str:
    return _CONTENT_TYPES[fmt]


def filename(name: str, fmt: str, at: datetime, tz: ZoneInfo) -> str:
    safe = (
        "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in name).strip().replace(" ", "-")
        or "report"
    )
    return f"{safe[:60]}-{at.astimezone(tz):%Y-%m-%d}.{fmt}"


def download_path(run_id: uuid.UUID) -> str:
    return f"/api/v1/report-runs/{run_id}/download"


def _cell(value: Any, tz: ZoneInfo) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _md(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "/").replace("\n", " ").replace("*", "")


def _pdf(title: str, header: list[str], body: list[list[Any]], *, run_at: str, notes: list[str]) -> bytes:
    from app.services.documents.render import render_markdown

    lines = [f"# {_md(title)}", "", f"Run at {run_at}", ""]
    lines += [f"*{_md(n)}*" for n in notes]
    lines += ["", "| " + " | ".join(_md(h) for h in header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(_md(v) for v in row) + " |" for row in body]
    return render_markdown(
        "\n".join(lines), header=_md(title)[:80], footer="Talent OS report", title=_md(title)
    )


def render(
    fmt: str,
    title: str,
    columns: Sequence[dict[str, Any]],
    rows: Sequence[dict[str, Any]],
    *,
    tz: ZoneInfo,
    run_at: datetime,
    masked_fields: Sequence[str] = (),
    limit_message: str | None = None,
) -> bytes:
    keys = [c["key"] for c in columns]
    header = [str(c.get("label") or c["key"]) for c in columns]
    body = [[_cell(row.get(k), tz) for k in keys] for row in rows]
    if fmt == "csv":
        return "".join([BOM, csv_line(header), *(csv_line(r) for r in body)]).encode("utf-8")
    if fmt == "xlsx":
        writer = XlsxWriter(title or "report", header)
        for r in body:
            writer.append(r)
        return writer.close()
    notes = []
    if masked_fields:
        notes.append("Hidden (requires Compensation / Identity access): " + ", ".join(masked_fields))
    if limit_message:
        notes.append(limit_message)
    if len(body) > PDF_MAX_ROWS:
        notes.append(
            f"Showing the first {PDF_MAX_ROWS:,} of {len(body):,} rows -- download the XLSX for all rows."
        )
        body = body[:PDF_MAX_ROWS]
    return _pdf(title, header, body, run_at=run_at.astimezone(tz).strftime("%Y-%m-%d %H:%M"), notes=notes)


async def store(
    ctx: RequestContext, db: AsyncSession, run: ReportRun, data: bytes, *, fmt: str, name: str
) -> uuid.UUID:
    from app.core import storage
    from app.services import files_service

    stored = await files_service.store_generated(
        ctx,
        db,
        data,
        key=storage.new_key(run.tenant_id),
        filename=name,
        content_type=content_type(fmt),
        owner_entity_type="report_run",
        owner_entity_id=run.id,
    )
    return stored.id


def delivery_kind(size_bytes: int, pii_classes: frozenset[str], attachment_max_mb: int) -> str:
    if pii_classes & LINK_CLASSES or size_bytes > attachment_max_mb * 1024 * 1024:
        return "link"
    return "attachment"


async def deliver(
    db: AsyncSession,
    run: ReportRun,
    *,
    to_email: str,
    recipient_name: str | None,
    report_name: str,
    data: bytes,
    fmt: str,
    name: str,
    kind: str,
    link_ttl_days: int,
    masked_fields: Sequence[str],
    limit_message: str | None,
) -> None:
    """Email the file (attachment) or an expiring link; the link variant also notifies in-app."""
    base = get_settings().frontend_url.rstrip("/")
    lines = [
        f"Hello {recipient_name or ''},".replace(" ,", ","),
        "",
        f"Your scheduled report '{report_name}' is ready.",
    ]
    if masked_fields:
        lines.append("Hidden columns (you do not have access): " + ", ".join(masked_fields) + ".")
    if limit_message:
        lines.append(limit_message)
    attachments: tuple[EmailAttachment, ...] = ()
    if kind == "attachment":
        attachments = (EmailAttachment(filename=name, content_type=content_type(fmt), data=data),)
        lines.append("The file is attached.")
    else:
        run.expires_at = (run.finished_at or datetime.now().astimezone()) + timedelta(days=link_ttl_days)
        lines.append(
            f"Download it here (valid {link_ttl_days} days, only for you): {base}{download_path(run.id)}"
        )
    await send_email(
        EmailMessage(
            to=to_email,
            subject=f"Report: {report_name}",
            text="\n".join(lines),
            tenant_id=run.tenant_id,
            kind="report_delivered",
            attachments=attachments,
        )
    )
    run.delivery = kind
    if kind == "link":
        from app.services import notifications_service as notifications

        await notifications.deliver(
            db,
            "report_delivered",
            [run.principal_user_id],
            notifications.Content(
                title=f"Report '{report_name}' is ready",
                body=f"Download link valid for {link_ttl_days} days.",
                entity_type="report_run",
                entity_id=run.id,
                link_path=download_path(run.id),
            ),
            event_id=f"report_delivered:{run.id}",
        )
    await db.flush()
