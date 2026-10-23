# ruff: file-ignore[line-too-long]
"""Pure transcript parsers (Epic 53, spec 0.13): TXT, WebVTT, SRT and DOCX (python-docx, text only).

* VTT / SRT: cue timestamps -> ``start_ms`` / ``end_ms``; the speaker comes from a ``<v Name>`` voice tag or a
  ``Name:`` prefix.
* Plain text (and DOCX paragraphs): one segment per non-empty line, with an optional ``Speaker:`` prefix.
* A speaker name maps to ``agent | candidate | interviewer | recruiter | unknown`` (case-insensitive); any other
  name is ``unknown`` and the name stays in the text (``"Priya: ..."``) so no information is lost.
* Segment text is whitespace-normalised and split into chunks of at most ``MAX_SEGMENT_CHARS``.

No I/O, no database: everything here is hypothesis-tested. Failures raise ``TranscriptParseError``.
"""

import html
import io
import re
import zipfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.schemas.transcripts import MAX_SEGMENT_CHARS

Format = Literal["txt", "vtt", "srt", "docx"]
ROLES = ("agent", "candidate", "interviewer", "recruiter", "unknown")

_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_CUE = re.compile(rf"^\s*{_TS}\s*-->\s*{_TS}")
_VOICE = re.compile(r"<v(?:\.[^\s>]*)?\s+([^>]*)>", re.IGNORECASE)
_TAG = re.compile(r"</?[^>]*>")
_LABEL = re.compile(r"^([A-Za-z][A-Za-z0-9 ._'-]{0,39}?)\s*:\s+(\S.*)$")
_SRT_INDEX = re.compile(r"^\d+$")


class TranscriptParseError(ValueError):
    """The bytes are not a transcript we can read (empty, binary, malformed)."""


@dataclass(frozen=True, slots=True)
class ParsedSegment:
    speaker_role: str
    start_ms: int | None
    end_ms: int | None
    text: str


# ================================================================ helpers
def role_for(name: str | None) -> str:
    """Map a speaker name / label to a role (case-insensitive); anything else is ``unknown``."""
    key = " ".join((name or "").split()).lower()
    return key if key in ROLES else "unknown"


def _norm(text: str) -> str:
    return " ".join(text.split())


def _chunks(text: str, limit: int = MAX_SEGMENT_CHARS) -> list[str]:
    out: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        out.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        out.append(rest)
    return out


def _ms(groups: Sequence[str | None]) -> int:
    hours, minutes, seconds, frac = groups
    millis = int((frac or "0").ljust(3, "0")[:3])
    return ((int(hours or 0) * 60 + int(minutes or 0)) * 60 + int(seconds or 0)) * 1000 + millis


def _split_label(line: str) -> tuple[str, str]:
    """``("candidate", "text")`` for ``"Candidate: text"``; unknown names keep the label in the text."""
    m = _LABEL.match(line)
    if m is None:
        return "unknown", line
    role = role_for(m.group(1))
    if role == "unknown" and m.group(1).strip().lower() != "unknown":
        return "unknown", line
    return role, m.group(2)


def _segments(role: str, start: int | None, end: int | None, text: str) -> list[ParsedSegment]:
    if start is not None and end is not None and end < start:
        end = start
    return [ParsedSegment(role, start, end, chunk) for chunk in _chunks(_norm(text))]


def _require(segments: list[ParsedSegment]) -> list[ParsedSegment]:
    if not segments:
        msg = "no transcript text found"
        raise TranscriptParseError(msg)
    return segments


# ================================================================ formats
def parse_txt(text: str) -> list[ParsedSegment]:
    out: list[ParsedSegment] = []
    for raw in text.splitlines():
        line = _norm(raw)
        if not line:
            continue
        role, body = _split_label(line)
        out.extend(_segments(role, None, None, body))
    return _require(out)


def _cue_text(lines: Iterable[str]) -> tuple[str, str]:
    """Role and plain text of a cue's payload lines (voice tag, else label prefix)."""
    joined = " ".join(line.strip() for line in lines if line.strip())
    voice = _VOICE.search(joined)
    plain = html.unescape(_TAG.sub("", joined))
    plain = _norm(plain)
    if voice is not None:
        name = _norm(voice.group(1))
        role = role_for(name)
        if role == "unknown" and name and name.lower() != "unknown":
            return "unknown", f"{name}: {plain}"
        return role, plain
    return _split_label(plain)


def _blocks(text: str) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _cues(blocks: list[list[str]]) -> list[ParsedSegment]:
    out: list[ParsedSegment] = []
    for block in blocks:
        idx = next((i for i, line in enumerate(block) if _CUE.match(line)), None)
        if idx is None:
            continue  # header, NOTE, STYLE, REGION or a stray block
        m = _CUE.match(block[idx])
        assert m is not None
        start, end = _ms(m.groups()[:4]), _ms(m.groups()[4:])
        payload = block[idx + 1 :]
        if not payload:
            continue
        role, body = _cue_text(payload)
        if body:
            out.extend(_segments(role, start, end, body))
    return _require(out)


