# ruff: noqa: E501
"""GST golden table (spec A14, G1..G18 pure rows) and pricing / words rows. Supplier GSTIN state 29 (Karnataka)
unless noted; 18 % unless noted; INR."""

from decimal import Decimal

import pytest

from app.services.billing import compute, pricing, tax, words
from app.services.billing.compute import LineInput

KA = tax.SupplierReg("29ABCDE1234F1Z5", "29")
D = Decimal


def decide(
    state: str | None = "29",
    gstin: str | None = "29AAAAA0000A1Z5",
    treatment: str = "registered",
    *,
    lut: tax.Lut | None = None,
    rc: bool = False,
    supplier: tax.SupplierReg | None = KA,
    country: str = "IN",
) -> tax.SupplyDecision:
    return tax.determine_supply(supplier, tax.Recipient(gstin, state, country), treatment, lut, rc)  # type: ignore[arg-type]


def doc(
    lines: list[LineInput],
    decision: tax.SupplyDecision,
    currency: str = "INR",
    fx: Decimal = Decimal(1),
    rate: Decimal = Decimal(18),
    mode: str = "none",
) -> compute.DocCalc:
    return compute.compute_document(
        lines, {li.sac_code: rate for li in lines}, decision, currency, fx, round_off_mode=mode
    )


def line(amount: str, *, pure: bool = False, sac: str = "998513") -> LineInput:
    return LineInput("time", "x", sac, D(1), D(amount), pure_agent=pure)


def comps(calc: compute.DocCalc, i: int = 0) -> dict[str, tuple[Decimal, Decimal, bool]]:
    return {c.component: (c.rate_pct, c.amount, c.charged) for c in calc.lines[i].components}


def test_g1_intra_state() -> None:
    d = decide()
    c = doc([line("100000.00")], d)
    assert d.supply_type == "b2b" and d.intra_state
    assert comps(c) == {"cgst": (D(9), D("9000.00"), True), "sgst": (D(9), D("9000.00"), True)}
    assert c.document_total == D("118000.00")


def test_g2_inter_state() -> None:
    c = doc([line("100000.00")], decide("27", "27AAAAA0000A1Z5"))
    assert comps(c) == {"igst": (D(18), D("18000.00"), True)}
    assert c.document_total == D("118000.00")


def test_g3_ut_without_legislature() -> None:
    d = decide("04", "04AAAAA0000A1Z5", supplier=tax.SupplierReg("04ABCDE1234F1Z5", "04"))
    assert d.use_utgst
    assert set(comps(doc([line("100000.00")], d))) == {"cgst", "utgst"}


def test_g4_five_percent_odd_paise() -> None:
    assert comps(doc([line("100.10")], decide(), rate=D(5)))["cgst"][1] == D("2.50")
    assert comps(doc([line("100.10")], decide("27"), rate=D(5)))["igst"][1] == D("5.01")


def test_g5_sez_same_state_with_lut() -> None:
    d = decide("29", treatment="sez", lut=tax.Lut("AD290326000001", "26-27"))
    assert (d.supply_type, d.intra_state, d.lut_ref) == ("sezwop", False, "AD290326000001")
    assert comps(doc([line("100000.00")], d)) == {"igst": (D(0), D("0.00"), True)}


def test_g6_sez_with_payment() -> None:
    d = decide("29", treatment="sez")
    assert d.supply_type == "sezwp" and not d.intra_state
    assert comps(doc([line("100000.00")], d))["igst"][1] == D("18000.00")


def test_g7_export_under_lut_usd() -> None:
    d = decide(None, None, "export", lut=tax.Lut("AD1", "26-27"), country="US")
    c = doc([line("10000.00")], d, "USD", D("83.250000"))
    assert (d.pos_state_code, d.supply_type) == ("96", "expwop")
    assert c.charged_tax_total == D("0.00") and c.inr_taxable_total == D("832500.00")


