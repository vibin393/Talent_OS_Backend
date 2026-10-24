# ruff: file-ignore[line-too-long]
"""Registration links, QR codes and posters (Epic 43 story 43.1, FR-217).

* One P1 Secure Link per drive-Institution (purpose ``campus_registration``, subject ``drive_institution``,
  ``ttl`` until ``registration_closes_at`` + 1 day (180 days when open-ended), ``max_uses`` = cap or 100,000). The raw
  token is sealed with the tenant key (``drive_institutions.token_sealed``) so the URL can be shown again.
* The QR (segno) encodes the public URL ``/campus/r/{token}``; the PNG is stored through ``files_service`` (scanned)
  and the SVG / printable PDF poster (reportlab) are rendered on demand.
* Events: one registration link per event (purpose ``event_checkin``, subject ``recruitment_event``) and one personal
  single-use check-in link per registrant (subject ``event_checkin``).
"""

import io
import uuid
from datetime import datetime, timedelta

import segno
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit, secure_links
from app.core.context import RequestContext
from app.core.encrypted import open_sealed, seal
from app.core.errors import NotFound
from app.core.secure_links import SecureLink
from app.models.campus import CampusDrive, DriveInstitution, Institution, RecruitmentEvent
from app.services.campus.common import (
    LINK_EVENT,
    LINK_REGISTRATION,
    QR_OWNER,
    SUBJECT_DRIVE_INSTITUTION,
    SUBJECT_EVENT,
    conflict,
    drive_for,
    event_for,
    event_url,
    now,
    registration_url,
    settings_row,
    user_id_of,
)

DEFAULT_TTL = timedelta(days=180)
MAX_USES = 100_000


def qr_png(url: str) -> bytes:
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=10, border=2)
    return buf.getvalue()


def qr_svg(url: str, *, scale: int = 6) -> bytes:
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=scale, border=2, xmldecl=False)
    return buf.getvalue()


def qr_svg_inline(url: str) -> str:
    return segno.make(url, error="m").svg_inline(scale=5, border=2)


