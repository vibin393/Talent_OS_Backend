"""Signature Certificate (Epic 22 A6, FR-119): one page appended to the signed PDF.

It lists the request id, the certificate id, the document title and version, the **signed-content SHA-256**
(``doc_sha256``; a file cannot contain its own hash, so ``final_sha256`` is stored, shown in the UI / email and
checked by verification -- decision 19) and, per signer: name, email, typed name, signed-at (UTC), IP, channel
and whether the email code was used. A QR code carries ``{FRONTEND_URL}/verify?c=<certificate_id>``.

``acceptance_certificate`` is the P1 offer form (``offers/letter_pdf.acceptance_certificate`` delegates here).
"""

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from xml.sax.saxutils import escape  # nosec B406 - xml.sax.saxutils escape/quoteattr are output encoders, not parsers

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from app.core.settings import get_settings
from app.services.documents import render

CHANNEL_LABELS = {"portal": "Portal", "session": "Signed-in session", "secure_link": "Secure link"}


@dataclasses.dataclass(frozen=True, slots=True)
class SignerEvidence:
    name: str
    email: str
    typed_name: str
    signed_at: datetime
    ip: str | None
    channel: str
    otp_verified: bool


def verify_url(certificate_id: uuid.UUID) -> str:
    return f"{get_settings().frontend_url.rstrip('/')}/verify?c={certificate_id}"


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _table(rows: Sequence[tuple[str, str]], widths: tuple[float, float] = (55, 110)) -> Table:
    st = render.styles()
    table = Table(
        [[Paragraph(escape(k), st["cell"]), Paragraph(escape(v), st["cell"])] for k, v in rows],
        colWidths=[widths[0] * mm, widths[1] * mm],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#BBBBBB")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EEF3FA")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ]
        )
    )
    return table


def render_certificate(
    *,
    tenant_name: str,
    request_ref: str,
    certificate_id: uuid.UUID,
    title: str,
    version_no: int,
    doc_sha256: str,
    signers: Sequence[SignerEvidence],
    completed_at: datetime,
    heading: str = "Signature certificate",
) -> bytes:
    st = render.styles()
    flow: list[object] = [
        Paragraph(escape(heading), st["h1"]),
        Paragraph(
            "Every signer below reviewed the document identified by the SHA-256 fingerprint, agreed to sign "
            "electronically and typed their full name as their signature.",
            st["body"],
        ),
        Spacer(1, 4),
        _table(
            [
                ("Request", request_ref),
                ("Certificate ID", str(certificate_id)),
                ("Document", f"{title} (version {version_no})"),
                ("Signed-content SHA-256", doc_sha256),
                ("Completed at (UTC)", _utc(completed_at)),
            ]
        ),
        Spacer(1, 8),
    ]
    for i, s in enumerate(signers, start=1):
        flow += [
            Paragraph(escape(f"Signer {i}"), st["h3"]),
            _table(
                [
                    ("Name", s.name),
                    ("Email", s.email),
                    ("Typed name", s.typed_name),
                    ("Signed at (UTC)", _utc(s.signed_at)),
                    ("IP address", s.ip or "unknown"),
                    ("Channel", CHANNEL_LABELS.get(s.channel, s.channel)),
                    ("Email code", "verified" if s.otp_verified else "email code not used"),
                ]
            ),
            Spacer(1, 4),
        ]
    url = verify_url(certificate_id)
    flow += [
        Spacer(1, 6),
        render.qr_flowable(url),
        Paragraph(escape(f"Verify this certificate: {url}"), st["small"]),
    ]
    return render.build(
        flow,
        header=tenant_name,
        footer=f"{request_ref} · signature certificate",
        title=f"Signature certificate {request_ref}",
    )


def acceptance_certificate(
    *,
    tenant_name: str,
    offer_ref: str,
    version_no: int,
    typed_name: str,
    decided_at: datetime,
    ip: str | None,
    letter_sha256: str | None,
    channel: str,
) -> bytes:
    """The P1 one-page offer acceptance certificate (kept for offers accepted before Epic 22)."""
    st = render.styles()
    rows = [
        ("Offer", f"{offer_ref} (version {version_no})"),
        ("Accepted by (typed name)", typed_name),
        ("Accepted at (UTC)", _utc(decided_at)),
        ("IP address", ip or "unknown"),
        ("Channel", "Candidate portal" if channel == "portal" else "Secure link"),
        ("Letter SHA-256", letter_sha256 or "n/a"),
    ]
    flow = [
        Paragraph("Electronic acceptance certificate", st["h1"]),
        Paragraph(
            "The candidate accepted this offer electronically by typing their full name and agreeing that it "
            "is their electronic signature for the version shown below.",
            st["body"],
        ),
        Spacer(1, 6),
        _table(rows),
        Spacer(1, 10),
        Paragraph(
            "The SHA-256 fingerprint identifies the exact offer letter that was presented and accepted.",
            st["small"],
        ),
    ]
    return render.build(
        flow,
        header=tenant_name,
        footer=f"Offer {offer_ref} · version {version_no} · acceptance certificate",
        title=f"Acceptance certificate {offer_ref} v{version_no}",
    )
