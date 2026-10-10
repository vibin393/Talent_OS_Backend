# ruff: noqa: E501
"""Epic 56 pure units: deterministic ranking (AD-71) and the outreach draft checks (FR-287)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.errors import ValidationFailed
from app.models.matching import DISMISS_REASONS as P1_DISMISS_REASONS
from app.models.sourcing_agent import DISMISS_REASONS
from app.schemas.sourcing_agent import ChannelState
from app.services.sourcing_agent import ranking
from app.services.sourcing_agent.contactability import best_channel
from app.services.sourcing_agent.outreach import (
    OPT_OUT_LINE,
    PayRange,
    pay_amounts,
    protected_terms,
    validate_draft,
)

NOW = datetime(2026, 10, 8, tzinfo=UTC)


def _row(score: float | None, days: int | None, cid: uuid.UUID | None = None) -> dict[str, object]:
    return {
        "candidate_id": cid or uuid.uuid4(),
        "overall": Decimal(str(score)) if score is not None else None,
        "last_activity_at": NOW - timedelta(days=days) if days is not None else None,
    }


rows_strategy = st.lists(
    st.tuples(st.one_of(st.none(), st.integers(0, 100)), st.one_of(st.none(), st.integers(0, 400)), st.uuids()),
    min_size=0, max_size=12, unique_by=lambda t: t[2],
)  # fmt: skip


@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None, max_examples=50)
@given(rows_strategy, st.randoms())
def test_rank_is_permutation_invariant(
    data: list[tuple[int | None, int | None, uuid.UUID]], rnd: object
) -> None:
    items = [_row(s, d, c) for s, d, c in data]
    shuffled = list(items)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    assert [r["candidate_id"] for r in ranking.rank(items)] == [
        r["candidate_id"] for r in ranking.rank(shuffled)
    ]


def test_rank_order_and_ties() -> None:
    a, b, c, d, e = (uuid.UUID(int=i) for i in range(1, 6))
    items = [_row(80, 10, b), _row(80, 2, c), _row(90, 50, a), _row(None, 0, d), _row(80, 2, e)]
    # overall desc; equal overall -> later activity first; then candidate id; unscored last
    assert [r["candidate_id"] for r in ranking.rank(items)] == [a, c, e, b, d]


def test_select_top_keeps_dismissed_above_the_cut() -> None:
    items = ranking.rank([_row(90 - i, 1) for i in range(6)])
    dismissed = {items[1]["candidate_id"]}
    top = ranking.select_top(items, 3, lambda r: r["candidate_id"] in dismissed)
    assert len(top) == 4 and top[1]["candidate_id"] in dismissed


def test_dismiss_reasons_match_p1() -> None:
    assert tuple(DISMISS_REASONS) == tuple(P1_DISMISS_REASONS)


def test_best_channel() -> None:
    ok, no = ChannelState(ok=True), ChannelState(ok=False, reason="channel_opt_out")
    assert best_channel({"email": ok, "sms": ok, "whatsapp": ok}, "sms") == "sms"
    assert best_channel({"email": no, "sms": ok, "whatsapp": ok}, "email") == "whatsapp"
    assert best_channel({"email": no, "sms": no, "whatsapp": no}, "email") is None


# ---------------------------------------------------------------- drafts
PAY = PayRange(Decimal(2_000_000), Decimal(3_000_000))


def test_pay_amounts_parse_indian_formats() -> None:
    assert pay_amounts("₹40 LPA") == [Decimal(4_000_000)]
    assert pay_amounts("up to 25 lakhs per annum") == [Decimal(2_500_000)]
    assert pay_amounts("INR 28,00,000 fixed") == [Decimal(2_800_000)]
    assert pay_amounts("no numbers here, 5 years experience") == []


def test_validate_draft_rejects_pay_outside_the_range() -> None:
    with pytest.raises(ValidationFailed) as exc:
        validate_draft("The role pays ₹40 LPA.", PAY)
    assert exc.value.code == "comp_outside_range"
    with pytest.raises(ValidationFailed):
        validate_draft("The role pays ₹40 LPA.", PayRange(None, None))  # nothing published
    out = validate_draft("The role pays up to ₹28 LPA.", PAY)
    assert OPT_OUT_LINE in out.text and "opt_out_added" in out.flags


def test_validate_draft_rejects_protected_terms() -> None:
    assert protected_terms("Looking for a young female engineer") == ["female"]
    with pytest.raises(ValidationFailed) as exc:
        validate_draft("Ideal for a married candidate under 30 years old", PAY)
    assert exc.value.code == "protected_term"
    assert protected_terms("Manage the data platform and engage stakeholders") == []


def test_validate_draft_keeps_existing_opt_out() -> None:
    out = validate_draft("Hello. Reply STOP to unsubscribe.", PAY)
    assert out.flags == () and out.text.count("unsubscribe") == 1
