# ruff: noqa: E501
"""Epic 53 (spec 0.13): pure transcript parsers -- TXT, VTT, SRT, DOCX, speaker mapping, a hypothesis round trip."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.schemas.transcripts import MAX_SEGMENT_CHARS
from app.services.transcripts import parsers
from app.services.transcripts.parsers import ParsedSegment, TranscriptParseError
from tests.epic3 import make_docx, make_zip

VTT = b"""WEBVTT

NOTE produced by a meeting tool

1
00:00:01.000 --> 00:00:04.250
<v Candidate>I have five years &amp; counting.</v>

00:05.000 --> 00:07.000
<v.loud Interviewer>Great, thanks.

00:08.000 --> 00:09.000
<v Priya Shah>Hello there

00:10.000 --> 00:11.000
Recruiter: and a
second line
"""

SRT = b"""1
00:00:01,000 --> 00:00:02,500
Agent: Hello, is now a good time?

2
00:00:03,000 --> 00:00:04,000
<i>Yes it is</i>
"""


def test_txt_lines_and_labels() -> None:
    segs = parsers.parse_txt(
        "AGENT: Hi there\n\n  candidate:   yes  please \nNote: keep this\nplain line\nUnknown: x"
    )
    assert [(s.speaker_role, s.text) for s in segs] == [
        ("agent", "Hi there"),
        ("candidate", "yes please"),
        ("unknown", "Note: keep this"),
        ("unknown", "plain line"),
        ("unknown", "x"),
    ]
    assert all(s.start_ms is None and s.end_ms is None for s in segs)


def test_vtt_timestamps_voices_and_entities() -> None:
    fmt, segs = parsers.parse(VTT, "call.vtt")
    assert fmt == "vtt"
    assert segs == [
        ParsedSegment("candidate", 1000, 4250, "I have five years & counting."),
        ParsedSegment("interviewer", 5000, 7000, "Great, thanks."),
        ParsedSegment("unknown", 8000, 9000, "Priya Shah: Hello there"),
        ParsedSegment("recruiter", 10000, 11000, "and a second line"),
    ]


def test_srt() -> None:
    fmt, segs = parsers.parse(SRT, "call.srt")
    assert fmt == "srt"
    assert segs == [
        ParsedSegment("agent", 1000, 2500, "Hello, is now a good time?"),
        ParsedSegment("unknown", 3000, 4000, "Yes it is"),
    ]


def test_docx_text_only() -> None:
    fmt, segs = parsers.parse(
        make_docx("Interviewer: Tell me about Python\nCandidate: Five years"), "notes.docx"
    )
    assert fmt == "docx"
    assert [(s.speaker_role, s.text) for s in segs] == [
        ("interviewer", "Tell me about Python"),
        ("candidate", "Five years"),
    ]


def test_long_lines_are_chunked() -> None:
    segs = parsers.parse_txt("Candidate: " + " ".join(["word"] * 4000))
    assert len(segs) == 3
    assert all(len(s.text) <= MAX_SEGMENT_CHARS and s.speaker_role == "candidate" for s in segs)


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (b"", "x.txt"),
        (b"   \n\n  ", "x.txt"),
        (b"\x00\x01\x02binary", "x.bin"),
        (b"\xff\xd8\xff\xe0 jpeg", "x.jpg"),
        (make_zip({"a.txt": b"hi"}), "x.zip"),
        (b"WEBVTT\n\nno cues here\n", "x.vtt"),
        (b"not a subtitle", "x.srt"),
    ],
)
def test_unparseable(data: bytes, name: str) -> None:
    with pytest.raises(TranscriptParseError):
        parsers.parse(data, name)


def test_format_ts() -> None:
    assert parsers.format_ts(None) == "--:--"
    assert parsers.format_ts(65_999) == "01:05"
    assert parsers.format_ts(3_725_000) == "62:05"


_WORDS = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJ0123456789.,?!'&", min_size=1, max_size=12)
_TEXT = st.lists(_WORDS, min_size=1, max_size=8).map(" ".join)


@st.composite
def _segments(draw: st.DrawFn) -> list[ParsedSegment]:
    out: list[ParsedSegment] = []
    t = 0
    for _ in range(draw(st.integers(min_value=1, max_value=8))):
        start = t + draw(st.integers(min_value=0, max_value=5000))
        end = start + draw(st.integers(min_value=0, max_value=20000))
        out.append(ParsedSegment(draw(st.sampled_from(parsers.ROLES)), start, end, draw(_TEXT)))
        t = end
    return out


@settings(max_examples=60, deadline=None)
@given(_segments())
def test_vtt_round_trip(segments: list[ParsedSegment]) -> None:
    fmt, parsed = parsers.parse(parsers.render_vtt(segments).encode(), "x.vtt")
    assert fmt == "vtt"
    assert parsed == segments


@settings(max_examples=60, deadline=None)
@given(_segments())
def test_txt_round_trip(segments: list[ParsedSegment]) -> None:
    _, parsed = parsers.parse(parsers.render_txt(segments).encode(), "x.txt")
    assert [(s.speaker_role, s.text) for s in parsed] == [(s.speaker_role, s.text) for s in segments]
