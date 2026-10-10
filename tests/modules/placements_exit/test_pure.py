"""Pure Epic 31 logic: conversion fee (table + hypothesis), shortfall, early attrition, bench eligibility,
transition tables, working days and the import boundary (AD-32)."""

import ast
import pathlib
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.errors import ConflictError
from app.services.placements import conversion_fee, extension_checks, extensions, offboarding


@pytest.mark.parametrize(
    ("rule", "comp", "expected"),
    [
        ({"kind": "pct_of_ctc", "value": "8.33"}, "1200000.00", Decimal("99960.00")),
        ({"kind": "percent_annual_comp", "value": "8.33"}, "1200000.00", Decimal("99960.00")),
        ({"kind": "fixed", "value": "50000"}, "1200000.00", Decimal("50000.00")),
        ({"kind": "none", "value": None}, "1200000.00", None),
        (None, "1200000.00", None),
        ({"kind": "pct_of_ctc", "value": "10", "waive_after_months": 6}, "100.00", None),
    ],
)
def test_fee_table(rule: dict | None, comp: str, expected: Decimal | None) -> None:
    months = 7 if rule and rule.get("waive_after_months") else None
    assert conversion_fee.compute(rule, Decimal(comp), months_served=months) == expected


def test_waiver_not_reached_still_charges() -> None:
    rule = {"kind": "fixed", "value": "100", "waive_after_months": 6}
    assert conversion_fee.compute(rule, Decimal(0), months_served=5) == Decimal("100.00")


@given(
    comp=st.decimals(min_value=0, max_value=10**9, places=2, allow_nan=False, allow_infinity=False),
    pct=st.decimals(min_value=0, max_value=100, places=2, allow_nan=False, allow_infinity=False),
)
def test_fee_pct_property(comp: Decimal, pct: Decimal) -> None:
    fee = conversion_fee.compute({"kind": "pct_of_ctc", "value": str(pct)}, comp)
    assert fee is not None
    assert fee == fee.quantize(Decimal("0.01"))
    assert Decimal(0) <= fee <= comp + Decimal("0.01")
    assert abs(fee - comp * pct / 100) <= Decimal("0.005")


def test_floats_refused() -> None:
    with pytest.raises(TypeError):
        conversion_fee.compute({"kind": "fixed", "value": 1.5}, Decimal(1))


def test_months_between() -> None:
    assert conversion_fee.months_between(date(2026, 1, 31), date(2026, 2, 28)) == 0
    assert conversion_fee.months_between(date(2026, 1, 15), date(2026, 7, 15)) == 6
    assert conversion_fee.months_between(None, date(2026, 7, 15)) == 0


@pytest.mark.parametrize(
    ("npd", "notice", "lwd", "expected"),
    [
        (30, date(2026, 10, 1), date(2026, 10, 11), 20),
        (30, date(2026, 10, 1), date(2026, 11, 5), 0),
        (None, date(2026, 10, 1), date(2026, 10, 2), None),
    ],
)
def test_notice_shortfall(npd: int | None, notice: date, lwd: date, expected: int | None) -> None:
    assert offboarding.notice_shortfall(npd, notice, lwd) == expected


@pytest.mark.parametrize(("days", "early"), [(29, True), (30, False), (31, False)])
def test_early_attrition_boundary(days: int, early: bool) -> None:
    start = date(2026, 1, 1)
    assert offboarding.is_early_attrition(start, date.fromordinal(start.toordinal() + days), 30) is early


@pytest.mark.parametrize(
    ("worker_type", "reason", "eligible"),
    [
        ("employee", "end_of_term", True),
        ("employee", "client_termination", True),
        ("employee", "converted", False),
        ("employee", "misconduct", False),
        ("employee", "worker_resignation", False),
        ("contractor", "end_of_term", False),
        ("vendor_contractor", "end_of_term", False),
    ],
)
def test_bench_eligibility(worker_type: str, reason: str, eligible: bool) -> None:
    excluded = ["converted", "misconduct", "worker_resignation"]
    assert offboarding.bench_eligible(worker_type, reason, excluded) is eligible


@pytest.mark.parametrize("actor", ["ai", "workflow", "system", "api_key"])
def test_confirm_and_close_are_human_only(actor: str) -> None:
    with pytest.raises(ConflictError):
        extensions.check_transition("pending_client_confirmation", "confirmed", actor)
    with pytest.raises(ConflictError):
        offboarding.check_transition("ready_to_close", "closed", actor)


@pytest.mark.parametrize("actor", ["user", "client_user", "secure_link", "recorded_by_user"])
def test_confirm_human_actors(actor: str) -> None:
    extensions.check_transition("pending_client_confirmation", "confirmed", actor)


@pytest.mark.parametrize(
    ("frm", "to", "actor", "ok"),
    [
        ("proposed", "pending_approval", "system", True),
        ("proposed", "pending_client_confirmation", "system", True),
        ("pending_approval", "proposed", "system", True),
        ("pending_client_confirmation", "declined", "client_user", True),
        ("pending_client_confirmation", "declined", "user", False),
        ("confirmed", "withdrawn", "user", False),
        ("declined", "confirmed", "user", False),
        ("proposed", "withdrawn", "user", True),
        ("proposed", "confirmed", "user", False),
    ],
)
def test_extension_matrix(frm: str, to: str, actor: str, ok: bool) -> None:
    if ok:
        extensions.check_transition(frm, to, actor)
    else:
        with pytest.raises(ConflictError):
            extensions.check_transition(frm, to, actor)


def test_working_days() -> None:
    assert extension_checks.working_days(date(2026, 10, 5), date(2026, 10, 11)) == 5  # Mon..Sun
    assert extension_checks.working_days(date(2026, 10, 10), date(2026, 10, 11)) == 0
    assert extension_checks.estimate_amount(
        Decimal(100), "hour", date(2026, 10, 5), date(2026, 10, 9)
    ) == Decimal(4000)


FORBIDDEN = (
    "app.services.time",
    "app.services.payroll",
    "app.services.billing",
    "app.services.expenses",
    "app.services.documents",
    "app.services.time_service",
    "app.services.billing_service",
    "app.services.payroll_service",
    "app.services.expenses_service",
    "app.services.documents_service",
)
EPIC31 = (
    "end_alerts",
    "extensions",
    "extension_checks",
    "conversion",
    "conversion_fee",
    "offboarding",
    "early_termination",
    "exit_settings",
    "exit_portal",
    "exit_hooks",
    "exit_events",
)


@pytest.mark.parametrize("module", EPIC31)
def test_placements_never_imports_upper_domains(module: str) -> None:
    root = pathlib.Path(__file__).resolve().parents[3] / "app" / "services" / "placements"
    tree = ast.parse((root / f"{module}.py").read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
    bad = [n for n in names if any(n == f or n.startswith(f + ".") for f in FORBIDDEN)]
    assert bad == []