def test_g8_export_with_payment_multi_line_inr_per_line() -> None:
    d = decide(None, None, "export", country="US")
    c = doc([line("333.33"), line("333.33")], d, "USD", D("83.123456"))
    assert comps(c)["igst"][1] == D("60.00")
    assert c.lines[0].inr_taxable == D("27707.54")
    assert c.lines[0].inr_components["igst"] == D("4987.36")  # not 4987.41 (= 60.00 x fx)
    assert c.document_total == D("786.66")
    assert c.inr_taxable_total == D("55415.08") and c.inr_tax_total == D("9974.72")


def test_g9_export_lut_missing_warns() -> None:
    d = decide(None, None, "export", lut=None, country="US")
    assert d.supply_type == "expwp" and "lut_missing" in d.warnings
    assert comps(doc([line("100.00")], d))["igst"][1] == D("18.00")


def test_g10_reverse_charge() -> None:
    d = decide(rc=True)
    c = doc([line("50000.00")], d)
    assert comps(c) == {"cgst": (D(9), D("4500.00"), False), "sgst": (D(9), D("4500.00"), False)}
    assert c.document_total == D("50000.00") and c.rcm_tax_total == D("9000.00")


def test_g11_half_up() -> None:
    assert comps(doc([line("50.50")], decide()))["cgst"][1] == D("4.55")


def test_g12_per_line_rounding() -> None:
    c = doc([line("10.05")] * 3, decide())
    assert sum(comps(c, i)["cgst"][1] for i in range(3)) == D("2.70")


def test_g13_overtime_effective_rate() -> None:
    li = LineInput("time", "OT", "998513", D("7.25"), D("1234.5678"), D("1.5"))
    c = doc([li], decide())
    assert c.lines[0].effective_rate == D("1851.8517")
    assert c.lines[0].taxable == D("13425.92")
    assert comps(c)["cgst"][1] == D("1208.33")


def test_g14_round_off() -> None:
    c = doc([line("1000.55")], decide("27"), mode="nearest_rupee")
    assert comps(c)["igst"][1] == D("180.10")
    assert c.round_off == D("0.35") and c.document_total == D("1181.00")


def test_g15_unregistered_b2c() -> None:
    d = decide("27", None, "unregistered")
    assert d.supply_type == "b2c" and not d.intra_state
    assert not tax.einvoice_applicable(d.supply_type, True)


def test_g16_registered_without_state() -> None:
    with pytest.raises(tax.TaxDecisionError) as exc:
        decide(None)
    assert exc.value.code == "place_of_supply_unresolved"


def test_g17_pure_agent() -> None:
    c = doc([line("2000.00", pure=True), line("10000.00")], decide())
    assert c.lines[0].components == () and c.charged_tax_total == D("1800.00")
    assert c.document_total == D("13800.00") and c.outside_taxable_total == D("2000.00")


def test_g18_non_indian_entity() -> None:
    d = decide(supplier=None)
    assert d.supply_type == "outside_gst"
    assert doc([line("100.00")], d).lines[0].components == ()


def test_unregistered_without_address_falls_back_to_supplier_state() -> None:
    d = decide(None, None, "unregistered")
    assert d.pos_state_code == "29" and d.intra_state


def test_negative_line_refused() -> None:
    with pytest.raises(pricing.PricingError):
        pricing.taxable(D("10.00"), D("10.01"), "INR")


def test_prorate_full_month_exact() -> None:
    assert sum(pricing.prorate_monthly([(D("100000.00"), 7), (D("100000.00"), 15)], 22, "INR")) == D(
        "100000.00"
    )


def test_words_indian_system() -> None:
    assert (
        words.integer_words(12345678)
        == "One Crore Twenty Three Lakh Forty Five Thousand Six Hundred Seventy Eight"
    )
    assert (
        words.amount_in_words(D("118000.50"), "INR")
        == "Indian Rupees One Lakh Eighteen Thousand and Fifty Paise Only"
    )
    assert (
        words.amount_in_words(D("786.66"), "USD")
        == "US Dollars Seven Hundred Eighty Six and Sixty Six Cents Only"
    )
    assert words.indian_grouping(D("1234567.50")) == "12,34,567.50"
