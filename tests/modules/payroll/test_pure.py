"""Epic 28 pure tests: Decimal earnings (AD-20), period cut-off maths, item -> period mapping, hashes and the
statutory guard (export only, PRD OQ2)."""

import ast
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services.payroll import earnings
from app.services.payroll.earnings import PricedItem
from app.services.payroll.pay_periods import PeriodView, map_period, next_period, period_containing

D = Decimal


def hourly(q: str, rate: str, mult: str = "1.0000", ccy: str = "INR") -> Decimal:
    return earnings.price_time(
        quantity=D(q), rate=D(rate), rate_unit="hour", multiplier=D(mult), category="regular", currency=ccy
    )[0]


def test_half_up_hourly_and_ot() -> None:
    assert hourly("7.83", "437.5000") == D("3425.63")  # 3425.625 -> HALF_UP (HALF_EVEN would give .62)
    assert hourly("1.50", "437.5000", "1.5000") == D("984.38")  # 984.375


def test_day_rate_quantized_once() -> None:
    kw = {
        "rate_unit": "day",
        "multiplier": D(1),
        "category": "regular",
        "currency": "INR",
        "hours_per_day": D("8.00"),
    }
    assert earnings.price_time(quantity=D("4.00"), rate=D("3500.0000"), **kw)[0] == D("1750.00")
    amt = earnings.price_time(quantity=D("7.83"), rate=D("3333.3333"), **kw)[0]
    assert amt == D("3262.50")
    rounded_first = ((D("7.83") / D("8.00")).quantize(D("0.01")) * D("3333.3333")).quantize(D("0.01"))
    assert rounded_first != amt  # rounding the division first gives a different value


def test_monthly_base_and_unpriced_categories() -> None:
    assert earnings.monthly_base(D("100000.00"), D("0.954545"), "INR") == D("95454.50")
    amt, _ = earnings.price_time(
        quantity=D("8"),
        rate=D("100000"),
        rate_unit="month",
        multiplier=D(1),
        category="regular",
        currency="INR",
    )
    assert amt == D("0.00")
    ot, used = earnings.price_time(
        quantity=D("2"),
        rate=D("100000"),
        rate_unit="month",
        multiplier=D("1.5"),
        category="ot",
        currency="INR",
        hourly_equivalent=D("100000") / D(176),
    )
    assert ot == D("1704.55") and used is not None
    with pytest.raises(ValueError, match="hourly_equivalent"):
        earnings.price_time(
            quantity=D("1"), rate=D("1"), rate_unit="month", multiplier=D(1), category="ot", currency="INR"
        )


def test_signed_adjustments_and_jpy() -> None:
    assert earnings.price_rate_adjustment(D("7.83"), D("-30.0000"), D("1.5"), "INR") == D("-352.35")
    assert hourly("-1.00", "437.5000", "1.5000") == D("-656.25")
    assert hourly("7.83", "2000", ccy="JPY") == D("15660")


def _item(
    cid: uuid.UUID, amount: str, ccy: str = "INR", kind: str = "timesheet_entry", cat: str = "regular"
) -> PricedItem:
    return PricedItem(
        kind,
        uuid.uuid4(),
        uuid.uuid4(),
        cid,
        ccy,
        cat,
        date(2026, 10, 1),
        D("7.83"),
        D("437.5"),
        D(1),
        D(amount),
    )


def test_rows_per_currency_and_reimbursements_not_in_gross() -> None:
    cid = uuid.uuid4()
    rows = earnings.build_rows(
        [
            _item(cid, "3425.63"),
            _item(cid, "3425.63"),
            _item(cid, "15660", "JPY"),
            _item(cid, "1026.57", kind="expense_line", cat="travel"),
        ]
    )
    assert [r.currency for r in rows] == ["INR", "JPY"]
    inr = rows[0]
    assert inr.earnings["regular"] == D("6851.26") and inr.reimbursements_total == D("1026.57")
    assert inr.gross_earnings_inputs == D("6851.26")
    totals = earnings.totals_by_currency(rows)
    assert totals["INR"]["gross_earnings_inputs"] == "6851.26"
    assert totals["JPY"]["gross_earnings_inputs"] == "15660"


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(st.decimals(min_value=-200, max_value=200, places=2, allow_nan=False), min_size=1, max_size=20),
    st.decimals(min_value=0, max_value=5000, places=4, allow_nan=False),
)
def test_row_total_is_exact_sum_and_sign_preserved(qs: list[Decimal], rate: Decimal) -> None:
    cid = uuid.uuid4()
    items = []
    for q in qs:
        amt = hourly(str(q), str(rate))
        assert amt == 0 or (amt > 0) == (q * rate > 0)
        items.append(_item(cid, str(amt)))
    (row,) = earnings.build_rows(items)
    assert row.earnings["regular"] == sum((i.amount for i in items), D(0))


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.decimals(min_value=0, max_value=100, places=2, allow_nan=False),
    st.integers(min_value=1, max_value=6),
    st.decimals(min_value=1, max_value=5000, places=4, allow_nan=False),
)
def test_splitting_changes_total_by_at_most_a_cent_per_part(q: Decimal, parts: int, rate: Decimal) -> None:
    whole = hourly(str(q), str(rate))
    base = (q / parts).quantize(D("0.01"), rounding="ROUND_DOWN")
    chunks = [base] * (parts - 1) + [q - base * (parts - 1)]
    split = sum((hourly(str(c), str(rate)) for c in chunks), D(0))
    assert abs(split - whole) <= D("0.01") * parts
    assert hourly(str(q), str(rate), "0") == D("0.00")


