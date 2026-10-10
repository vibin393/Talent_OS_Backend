"""Epic 62 pure units: the chase cadence plan (tables + hypothesis) and the BGV follow-up cadence."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.chasing.bgv_follow_ups import MAX_FOLLOW_UPS, add_working_days, next_due
from app.services.chasing.plan import ChaseFacts, ItemFact, due_chases

TODAY = date(2026, 10, 12)  # a Monday
NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
CID = uuid.uuid4()


def item(**kw: object) -> ItemFact:
    base: dict[str, object] = {
        "item_kind": "checklist_item", "item_ref": uuid.uuid4(), "label": "PAN card", "status": "missing",
        "placement_id": uuid.uuid4(), "start_date": TODAY + timedelta(days=7),
    }  # fmt: skip
    base.update(kw)
    return ItemFact(**base)  # type: ignore[arg-type]


def facts(*items: ItemFact, **kw: object) -> ChaseFacts:
    starts = tuple(i.start_date for i in items if i.start_date)
    return ChaseFacts(candidate_id=CID, now=NOW, items=items, start_dates=starts, **kw)  # type: ignore[arg-type]


def test_start_in_7_days_missing_is_due() -> None:
    plan = due_chases(facts(item(), portal_last_seen=NOW), TODAY)
    assert [(i.offset_days, i.due_basis) for i in plan.items] == [(7, "start_date")]
    assert not plan.escalate


def test_day_6_is_not_a_cadence_day() -> None:
    assert (
        due_chases(facts(item(start_date=TODAY + timedelta(days=6)), portal_last_seen=NOW), TODAY).items == ()
    )


def test_recent_p2_contact_skips() -> None:
    plan = due_chases(facts(item(), last_contact=NOW - timedelta(hours=10), portal_last_seen=NOW), TODAY)
    assert plan.items == () and plan.skipped[0][1] == "recently_contacted"


def test_uploaded_item_stops_chasing() -> None:
    assert due_chases(facts(item(status="pending"), portal_last_seen=NOW), TODAY).items == ()
    assert due_chases(facts(item(status="met"), portal_last_seen=NOW), TODAY).items == ()


def test_portal_never_opened_escalates() -> None:
    plan = due_chases(facts(item(start_date=TODAY + timedelta(days=5))), TODAY)
    assert plan.escalate and "portal_not_opened" in plan.reasons


def test_deadline_close_escalates() -> None:
    plan = due_chases(facts(item(start_date=TODAY + timedelta(days=1)), portal_last_seen=NOW), TODAY)
    assert plan.items[0].offset_days == 1 and plan.reasons == ("deadline_close",)


def test_credential_expiry_skips_p2_offset_days() -> None:
    cred = item(
        item_kind="credential",
        status="current",
        start_date=None,
        expires_on=TODAY + timedelta(days=14),
        p2_alert_offsets=(60, 30, 7),
    )
    assert [i.offset_days for i in due_chases(facts(cred, portal_last_seen=NOW), TODAY).items] == [14]
    on_p2 = item(
        item_kind="credential",
        status="current",
        start_date=None,
        expires_on=TODAY + timedelta(days=7),
        p2_alert_offsets=(60, 30, 7),
    )
    plan = due_chases(facts(on_p2, portal_last_seen=NOW), TODAY)
    assert plan.items == () and plan.skipped[0][1] == "p2_alert_day"


def test_post_deadline_every_3_days_max_3() -> None:
    late = item(start_date=TODAY - timedelta(days=3), expires_on=None)
    assert due_chases(facts(late, portal_last_seen=NOW), TODAY).items[0].offset_days == -3
    capped = item(
        start_date=TODAY - timedelta(days=12), post_deadline_chases=3, chased_offsets=frozenset({-3, -6, -9})
    )
    assert due_chases(facts(capped, portal_last_seen=NOW), TODAY).items == ()


@given(st.integers(min_value=-20, max_value=30), st.sets(st.integers(min_value=-20, max_value=30)))
@settings(max_examples=200, deadline=None)
def test_never_two_chases_per_item_and_offset(days: int, chased: set[int]) -> None:
    it = item(
        start_date=TODAY + timedelta(days=days),
        chased_offsets=frozenset(chased),
        post_deadline_chases=sum(1 for c in chased if c < 0),
    )
    plan = due_chases(facts(it, portal_last_seen=NOW), TODAY)
    for d in plan.items:
        assert d.offset_days not in chased
        assert d.offset_days in (14, 7, 3, 1) or (d.offset_days < 0 and d.offset_days % 3 == 0)


@given(st.integers(min_value=0, max_value=19))
@settings(max_examples=40, deadline=None)
def test_person_quiet_window(hours: int) -> None:
    plan = due_chases(facts(item(), last_contact=NOW - timedelta(hours=hours), portal_last_seen=NOW), TODAY)
    assert plan.items == ()


def test_bgv_cadence_working_days_and_limit() -> None:
    friday = date(2026, 10, 9)
    assert add_working_days(friday, 1) == date(2026, 10, 12)
    assert next_due(friday, None, 0) == date(2026, 10, 12)
    assert next_due(friday, date(2026, 10, 12), 1) == date(2026, 10, 14)
    assert next_due(friday, date(2026, 10, 12), MAX_FOLLOW_UPS) is None


@pytest.mark.parametrize("count", range(MAX_FOLLOW_UPS))
def test_bgv_next_due_before_limit(count: int) -> None:
    assert next_due(date(2026, 10, 1), date(2026, 10, 5) if count else None, count) is not None
