"""The one document renderer (AD-28, Epic 22): Markdown -> PDF with ReportLab, pure Python.

Taken over from ``offers/letter_pdf.py`` (which keeps its public names as re-exports): ``markdown_flowables``
(``#``/``##``/``###`` headings, paragraphs, ``**bold**`` / ``*italic*``, ``-`` bullets, ``| a | b |`` tables, two
trailing spaces as a line break), the two-pass numbered canvas (header + ``footer · page x of y``),
``append_pages`` / ``pdf_text`` / ``page_count`` (pypdf) and ``render_markdown``. QR codes come from segno
(no native dependencies, no WeasyPrint).
"""

import html
import io
import re
from typing import Any
from xml.sax.saxutils import escape  # nosec B406 - xml.sax.saxutils escape/quoteattr are output encoders, not parsers

from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])")
_TABLE_SEP = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=base["BodyText"], fontSize=10.5, leading=14, spaceAfter=6)
    return {
        "h1": ParagraphStyle("h1", parent=base["Heading1"], fontSize=17, spaceAfter=8),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=13, spaceBefore=8, spaceAfter=4),
        "h3": ParagraphStyle("h3", parent=base["Heading3"], fontSize=11.5, spaceBefore=6, spaceAfter=3),
        "body": body,
        "cell": ParagraphStyle("cell", parent=body, spaceAfter=0),
        "small": ParagraphStyle(
            "small", parent=body, fontSize=9, leading=12, textColor=colors.HexColor("#555555")
        ),
    }


def inline(text: str) -> str:
    """Markdown inline -> reportlab markup. Values arrive HTML-escaped from the sandbox: normalise to plain
    text, XML-escape once, then apply bold / italic."""
    plain = html.unescape(text)
    out = escape(plain)
    out = _BOLD.sub(r"<b>\1</b>", out)
    out = _ITALIC.sub(r"<i>\1</i>", out)
    return out


def _cells(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [c.strip() for c in stripped.split("|")]


def markdown_flowables(markdown: str, styles: dict[str, ParagraphStyle] | None = None) -> list[Any]:
    st = styles or _styles()
    flow: list[Any] = []
    lines = markdown.replace("\r\n", "\n").split("\n")
    i = 0
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        parts: list[str] = []
        for raw in paragraph:
            hard_break = raw.endswith("  ")
            parts.append(inline(raw.strip()) + ("<br/>" if hard_break else " "))
        text = "".join(parts).strip()
        text = text.removesuffix("<br/>")
        if text:
            flow.append(Paragraph(text, st["body"]))
        paragraph.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            i += 1
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            flush_paragraph()
            level = len(heading.group(1))
            flow.append(Paragraph(inline(heading.group(2)), st[f"h{level}"]))
            i += 1
            continue
        if stripped.startswith("|"):
            flush_paragraph()
            rows: list[list[str]] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                if not _TABLE_SEP.match(lines[i].strip()):
                    rows.append(_cells(lines[i]))
                i += 1
            width = max(len(r) for r in rows)
            data = [[Paragraph(inline(c), st["cell"]) for c in (r + [""] * (width - len(r)))] for r in rows]
            table = Table(data, colWidths=[None] * width, hAlign="LEFT")
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#BBBBBB")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF3FA")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            flow += [table, Spacer(1, 6)]
            continue
        if re.match(r"^[-*]\s+", stripped):
            flush_paragraph()
            items: list[Any] = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i].strip()):
                item_text = re.sub(r"^[-*]\s+", "", lines[i].strip())
                items.append(ListItem(Paragraph(inline(item_text), st["body"]), leftIndent=12))
                i += 1
            flow += [ListFlowable(items, bulletType="bullet", start="•", leftIndent=12), Spacer(1, 4)]
            continue
        paragraph.append(line)
        i += 1
    flush_paragraph()
    return flow


def _numbered_canvas(header: str, footer_prefix: str) -> type[rl_canvas.Canvas]:
    class NumberedCanvas(rl_canvas.Canvas):
        """Two-pass canvas so the footer can say ``page x of y``."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._saved: list[dict[str, Any]] = []

        def showPage(self) -> None:  # ruff: ignore[invalid-function-name] - reportlab API
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self) -> None:
            total = len(self._saved)
            for state in self._saved:
                self.__dict__.update(state)
                self._decorate(total)
                super().showPage()
            super().save()

        def _decorate(self, total: int) -> None:
            width, height = A4
            self.setFont("Helvetica-Bold", 10)
            self.setFillColor(colors.HexColor("#0F6CBD"))
            self.drawString(20 * mm, height - 14 * mm, header)
            self.setStrokeColor(colors.HexColor("#DDDDDD"))
            self.line(20 * mm, height - 16 * mm, width - 20 * mm, height - 16 * mm)
            self.setFont("Helvetica", 8)
            self.setFillColor(colors.HexColor("#666666"))
            self.drawCentredString(
                width / 2, 10 * mm, f"{footer_prefix} · page {self._pageNumber} of {total}"
            )

    return NumberedCanvas


def _build(flow: list[Any], *, header: str, footer_prefix: str, title: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=24 * mm,
        bottomMargin=18 * mm,
        title=title,
        author=header,
    )
    doc.build(flow or [Spacer(1, 1)], canvasmaker=_numbered_canvas(header, footer_prefix))
    return buf.getvalue()


def append_pages(first: bytes, second: bytes) -> bytes:
    writer = PdfWriter()
    for data in (first, second):
        for page in PdfReader(io.BytesIO(data)).pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def pdf_text(data: bytes) -> str:
    """Extract text (tests and diagnostics)."""
    return "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages)


def render_markdown(markdown: str, *, header: str, footer: str, title: str) -> bytes:
    """Render a Markdown document (templates, letters, statements); returns PDF bytes."""
    return _build(markdown_flowables(markdown), header=header, footer_prefix=footer, title=title)


def page_count(data: bytes) -> int:
    return len(PdfReader(io.BytesIO(data)).pages)


def qr_flowable(url: str, size_mm: float = 32) -> Any:
    """A QR code image flowable (segno PNG, in memory)."""
    import segno
    from reportlab.platypus import Image

    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="png", scale=6, border=2)
    buf.seek(0)
    return Image(buf, width=size_mm * mm, height=size_mm * mm, hAlign="LEFT")


def build(flow: list[Any], *, header: str, footer: str, title: str) -> bytes:
    """Public form of ``_build`` (certificates and other generated pages)."""
    return _build(flow, header=header, footer_prefix=footer, title=title)


def styles() -> dict[str, ParagraphStyle]:
    return _styles()
