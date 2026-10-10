"""§5.2 transition table (AD-4) and FR-65 guards -- pure, no database."""

import ast
import itertools
from pathlib import Path

import pytest

from app.services.pipeline import margin, stages
from app.services.pipeline.stages import TRANSITIONS, allowed_actors

ACTORS = ("user", "candidate", "system", "workflow", "ai", "api_key")
STAGES = (
    ("applied", "anchored"),
    ("screening", "custom"),
    ("tech", "custom"),
    ("shortlisted", "anchored"),
    ("submitted", "anchored"),
    ("interviewing", "anchored"),
    ("selected", "anchored"),
    ("offered", "anchored"),
    ("hired", "anchored"),
    ("rejected", "anchored"),
    ("withdrawn", "anchored"),
)
NON_TERMINAL = {
    "applied",
    "screening",
    "tech",
    "shortlisted",
    "submitted",
    "interviewing",
    "selected",
    "offered",
}
CUSTOM = {"screening", "tech"}


def expected(src: str | None, dst: str) -> set[str]:
    """Return the spec table, written out independently of ``TRANSITIONS``."""
    if src is None:
        return {"user", "candidate", "workflow", "api_key"} if dst == "applied" else set()
    if src == dst:
        return set()
    if dst == "rejected":
        return {"user"} if src in NON_TERMINAL else set()
    if dst == "withdrawn":
        return {"user", "candidate"} if src in NON_TERMINAL else set()
    if src in ("applied", *CUSTOM) and dst in CUSTOM:
        return {"user", "workflow"}  # Epic 10 additively lets workflow rules move into custom stages
    if src in ("applied", *CUSTOM) and dst in ("applied", "shortlisted"):
        return {"user"}
    table = {
        ("shortlisted", "submitted"): {"system"},
        ("submitted", "interviewing"): {"user", "system", "workflow"},  # + Epic 10 workflow
        ("interviewing", "selected"): {"user"},
        ("selected", "offered"): {"system"},
        ("offered", "hired"): {"system"},
        ("offered", "selected"): {"system"},
        ("rejected", "applied"): {"user"},
        ("withdrawn", "applied"): {"user"},
    }
    return table.get((src, dst), set())


MATRIX = [
    (src, dst, actor)
    for (src, dst), actor in itertools.product(
        itertools.product([None, *(k for k, _ in STAGES)], [k for k, _ in STAGES]), ACTORS
    )
]
KIND = dict(STAGES)


@pytest.mark.parametrize(("src", "dst", "actor"), MATRIX)
def test_transition_matrix(src: str | None, dst: str, actor: str) -> None:
    allowed = allowed_actors(src, KIND.get(src) if src else None, dst, KIND[dst]) or frozenset()
    assert (actor in allowed) == (actor in expected(src, dst)), (src, dst, actor)


def test_rejected_is_user_only_and_ai_never_allowed() -> None:
    assert TRANSITIONS[stages.NT, "rejected"] == frozenset({"user"})
    assert all("ai" not in actors for actors in TRANSITIONS.values())
    assert all("workflow" not in actors for (_, dst), actors in TRANSITIONS.items() if dst == "rejected")


def test_ai_task_modules_never_import_human_only_functions() -> None:
    """FR-65 import graph: the AI summary task and module import no transition / outcome function."""
    root = Path(__file__).resolve().parents[3] / "app"
    forbidden_names = {
        "transition_application",
        "set_outcome",
        "_apply_outcome",
        "send_submission",
        "confirm_rejection",
    }
    forbidden_modules = {
        "app.services.pipeline.applications",
        "app.services.pipeline.screening",
        "app.services.pipeline.submissions",
        "app.services.pipeline.feedback",
    }
    for rel in ("tasks/pipeline_ai.py", "services/pipeline/ai_summary.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert module not in forbidden_modules, (rel, module)
                for alias in node.names:
                    assert alias.name not in forbidden_names, (rel, alias.name)
                    assert f"{module}.{alias.name}" not in forbidden_modules, (rel, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in forbidden_modules, (rel, alias.name)
            elif isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                assert name not in forbidden_names, (rel, name)


# ================================================================ margin (pure, table-driven)
MARGIN_CASES = [
    # pay, bill, burden, pay_unit, bill_unit, rate_unit, amount, pct, below(min 15)
    ("900", "1500", "18", None, None, "hour", "438.00", "29.20", False),
    ("1300", "1500", "18", None, None, "hour", "-34.00", "-2.27", True),
    ("1000", "1500", "0", None, None, "hour", "500.00", "33.33", False),
    ("7200", "1500", "18", "day", None, "hour", "438.00", "29.20", False),
    ("100", "20000", "0", "hour", "month", "month", "3200.00", "16.00", False),
    ("1234.56", "1500.00", "10", None, None, "day", "141.98", "9.47", True),
    ("0", "1", "0", None, None, "year", "1.00", "100.00", False),
]


@pytest.mark.parametrize(
    ("pay", "bill", "burden", "pu", "bu", "unit", "amount", "pct", "below"), MARGIN_CASES
)
def test_margin(
    pay: str,
    bill: str,
    burden: str,
    pu: str | None,
    bu: str | None,
    unit: str,
    amount: str,
    pct: str,
    below: bool,
) -> None:
    from decimal import Decimal

    m = margin.compute(
        pay=Decimal(pay),
        bill=Decimal(bill),
        pay_currency="INR",
        bill_currency="INR",
        rate_unit=unit,
        burden_pct=Decimal(burden),
        min_pct=Decimal(15),
        pay_unit=pu,
        bill_unit=bu,
    )
    assert (str(m.amount), str(m.pct), m.below_min) == (amount, pct, below)


def test_margin_currency_mismatch() -> None:
    from decimal import Decimal

    from app.core.errors import ValidationFailed

    for pay_c, bill_c, demand_c in (("USD", "INR", None), ("INR", "INR", "USD")):
        with pytest.raises(ValidationFailed) as exc:
            margin.compute(
                pay=Decimal(1),
                bill=Decimal(2),
                pay_currency=pay_c,
                bill_currency=bill_c,
                rate_unit="hour",
                burden_pct=Decimal(0),
                min_pct=Decimal(15),
                demand_currency=demand_c,
            )
        assert exc.value.code == "currency_mismatch"


def test_normalize_units() -> None:
    from decimal import Decimal

    assert margin.normalize(Decimal(1600), "day", "hour") == Decimal(200)
    assert margin.normalize(Decimal(1), "hour", "year") == Decimal(2016)


def test_resume_scrubber() -> None:
    from app.services.pipeline.resume_pdf import scrub

    text = scrub(
        "Mail priya.raman@example.com, call +91 98765 43210 / 9876543210 / 98765-43210, "
        "see https://github.com/priya and linkedin.com/in/priya-r, www.priya.dev, @priya_r. "
        "Built ASP.NET apps."
    )
    for leaked in ("@example.com", "98765", "9876543210", "github.com", "linkedin", "priya.dev", "@priya_r"):
        assert leaked not in text
    assert "ASP.NET" in text


def test_token_paths_are_redacted_in_logs() -> None:
    from app.core.logger import redact

    token = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd"
    line = redact(f"GET /api/v1/public/links/{token} 200 and https://app.example.com/l/{token}")
    assert token not in line
