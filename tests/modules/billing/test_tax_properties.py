"""Property tests (hypothesis) of the pure GST / pricing functions (spec A14)."""

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.billing import compute, pricing, tax

KA = tax.SupplierReg("29ABCDE1234F1Z5", "29")
INTRA = tax.determine_supply(KA, tax.Recipient("29AAAAA0000A1Z5", "29"), "registered", None, False)
INTER = tax.determine_supply(KA, tax.Recipient("27AAAAA0000A1Z5", "27"), "registered", None, False)

amounts = st.decimals(
    min_value=Decimal("0.01"),
    max_value=Decimal("99999999.99"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
rates = st.sampled_from(
    [
        Decimal(0),
        Decimal("0.1"),
        Decimal("0.25"),
        Decimal(3),
        Decimal(5),
        Decimal(12),
        Decimal(18),
        Decimal(28),
    ]
)


@settings(max_examples=300, deadline=None)
@given(amounts, rates)
def test_components_bounded_by_rate(taxable: Decimal, rate: Decimal) -> None:
    for decision in (INTRA, INTER):
        for c in tax.compute_line_tax(taxable, rate, decision, "INR"):
            assert c.amount <= taxable * c.rate_pct / 100 + Decimal("0.01")
            assert c.amount >= 0


@settings(max_examples=300, deadline=None)
@given(amounts, rates)
def test_intra_halves_close_to_igst(taxable: Decimal, rate: Decimal) -> None:
    intra = sum(c.amount for c in tax.compute_line_tax(taxable, rate, INTRA, "INR"))
    igst = sum(c.amount for c in tax.compute_line_tax(taxable, rate, INTER, "INR"))
    assert abs(intra - igst) <= Decimal("0.01")


@settings(max_examples=200, deadline=None)
@given(
    st.decimals(min_value=Decimal("1000"), max_value=Decimal("9999999.99"), places=2),
    st.lists(st.integers(min_value=1, max_value=10), min_size=1, max_size=4),
)
def test_full_month_proration_equals_rate(rate: Decimal, days: list[int]) -> None:
    month = sum(days)
    assert sum(pricing.prorate_monthly([(rate, d) for d in days], month, "INR")) == rate


@settings(max_examples=150, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.decimals(min_value=Decimal("0.25"), max_value=Decimal("300"), places=2),
            st.decimals(min_value=Decimal("1"), max_value=Decimal("50000"), places=4),
            st.sampled_from([Decimal(1), Decimal("1.5"), Decimal(2)]),
        ),
        min_size=1,
        max_size=6,
    ),
    st.sampled_from([Decimal(1), Decimal("83.123456")]),
)
def test_recompute_is_byte_identical(rows: list[tuple[Decimal, Decimal, Decimal]], fx: Decimal) -> None:
    lines = [compute.LineInput("time", "x", "998513", q, r, m) for q, r, m in rows]
    a = compute.compute_document(lines, {"998513": Decimal(18)}, INTER, "INR", fx)
    b = compute.compute_document(lines, {"998513": Decimal(18)}, INTER, "INR", fx)
    assert compute.fingerprint(INTER, a, fx) == compute.fingerprint(INTER, b, fx)
    assert compute.canonical_json(
        a.__dict__ if hasattr(a, "__dict__") else [a.document_total]
    ) == compute.canonical_json(b.__dict__ if hasattr(b, "__dict__") else [b.document_total])
    assert a.document_total == a.taxable_total + a.outside_taxable_total + a.charged_tax_total + a.round_off
