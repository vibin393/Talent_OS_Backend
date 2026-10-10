"""Client-formatted resume PDF (FR-36) with reportlab (pure Python; no native dependencies).

Layout: tenant name header, candidate full name, Submission ID line, "Submitted by {tenant}" footer on each
page. **Contact removal:** the structured profile carries no emails / phones / addresses / links by
construction, and every free-text value is scrubbed (emails, phone numbers in E.164, Indian 10-digit and
grouped forms, URLs, ``linkedin.com/...`` and social handles).
"""

import io
import re
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

_SCRUBBERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+"),  # emails
    re.compile(r"(?i)\b(?:https?://|www\.)\S+"),  # URLs
    re.compile(
        r"(?i)\b(?:[a-z0-9\-]+\.)*(?:linkedin|github|twitter|x|facebook|instagram|gitlab|behance|medium)\.com\S*"
    ),
    # bare domains (lower-case only, so "ASP.NET" / "Node.js" survive)
    re.compile(r"\b[a-z0-9\-]+(?:\.[a-z0-9\-]+)*\.(?:com|in|io|org|net|co|dev|me|ai|app)(?:/\S*)?(?![\w.])"),
    re.compile(r"\+\d[\d\s\-().]{7,17}\d"),  # E.164 and international formats
    re.compile(r"(?<!\d)(?:\+?91[\s\-]?|0)?[6-9]\d{4}[\s\-]?\d{5}(?!\d)"),  # Indian mobile, 10 digits / 5+5
    re.compile(r"(?<!\d)\(?\d{3}\)?[\s\-.]\d{3}[\s\-.]\d{4}(?!\d)"),  # 3-3-4 grouped
    re.compile(r"(?<!\d)0\d{2,4}[\s\-]\d{6,8}(?!\d)"),  # landline with STD code
    re.compile(r"(?<![\w.])@[A-Za-z0-9_]{2,30}\b"),  # social handles
)
REDACTED = ""


def scrub(text: str | None) -> str:
    """Remove contact details from free text (see module docstring)."""
    if not text:
        return ""
    out = str(text)
    for pattern in _SCRUBBERS:
        out = pattern.sub(REDACTED, out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


def _p(text: str | None) -> str:
    return escape(scrub(text))


def render(profile: dict[str, Any], *, tenant_name: str, submission_ref: str) -> bytes:
    """Render the client-formatted resume; returns PDF bytes."""
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=styles["Heading1"], fontSize=18, spaceAfter=2)
    h2 = ParagraphStyle(
        "h2",
        parent=styles["Heading2"],
        fontSize=12,
        spaceBefore=8,
        spaceAfter=4,
        textColor=colors.HexColor("#0F6CBD"),
    )
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=9.5, leading=13)
    small = ParagraphStyle("small", parent=body, fontSize=8.5, textColor=colors.HexColor("#555555"))
    tenant = escape(scrub(tenant_name) or "Talent OS")
    ref = escape(submission_ref)

    def _decorate(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        width, height = A4
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(18 * mm, height - 12 * mm, scrub(tenant_name) or "Talent OS")
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(width - 18 * mm, height - 12 * mm, f"Submission ID: {submission_ref}")
        canvas.drawString(18 * mm, 10 * mm, f"Submitted by {scrub(tenant_name) or 'Talent OS'}")
        canvas.drawRightString(width - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    story: list[Any] = [
        Paragraph(_p(profile.get("full_name")) or "Candidate", h1),
        Paragraph(f"Submission ID: {ref} &middot; Submitted by {tenant}", small),
    ]
    headline = profile.get("headline")
    facts = []
    if headline:
        facts.append(_p(headline))
    if profile.get("experience_years"):
        facts.append(f"{profile['experience_years']} years of experience")
    if profile.get("city"):
        facts.append(_p(profile["city"]))
    if profile.get("notice_period_days") is not None:
        facts.append(f"Notice period: {int(profile['notice_period_days'])} days")
    if facts:
        story += [Spacer(1, 4), Paragraph(" &middot; ".join(f for f in facts if f), body)]
    if profile.get("summary"):
        story += [Paragraph("Summary", h2), Paragraph(_p(profile["summary"]), body)]
    skills = [s for s in profile.get("skills") or [] if s.get("name")]
    if skills:
        parts = [
            _p(s["name"]) + (f" ({s['years']:g} yrs)" if isinstance(s.get("years"), int | float) else "")
            for s in skills
        ]
        story += [Paragraph("Skills", h2), Paragraph(", ".join(p for p in parts if p), body)]
    experience = profile.get("experience") or []
    if experience:
        story.append(Paragraph("Experience", h2))
        for e in experience:
            title = " &middot; ".join(x for x in (_p(e.get("title")), _p(e.get("employer"))) if x)
            period = " - ".join(
                x
                for x in (
                    (e.get("start") or "")[:7],
                    "present" if e.get("is_current") else (e.get("end") or "")[:7],
                )
                if x
            )
            story.append(
                Paragraph(
                    f"<b>{title or 'Role'}</b>"
                    + (f"  <font size=8>{escape(period)}</font>" if period else ""),
                    body,
                )
            )
            if e.get("description"):
                story.append(Paragraph(_p(e["description"]), body))
            story.append(Spacer(1, 3))
    education = profile.get("education") or []
    if education:
        story.append(Paragraph("Education", h2))
        for ed in education:
            line = ", ".join(
                x for x in (_p(ed.get("degree")), _p(ed.get("field")), _p(ed.get("institution"))) if x
            )
            if ed.get("end_year"):
                line += f" ({int(ed['end_year'])})"
            story.append(Paragraph(line or "-", body))
    certs = profile.get("certifications") or []
    if certs:
        story.append(Paragraph("Certifications", h2))
        for c in certs:
            line = " - ".join(x for x in (_p(c.get("name")), _p(c.get("issuer"))) if x)
            story.append(Paragraph(line or "-", body))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=20 * mm,
        bottomMargin=18 * mm,
        title=f"Candidate profile {submission_ref}",
        author=scrub(tenant_name) or "Talent OS",
    )
    doc.build(story, onFirstPage=_decorate, onLaterPages=_decorate)
    return buf.getvalue()
