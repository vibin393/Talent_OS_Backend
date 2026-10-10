"""Approval step conditions (pure)."""

from decimal import Decimal

import pytest

from app.services.approvals.conditions import ConditionError, evaluate, validate


def c(combinator: str, *clauses: tuple[str, str, object]) -> dict[str, object]:
    return {combinator: [{"field": f, "op": o, "value": v} for f, o, v in clauses]}


@pytest.mark.parametrize(
    ("condition", "facts", "applies"),
    [
        (None, {}, True),
        (c("all", ("bill_rate", "gte", 2000)), {"bill_rate": Decimal("1500")}, False),
        (c("all", ("bill_rate", "gte", "2000")), {"bill_rate": "2000.00"}, True),
        (c("all", ("bill_rate", "gte", 2000)), {}, True),  # missing fact -> applies
        (c("all", ("bill_rate", "gte", 2000)), {"bill_rate": None}, True),
        (c("all", ("margin_percent", "lt", 15)), {"margin_percent": "14.99"}, True),
        (c("all", ("positions", "gt", 1), ("country", "eq", "in")), {"positions": 2, "country": "IN"}, True),
        (c("all", ("positions", "gt", 1), ("country", "eq", "US")), {"positions": 2, "country": "IN"}, False),
        (c("any", ("positions", "gt", 5), ("country", "eq", "US")), {"positions": 2, "country": "IN"}, False),
        (c("any", ("positions", "gt", 5), ("country", "eq", "US")), {"positions": 6, "country": "IN"}, True),
        (c("any", ("positions", "gt", 5)), {}, True),
        (
            c("all", ("employment_type", "in", ["permanent", "contract"])),
            {"employment_type": "contract"},
            True,
        ),
        (c("all", ("employment_type", "ne", "contract")), {"employment_type": "contract"}, False),
        (c("all", ("pay_rate", "lte", 100)), {"pay_rate": Decimal("100.00")}, True),
    ],
)
def test_evaluate(condition: dict[str, object] | None, facts: dict[str, object], applies: bool) -> None:
    validate(condition)
    assert evaluate(condition, facts) is applies


@pytest.mark.parametrize(
    "condition",
    [
        {"all": []},
        {"none": [{"field": "bill_rate", "op": "eq", "value": 1}]},
        c("all", ("salary", "eq", 1)),
        c("all", ("bill_rate", "between", 1)),
        c("all", ("country", "gt", "IN")),
        c("all", ("bill_rate", "gt", "abc")),
        c("all", ("country", "in", "IN")),
        {"all": [{"field": "bill_rate", "op": "eq"}]},
        {"all": [], "any": []},
    ],
)
def test_invalid_conditions(condition: dict[str, object]) -> None:
    with pytest.raises(ConditionError):
        validate(condition)
