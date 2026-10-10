# ruff: noqa: E501
"""Chunking of Knowledge Source text (Epic 62 A1; pure, hypothesis-tested).

Markdown headings start sections; a section's paragraphs are packed into chunks of about ``target`` characters (never
more than ``max_len``). A paragraph longer than ``max_len`` is split by sentence, then hard-split. The next chunk of a
section repeats the last sentence of the previous one (one-sentence overlap) when it fits.
"""

import dataclasses
import re

TARGET = 800
MAX_LEN = 2000
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


@dataclasses.dataclass(frozen=True, slots=True)
class Chunk:
    heading: str | None
    text: str


def _sections(text: str) -> list[tuple[str | None, str]]:
    out: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in text.replace("\r\n", "\n").split("\n"):
        m = _HEADING.match(line)
        if m:
            out.append((m.group(1).strip()[:300] or None, []))
        else:
            out[-1][1].append(line)
    return [(h, "\n".join(lines).strip()) for h, lines in out if "\n".join(lines).strip()]


def _hard_split(text: str, max_len: int) -> list[str]:
    pieces: list[str] = []
    rest = text.strip()
    while len(rest) > max_len:
        cut = rest.rfind(" ", 0, max_len)
        if cut <= 0:
            cut = max_len
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return [p for p in pieces if p]


def _units(paragraph: str, max_len: int) -> list[str]:
    """Split a paragraph into units of at most ``max_len`` characters (paragraph, else sentences, else hard splits)."""
    para = " ".join(paragraph.split())
    if len(para) <= max_len:
        return [para] if para else []
    units: list[str] = []
    for sentence in _SENTENCE.split(para):
        units.extend(_hard_split(sentence, max_len))
    return units


def _last_sentence(text: str) -> str:
    parts = [p for p in _SENTENCE.split(text) if p.strip()]
    return parts[-1].strip() if parts else ""


def chunk(text: str, *, target: int = TARGET, max_len: int = MAX_LEN) -> list[Chunk]:
    """Split ``text`` (markdown or plain) into chunks; every chunk is non-empty and at most ``max_len`` characters."""
    chunks: list[Chunk] = []
    for heading, body in _sections(text or ""):
        units: list[str] = []
        for paragraph in re.split(r"\n\s*\n", body):
            units.extend(_units(paragraph, max_len))
        current = ""
        for unit in units:
            candidate = f"{current}\n\n{unit}" if current else unit
            if current and (len(candidate) > max_len or len(current) >= target):
                chunks.append(Chunk(heading, current))
                overlap = _last_sentence(current)
                joined = f"{overlap} {unit}" if overlap and overlap != current else unit
                current = joined if len(joined) <= max_len and len(overlap) < target // 2 else unit
            else:
                current = candidate
        if current:
            chunks.append(Chunk(heading, current))
    return chunks


__all__ = ["MAX_LEN", "TARGET", "Chunk", "chunk"]