def test_cutoff_periods() -> None:
    assert period_containing(date(2026, 10, 10), 25) == (date(2026, 9, 26), date(2026, 10, 25))
    assert period_containing(date(2026, 10, 26), 25) == (date(2026, 10, 26), date(2026, 11, 25))
    assert period_containing(date(2028, 2, 10), None) == (date(2028, 2, 1), date(2028, 2, 29))
    assert next_period(date(2026, 10, 25), 25) == (date(2026, 10, 26), date(2026, 11, 25))
    assert next_period(date(2026, 12, 31), None) == (date(2027, 1, 1), date(2027, 1, 31))


def test_map_period_modes_and_timezone() -> None:
    sep = PeriodView(
        uuid.uuid4(), date(2026, 9, 1), date(2026, 9, 30), "locked", datetime(2026, 10, 1, 6, tzinfo=UTC)
    )
    octo = PeriodView(uuid.uuid4(), date(2026, 10, 1), date(2026, 10, 31), "open", None)
    late = datetime(2026, 10, 2, 9, tzinfo=UTC)
    assert map_period(date(2026, 9, 15), late, "item_date", [sep, octo]) is sep
    assert map_period(date(2026, 9, 15), late, "approval_date", [sep, octo]) is octo
    early = datetime(2026, 9, 30, 5, tzinfo=UTC)
    assert map_period(date(2026, 9, 15), early, "approval_date", [sep, octo]) is sep
    # Locked at 23:00 UTC on 30 Sep; approved 23:30 UTC = 1 Oct 05:00 IST -> October
    sep2 = PeriodView(sep.id, sep.start, sep.end, "locked", datetime(2026, 9, 30, 23, 0, tzinfo=UTC))
    at = datetime(2026, 9, 30, 23, 30, tzinfo=UTC)
    assert map_period(date(2026, 9, 15), at, "approval_date", [sep2, octo]) is octo


def test_items_hash_order_independent_and_sensitive() -> None:
    cid = uuid.uuid4()
    a, b = _item(cid, "1.00"), _item(cid, "2.00")
    assert earnings.items_hash([a, b]) == earnings.items_hash([b, a])
    assert earnings.items_hash([a]) != earnings.items_hash([a, b])
    assert len(earnings.canonical_hash({"x": D("1.10"), "d": date(2026, 1, 1)})) == 64


def test_no_statutory_computation_in_payroll_package() -> None:
    root = Path(__file__).resolve().parents[3] / "app" / "services" / "payroll"
    banned = {"pf_rate", "esi_rate", "tds_rate", *earnings.PLACEHOLDERS}
    for path in root.glob("*.py"):
        src = path.read_text(encoding="utf-8")
        for name in ("pf_rate", "esi_rate", "tds_rate"):
            assert name not in src, (path.name, name)
        for node in ast.walk(ast.parse(src)):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                name = t.id if isinstance(t, ast.Name) else t.attr if isinstance(t, ast.Attribute) else None
                assert name not in banned, (path.name, name)


@pytest.mark.parametrize(
    ("kind", "bucket"),
    [
        ("expense_line", "reimbursement"),
        ("adjustment_entry", "adjustment"),
        ("rate_adjustment", "rate_adjustment"),
        ("timesheet_entry", "earning"),
        ("monthly_base", "base"),
    ],
)
def test_buckets(kind: str, bucket: str) -> None:
    assert earnings.bucket_for(kind) == bucket
