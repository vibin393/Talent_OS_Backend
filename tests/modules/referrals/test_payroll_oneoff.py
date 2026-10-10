# ruff: noqa: E501
"""Epic 42 / coordinator decision 8: the one-off ``referral_reward`` payroll kind (pure). Existing kinds keep their
bucket (the unchanged P2 payroll suite proves the files are byte-identical without one-off items); a one-off item
lands in ``adjustments_total`` and the gross earnings inputs at its fixed amount, with no hours."""

import uuid
from datetime import date
from decimal import Decimal

from app.services.payroll import earnings
from app.services.postings import posting_sources


def test_buckets_unchanged_and_one_off_is_adjustment() -> None:
    assert frozenset({"referral_reward"}) == earnings.ONE_OFF_KINDS
    assert earnings.bucket_for("referral_reward") == "adjustment"
    assert earnings.bucket_for("adjustment_entry") == "adjustment"
    assert earnings.bucket_for("expense_line") == "reimbursement"
    assert earnings.bucket_for("rate_adjustment") == "rate_adjustment"
    assert earnings.bucket_for("monthly_base") == "base"
    assert earnings.bucket_for("timesheet_entry") == "earning"


def test_one_off_item_row_totals() -> None:
    cid = uuid.uuid4()
    item = earnings.PricedItem(
        "referral_reward", uuid.uuid4(), uuid.uuid4(), cid, "INR", "referral_reward", date(2026, 10, 6),
        Decimal(1), None, None, Decimal("25000.00"),
    )  # fmt: skip
    (row,) = earnings.build_rows([item])
    assert row.adjustments_total == Decimal("25000.00")
    assert row.gross_earnings_inputs == Decimal("25000.00")
    assert all(v == 0 for v in row.hours.values()) and all(v == 0 for v in row.earnings.values())
    assert item.postable


def test_posting_source_registered_payable_only() -> None:
    from app.core.hooks import autodiscover

    autodiscover("app.services")
    src = posting_sources()["referral_reward"]
    assert src.ledgers == frozenset({"payable"}) and src.loader is not None