def parse_vtt(text: str) -> list[ParsedSegment]:
    body = text.lstrip("﻿")
    if not body.lstrip().startswith("WEBVTT"):
        msg = "missing WEBVTT header"
        raise TranscriptParseError(msg)
    return _cues(_blocks(body))


def parse_srt(text: str) -> list[ParsedSegment]:
    blocks = _blocks(text.lstrip("﻿"))
    if not blocks or not any(_CUE.match(line) for line in blocks[0][:2]):
        msg = "not an SRT file"
        raise TranscriptParseError(msg)
    return _cues(blocks)


def docx_text(data: bytes) -> str:
    """Paragraph text of a DOCX (python-docx; text only, tables included)."""
    try:
        import docx

        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # python-docx raises several types for corrupt packages
        msg = "unreadable DOCX"
        raise TranscriptParseError(msg) from exc
    lines = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append(" ".join(cell.text for cell in row.cells))
    return "\n".join(lines)


def parse_docx(data: bytes) -> list[ParsedSegment]:
    return parse_txt(docx_text(data))


# ================================================================ dispatch
def _is_docx(data: bytes) -> bool:
    if not data.startswith(b"PK\x03\x04"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            return "word/document.xml" in zf.namelist()
    except (zipfile.BadZipFile, ValueError, OSError):
        return False


def decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            text = data.decode("utf-16")
        except UnicodeDecodeError as exc:
            msg = "undecodable text"
            raise TranscriptParseError(msg) from exc
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            msg = "transcripts must be UTF-8 text"
            raise TranscriptParseError(msg) from exc
    if "\x00" in text:
        msg = "binary content"
        raise TranscriptParseError(msg)
    return text


def detect_format(data: bytes, filename: str | None = None) -> Format:
    """Detect the transcript format from the bytes (the extension only breaks a tie for SRT and VTT)."""
    if data.startswith(b"PK\x03\x04"):
        if _is_docx(data):
            return "docx"
        msg = "unsupported archive"
        raise TranscriptParseError(msg)
    text = decode_text(data)
    stripped = text.lstrip("﻿").lstrip()
    if stripped.startswith("WEBVTT"):
        return "vtt"
    blocks = _blocks(stripped)
    if blocks and any(_CUE.match(line) for line in blocks[0][:2]):
        return "srt"
    if (filename or "").lower().endswith((".vtt", ".srt")):
        msg = "malformed subtitle file"
        raise TranscriptParseError(msg)
    return "txt"


def parse(data: bytes, filename: str | None = None) -> tuple[Format, list[ParsedSegment]]:
    """Detect and parse; returns ``(format, segments)`` with at least one segment."""
    if not data:
        msg = "empty file"
        raise TranscriptParseError(msg)
    fmt = detect_format(data, filename)
    if fmt == "docx":
        return fmt, parse_docx(data)
    text = decode_text(data)
    if fmt == "vtt":
        return fmt, parse_vtt(text)
    if fmt == "srt":
        return fmt, parse_srt(text)
    return fmt, parse_txt(text)


# ================================================================ rendering (round trips, model input)
def format_ts(ms: int | None) -> str:
    """``mm:ss`` (minutes are not wrapped at 60) or ``--:--``."""
    if ms is None:
        return "--:--"
    total = ms // 1000
    return f"{total // 60:02d}:{total % 60:02d}"


def _vtt_ts(ms: int) -> str:
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def render_vtt(segments: Sequence[ParsedSegment]) -> str:
    """WebVTT with ``<v Role>`` voice tags (timestamps default to 0)."""
    cues = ["WEBVTT", ""]
    for seg in segments:
        start = seg.start_ms or 0
        end = max(seg.end_ms or start, start)
        cues += [
            f"{_vtt_ts(start)} --> {_vtt_ts(end)}",
            f"<v {seg.speaker_role.title()}>{html.escape(seg.text)}",
            "",
        ]
    return "\n".join(cues)


def render_txt(segments: Sequence[ParsedSegment]) -> str:
    return "\n".join(f"{seg.speaker_role.title()}: {seg.text}" for seg in segments)


__all__ = [
    "ROLES",
    "Format",
    "ParsedSegment",
    "TranscriptParseError",
    "decode_text",
    "detect_format",
    "docx_text",
    "format_ts",
    "parse",
    "parse_docx",
    "parse_srt",
    "parse_txt",
    "parse_vtt",
    "render_txt",
    "render_vtt",
    "role_for",
]
