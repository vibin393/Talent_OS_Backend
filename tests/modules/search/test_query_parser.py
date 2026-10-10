"""Story 5.1: Boolean query parser golden tables (pure; no DB)."""

import pytest

from app.services.search import query_parser as qp
from app.services.search.query_parser import And, Not, Or, Phrase, QuerySyntaxError, Term

VALID: list[tuple[str, qp.Node]] = [
    ("databricks", Term("databricks")),
    ("data*", Term("data", prefix=True)),
    ('"data engineer"', Phrase("data engineer")),
    ("a b", And((Term("a"), Term("b")))),
    ("a AND b", And((Term("a"), Term("b")))),
    ("a OR b", Or((Term("a"), Term("b")))),
    ("a and b", And((Term("a"), Term("and"), Term("b")))),  # lowercase operators are terms
    ("a or b", And((Term("a"), Term("or"), Term("b")))),
    ("a NOT b", And((Term("a"), Not(Term("b"))))),
    ("a -b", And((Term("a"), Not(Term("b"))))),
    ("c-level", Term("c-level")),
    ("a OR b AND c", Or((Term("a"), And((Term("b"), Term("c")))))),
    (
        '("data engineer" OR dataengineer) AND databricks NOT sas',
        And((Or((Phrase("data engineer"), Term("dataengineer"))), Term("databricks"), Not(Term("sas")))),
    ),
    ("((a))", Term("a")),
    ("NOT a b", And((Not(Term("a")), Term("b")))),
]

INVALID: list[tuple[str, int, str]] = [
    ("databricks AND (azure", 16, "Unclosed parenthesis"),
    ('"data engineer', 1, "Unclosed quote"),
    ("a AND", 3, "AND needs a term on both sides"),
    ("AND a", 1, "AND needs a term on both sides"),
    ("a OR", 3, "OR needs a term on both sides"),
    ("a OR OR b", 3, "OR needs a term on both sides"),
    ("a NOT", 3, "NOT needs a term"),
    ("()", 1, "Empty parentheses"),
    ("a )", 3, "Unexpected closing parenthesis"),
    ("NOT sas", 1, "A query needs at least one term that is not negated"),
    ("-a -b", 1, "A query needs at least one term that is not negated"),
    ("a OR NOT b", 1, "A query needs at least one term that is not negated"),
    ("", 1, "Empty query"),
    ("   ", 1, "Empty query"),
    ('""', 1, "Empty phrase"),
    ("&&&", 1, "Search term has no letters or digits"),
]


@pytest.mark.parametrize(("query", "expected"), VALID)
def test_valid_queries(query: str, expected: qp.Node) -> None:
    assert qp.parse(query) == expected


@pytest.mark.parametrize(("query", "position", "message"), INVALID)
def test_invalid_queries(query: str, position: int, message: str) -> None:
    with pytest.raises(QuerySyntaxError) as info:
        qp.parse(query)
    assert (info.value.position, info.value.message) == (position, message)


def test_limits() -> None:
    with pytest.raises(QuerySyntaxError, match="too long"):
        qp.parse("a" * 1001)
    with pytest.raises(QuerySyntaxError, match="Too many search terms"):
        qp.parse(" ".join(f"t{i}" for i in range(65)))
    qp.parse(" ".join(f"t{i}" for i in range(64)))
    with pytest.raises(QuerySyntaxError, match="nested"):
        qp.parse("(" * 9 + "a" + ")" * 9)
    qp.parse("(" * 8 + "a" + ")" * 8)


def test_lexemes_are_sanitised() -> None:
    # Only letters / digits ever reach to_tsquery: tsquery operators and quotes are stripped.
    for raw in ("a:* & !b", "x' | 'y", "foo<->bar", "c++", "o'reilly", "naïve"):
        for lexeme in qp.lexemes(raw):
            assert lexeme.isalnum(), (raw, lexeme)
    assert qp.lexemes("naïve café") == ["naïve", "café"]


def test_round_trip_and_helpers() -> None:
    node = qp.parse('("data engineer" OR dataengineer) AND databricks NOT sas')
    assert qp.parse(qp.to_text(node)) == node
    assert qp.count_atoms(node) == 4
    assert qp.positive_terms(node) == ["data engineer", "dataengineer", "databricks"]
    assert qp.is_single_atom(qp.parse("databricks"))
    assert not qp.is_single_atom(qp.parse("a b"))