def poster_pdf(title: str, subtitle: str, url: str, footer: str | None, lines: tuple[str, ...] = ()) -> bytes:
    """A4 printable poster: title, subtitle, the QR and the URL."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    width, height = A4
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(title[:200])
    c.setFont("Helvetica-Bold", 26)
    c.drawCentredString(width / 2, height - 90, title[:60])
    c.setFont("Helvetica", 15)
    c.drawCentredString(width / 2, height - 120, subtitle[:90])
    y = height - 145
    for line in lines[:3]:
        c.drawCentredString(width / 2, y, line[:90])
        y -= 20
    size = 330
    c.drawImage(ImageReader(io.BytesIO(qr_png(url))), (width - size) / 2, y - size - 20, size, size)
    c.setFont("Helvetica", 11)
    c.drawCentredString(width / 2, y - size - 45, "Scan the code to register")
    c.setFont("Helvetica", 7)
    c.drawCentredString(width / 2, y - size - 62, url[:160])
    if footer:
        c.setFont("Helvetica", 9)
        c.drawCentredString(width / 2, 40, footer[:150])
    c.showPage()
    c.save()
    return buf.getvalue()


def _ttl(until: datetime | None) -> timedelta:
    if until is None:
        return DEFAULT_TTL
    left = until + timedelta(days=1) - now()
    return left if left > timedelta(hours=1) else timedelta(hours=1)


async def _store_qr(c: RequestContext, db: AsyncSession, di: DriveInstitution, url: str) -> uuid.UUID:
    from app.core import storage
    from app.services import files_service

    file = await files_service.store_generated(
        c, db, qr_png(url), key=storage.new_key(di.tenant_id), filename="registration-qr.png",
        content_type="image/png", owner_entity_type=QR_OWNER, owner_entity_id=di.id,
    )
    return file.id


async def issue_drive_link(c: RequestContext, db: AsyncSession, drive: CampusDrive, di: DriveInstitution) -> None:
    """Issue (or re-issue) the registration link + QR of one drive-Institution pair."""
    if di.registration_link_id is not None:
        await secure_links.revoke(db, di.registration_link_id)
    link, token = await secure_links.issue(
        db,
        tenant_id=di.tenant_id,
        purpose=LINK_REGISTRATION,
        subject_type=SUBJECT_DRIVE_INSTITUTION,
        subject_id=di.id,
        ttl=_ttl(drive.registration_closes_at),
        max_uses=di.registration_cap or MAX_USES,
        metadata={"drive_id": str(drive.id)},
        created_by=user_id_of(c),
    )
    di.registration_link_id = link.id
    di.token_sealed = seal(di.tenant_id, token)[0]
    di.qr_file_id = await _store_qr(c, db, di, registration_url(token))
    await db.flush()
    audit.record("campus.link_issued", "drive_institution", di.id, session=db, drive_id=str(drive.id))


async def revoke_drive_links(db: AsyncSession, drive: CampusDrive) -> int:
    rows = list((await db.execute(select(DriveInstitution).where(DriveInstitution.drive_id == drive.id))).scalars())
    count = 0
    for di in rows:
        if di.registration_link_id is not None and await secure_links.revoke(db, di.registration_link_id):
            count += 1
            audit.record("campus.link_revoked", "drive_institution", di.id, session=db, drive_id=str(drive.id))
    from app.services.campus.imports import revoke_invites

    count += await revoke_invites(db, drive.id)
    return count


async def link_state(db: AsyncSession, link_ids: list[uuid.UUID]) -> dict[uuid.UUID, SecureLink]:
    if not link_ids:
        return {}
    rows = await db.execute(select(SecureLink).where(SecureLink.id.in_(link_ids)))
    return {r.id: r for r in rows.scalars()}


def active(link: SecureLink | None) -> bool:
    return bool(
        link is not None and link.revoked_at is None and link.expires_at > now() and link.use_count < link.max_uses
    )


def token_of(tenant_id: uuid.UUID, sealed: object) -> str | None:
    from app.core.encrypted import Sealed

    if not isinstance(sealed, Sealed):
        return None
    try:
        return open_sealed(tenant_id, sealed)
    except Exception:  # pragma: no cover - key rotation drift
        return None


async def qr_download(
    c: RequestContext, db: AsyncSession, drive_id: uuid.UUID, drive_institution_id: uuid.UUID, fmt: str
) -> tuple[bytes, str, str]:
    drive = await drive_for(c, db, drive_id)
    di = await db.get(DriveInstitution, drive_institution_id)
    if di is None or di.drive_id != drive.id:
        raise NotFound("Institution not found on this drive")
    links = await link_state(db, [di.registration_link_id] if di.registration_link_id else [])
    token = token_of(di.tenant_id, di.token_sealed)
    if token is None or not active(links.get(di.registration_link_id)):  # type: ignore[arg-type]
        raise conflict("link_inactive", "Open registration first; this institution has no active link")
    url = registration_url(token)
    inst = await db.get(Institution, di.institution_id)
    name = str(inst.name) if inst is not None else "Institution"
    base = f"campus-{drive.batch_year}-{(name[:30] or 'institution').replace(' ', '-').lower()}"
    if fmt == "svg":
        return qr_svg(url), "image/svg+xml", f"{base}.svg"
    if fmt == "pdf":
        settings = await settings_row(db)
        closes = drive.registration_closes_at.strftime("%d %b %Y") if drive.registration_closes_at else None
        return (
            poster_pdf(drive.name, name, url, settings.poster_footer, (f"Batch {drive.batch_year}",) + ((f"Register by {closes}",) if closes else ())),
            "application/pdf",
            f"{base}.pdf",
        )
    if di.qr_file_id is not None:
        from app.services import files_service

        file = await files_service.get_file(db, di.qr_file_id)
        if file is not None and file.deleted_at is None:
            try:
                return await files_service.read_file(file), "image/png", f"{base}.png"
            except FileNotFoundError:  # pragma: no cover - storage drift: render again
                pass
    return qr_png(url), "image/png", f"{base}.png"


# ================================================================ events
async def issue_event_link(c: RequestContext, db: AsyncSession, event: RecruitmentEvent) -> None:
    if event.registration_link_id is not None:
        await secure_links.revoke(db, event.registration_link_id)
    left = event.ends_at - now()
    link, token = await secure_links.issue(
        db,
        tenant_id=event.tenant_id,
        purpose=LINK_EVENT,
        subject_type=SUBJECT_EVENT,
        subject_id=event.id,
        ttl=left if left > timedelta(hours=1) else timedelta(hours=1),
        max_uses=MAX_USES,
        created_by=user_id_of(c),
    )
    event.registration_link_id = link.id
    event.token_sealed = seal(event.tenant_id, token)[0]
    await db.flush()
    audit.record("campus.link_issued", "recruitment_event", event.id, session=db)


async def event_qr_download(c: RequestContext, db: AsyncSession, event_id: uuid.UUID, fmt: str) -> tuple[bytes, str, str]:
    event = await event_for(c, db, event_id)
    links = await link_state(db, [event.registration_link_id] if event.registration_link_id else [])
    token = token_of(event.tenant_id, event.token_sealed)
    if token is None or not active(links.get(event.registration_link_id)):  # type: ignore[arg-type]
        raise conflict("link_inactive", "Open the event first; it has no active registration link")
    url = event_url(token)
    base = f"event-{event.starts_at:%Y%m%d}"
    if fmt == "svg":
        return qr_svg(url), "image/svg+xml", f"{base}.svg"
    if fmt == "pdf":
        settings = await settings_row(db)
        when = event.starts_at.strftime("%d %b %Y")
        return poster_pdf(event.name, event.venue or "Recruitment event", url, settings.poster_footer, (when,)), "application/pdf", f"{base}.pdf"  # NOSONAR
    return qr_png(url), "image/png", f"{base}.png"
