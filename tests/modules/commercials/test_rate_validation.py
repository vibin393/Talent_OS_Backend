"""Pure rate matching (Epic 16 A6): table-driven + hypothesis (determinism, specificity order)."""

import uuid
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.commercials.rate_validation import Line, RateQuery, evaluate, match_line, normalise

D = Decimal


def mk(
    role: str = "java developer",
    location: str | None = "bengaluru",
    lo: str | None = "3",
    hi: str | None = "6",
    mn: str = "1200",
    mx: str = "1800",
    vendor: str | None = None,
    currency: str = "INR",
    unit: str = "hour",
) -> Line:
    return Line(
        id=uuid.uuid4(),
        role_key=role,
        location_key=location,
        experience_min_years=D(lo) if lo else None,
        experience_max_years=D(hi) if hi else None,
        min_bill_rate=D(mn),
        max_bill_rate=D(mx),
        currency=currency,
        rate_unit=unit,
        max_vendor_rate=D(vendor) if vendor else None,
    )


def q(bill: str | None = "1500", **kw: object) -> RateQuery:
    base: dict[str, object] = {
        "role": "Java  Developer ",
        "location": "Bengaluru",
        "experience_years": D("4"),
        "currency": "INR",
        "rate_unit": "hour",
        "bill_rate": D(bill) if bill else None,
    }
    base.update(kw)
    return RateQuery(**base)  # type: ignore[arg-type]


def test_normalise() -> None:
    assert normalise("  Java   Developer ") == "java developer"
    assert normalise("   ") is None
    assert normalise(None) is None


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (q("1500"), "ok"),
        (q("1200"), "ok"),
        (q("1800"), "ok"),
        (q("1950"), "out_of_range"),
        (q("1100"), "out_of_range"),
        (q("1500", role="Python Developer"), "no_matching_line"),
        (q("1500", currency="USD"), "no_matching_line"),
        (q("1500", rate_unit="day"), "no_matching_line"),
        (q("1500", experience_years=D("8")), "no_matching_line"),
        (q("1500", experience_years=None), "no_matching_line"),
        (q("1500", location="Chennai"), "no_matching_line"),
    ],
)
def test_matrix(query: RateQuery, expected: str) -> None:
    lines = [mk()]
    assert evaluate(match_line(lines, query), query) == expected


def test_vendor_rate_exceeded() -> None:
    lines = [mk(vendor="1000")]
    assert (
        evaluate(match_line(lines, q(vendor_rate=D("1100"))), q(vendor_rate=D("1100")))
        == "vendor_rate_exceeded"
    )
    assert evaluate(match_line(lines, q(vendor_rate=D("900"))), q(vendor_rate=D("900"))) == "ok"


def test_specificity_location_beats_any_and_narrow_band_wins() -> None:
    anywhere = mk(location=None, mn="1000", mx="2000")
    located_wide = mk(lo="0", hi="20", mn="1100", mx="1900")
    located_narrow = mk(lo="3", hi="5", mn="1300", mx="1700")
    assert match_line([anywhere, located_wide, located_narrow], q()) == located_narrow
    assert match_line([anywhere, located_wide], q()) == located_wide
    # A query for another city only matches the "any location" line.
    assert match_line([anywhere, located_wide, located_narrow], q(location="Pune")) == anywhere


def test_ties_lowest_min_then_id() -> None:
    a = mk(mn="1300")
    b = mk(mn="1200")
    assert match_line([a, b], q()) == b
    twin = Line(
        uuid.UUID(int=0),
        b.role_key,
        b.location_key,
        b.experience_min_years,
        b.experience_max_years,
        b.min_bill_rate,
        b.max_bill_rate,
        b.currency,
        b.rate_unit,
    )
    assert match_line([a, b, twin], q()) == twin  # equal rank -> lowest id


line_st = st.builds(
    mk,
    role=st.sampled_from(["java developer", "qa engineer"]),
    location=st.sampled_from([None, "bengaluru", "pune"]),
    lo=st.sampled_from([None, "0", "2", "3"]),
    hi=st.sampled_from([None, "5", "6", "10"]),
    mn=st.sampled_from(["1000", "1200", "1300"]),
    mx=st.sampled_from(["1800", "2000"]),
)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(lines=st.lists(line_st, min_size=0, max_size=8), years=st.sampled_from([None, D("1"), D("4"), D("7")]))
def test_match_is_deterministic_and_most_specific(lines: list[Line], years: Decimal | None) -> None:
    query = q(experience_years=years)
    first = match_line(lines, query)
    assert first == match_line(list(reversed(lines)), query)
    if first is None:
        return
    # No other candidate is strictly more specific.
    for other in lines:
        if other is first or match_line([other], query) is None:
            continue
        if first.location_key is None:
            assert other.location_key is None
