"""Epic 62 pure units: chunker (hypothesis), grounding check (table + hypothesis)."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.knowledge.chunking import MAX_LEN, chunk
from app.services.knowledge.grounding import check_answer, dates, money

WORDS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12)


@given(st.lists(st.lists(WORDS, min_size=1, max_size=400), min_size=1, max_size=8))
@settings(max_examples=60, deadline=None)
def test_chunks_bounded_and_cover_every_word(paragraphs: list[list[str]]) -> None:
    text = "\n\n".join(" ".join(p) + "." for p in paragraphs)
    chunks = chunk(text)
    assert chunks
    assert all(0 < len(c.text) <= MAX_LEN for c in chunks)
    joined = " ".join(c.text for c in chunks)
    for p in paragraphs:
        for w in p:
            assert w in joined


def test_headings_start_sections() -> None:
    chunks = chunk(
        "# Payroll FAQ\n\nSalary is credited on the 7th.\n\n## IT\n\nLaptops are issued on day one."
    )
    assert [c.heading for c in chunks] == ["Payroll FAQ", "IT"]


def test_long_paragraph_is_split_with_overlap() -> None:
    sentences = [f"Sentence number {i} explains a rule of onboarding." for i in range(120)]
    chunks = chunk(" ".join(sentences), target=400, max_len=800)
    assert len(chunks) > 3 and all(len(c.text) <= 800 for c in chunks)


@pytest.mark.parametrize(
    ("answer", "cited", "facts", "violations"),
    [
        (
            "Laptops are issued on day one at the Pune office.",
            ["Laptops are issued on day one at the Pune office"],
            [],
            (),
        ),
        ("Your joining bonus is ₹50,000.", ["Joining bonus is paid after 6 months."], [], ("money",)),
        ("Your joining bonus is ₹50,000.", ["The joining bonus is Rs 50,000."], [], ()),
        (
            "Salary is credited on the 7th of each month.",
            ["Salary is credited on the 7th of each month"],
            [],
            (),
        ),
        (
            "Upload your PAN by 15 October.",
            ["Upload your PAN"],
            ["Task PAN card: status pending, due 2026-10-15"],
            (),
        ),
        (
            "Upload your PAN by 20 October.",
            ["Upload your PAN"],
            ["Task PAN card: status pending, due 2026-10-15"],
            ("date",),
        ),
        ("Your documents are approved.", ["Upload documents in the portal"], [], ("decision",)),
        ("See https://evil.test/x for details.", ["See the portal"], [], ("link",)),
        ("You get 20% extra.", ["No extras"], [], ("percentage",)),
        ("Probation is 6 months.", ["Probation is 6 months"], [], ()),
        ("Probation is 3 months.", ["Probation is 6 months"], [], ("duration",)),
        ("Hello", [], [], ("no_citations",)),
        ("x" * 1501, ["x"], [], ("too_long",)),
    ],
)
def test_check_answer_table(
    answer: str, cited: list[str], facts: list[str], violations: tuple[str, ...]
) -> None:
    result = check_answer(answer, cited, facts)
    assert result.violations == violations
    assert result.ok is (not violations)


@given(st.integers(min_value=1000, max_value=9_999_999))
@settings(max_examples=50, deadline=None)
def test_unsupported_amount_always_rejected(amount: int) -> None:
    support = "Your stipend is paid monthly."
    assert not check_answer(f"You will get ₹{amount:,}.", [support], []).ok
    assert check_answer(f"You will get ₹{amount:,}.", [f"The amount is INR {amount}"], []).ok


@given(st.lists(WORDS, min_size=1, max_size=30))
@settings(max_examples=50, deadline=None)
def test_answer_copied_from_source_is_grounded(words: list[str]) -> None:
    text = " ".join(words)
    assert check_answer(text, [text], []).ok


def test_extractors() -> None:
    assert money("₹ 1,20,000 or 3 lakh") == {"120000", "3"}
    assert dates("Join on 2026-11-02, a Monday") >= {"11-2", "monday"}
