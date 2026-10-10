# ruff: noqa: E501, C408
"""Epic 48 (A3): extractors are pure and ignore every fact timestamped after ``as_of``."""

import uuid
from datetime import UTC, date, datetime, timedelta

from app.services.analytics.features import attrition, offer_drop, time_to_fill
from app.services.analytics.features.common import CandidateFact, DemandFact, OfferFact, PlacementFact

AS_OF = datetime(2026, 3, 1, tzinfo=UTC)
BEFORE = AS_OF - timedelta(days=5)
AFTER = AS_OF + timedelta(days=5)


def _demand(**kw: object) -> DemandFact:
    base: dict[str, object] = dict(
        id=uuid.uuid4(), client_id=uuid.uuid4(), status="open", opened_at=AS_OF, positions=1, priority="high",
        employment_type="contract", work_mode="remote", city="pune", min_experience=4.0, bill_rate=1500.0,
        rate_key=("INR", "hour"), skill_ids=(), required_skills=2, primary_user_id=None, filled_at=None, closed_at=None,
    )  # fmt: skip
    base.update(kw)
    return DemandFact(**base)  # type: ignore[arg-type]


def test_time_to_fill_ignores_future_feedback_fills_and_assignments() -> None:
    user = uuid.uuid4()
    d = _demand(primary_user_id=user)
    prior_filled = _demand(client_id=d.client_id, opened_at=BEFORE - timedelta(days=30), filled_at=BEFORE)
    future_filled = _demand(client_id=d.client_id, opened_at=BEFORE - timedelta(days=10), filled_at=AFTER)
    other_open = _demand(opened_at=BEFORE - timedelta(days=3))
    later_assigned = _demand(opened_at=BEFORE - timedelta(days=3))
    ctx = time_to_fill.Context(
        candidate_total=100,
        skill_holders={},
        feedback_delays={d.client_id: [(BEFORE, 2.0), (AFTER, 40.0)]},
        client_demands={d.client_id: [d, prior_filled, future_filled]},
        rate_medians={("INR", "hour"): 1000.0},
        assignments={user: [(other_open.id, BEFORE, None), (later_assigned.id, AFTER, None)]},
        demand_open={
            other_open.id: (other_open.opened_at, None),
            later_assigned.id: (later_assigned.opened_at, None),
        },
    )
    f = time_to_fill.extract(d, AS_OF, ctx)
    assert f["client_feedback_delay"] == 2.0  # the 40-day delay arrives after as_of
    assert f["client_fill_rate"] == 1.0  # the future fill is not yet resolved at as_of
    assert f["recruiter_load"] == 1.0  # the later assignment is not counted
    assert f["rate_vs_median"] == 1.5


def _placement(**kw: object) -> PlacementFact:
    base: dict[str, object] = dict(
        id=uuid.uuid4(), candidate_id=uuid.uuid4(), client_id=uuid.uuid4(), offer_id=uuid.uuid4(), status="pre_joining",
        drop_reason=None, planned_start_date=date(2026, 3, 20), planned_end_date=date(2026, 12, 31),
        actual_start_date=None, end_date=None, created_at=BEFORE, counter_offer_at=AFTER, work_city="pune", status_times={},
    )  # fmt: skip
    base.update(kw)
    return PlacementFact(**base)  # type: ignore[arg-type]


def test_offer_drop_ignores_future_revisions_tasks_bgv_and_counter_offer() -> None:
    p = _placement()
    o = OfferFact(
        p.offer_id,
        p.candidate_id,
        p.client_id,
        "accepted",
        BEFORE,
        (BEFORE - timedelta(days=9), BEFORE - timedelta(days=8), AFTER),
        date(2026, 3, 20),
        1_200_000.0,
        "INR",
        "pune",
    )  # type: ignore[arg-type]
    c = CandidateFact(30, 1_000_000.0, "INR", "chennai")
    s = offer_drop.Subject(o, p, c, tasks=[(BEFORE, BEFORE), (BEFORE, AFTER)], bgv=[(BEFORE, AFTER)])
    f = offer_drop.extract(s, AS_OF)
    assert f["offer_revisions"] == 1.0  # the post-as_of revision is ignored
    assert f["prejoining_completion"] == 0.5  # the second task completes after as_of
    assert f["bgv_pending"] == 1.0  # resolved only after as_of
    assert f["counter_offer"] == 0.0  # recorded after as_of
    assert f["different_city"] == 1.0
    assert round(f["compensation_change"], 6) == 20.0
    assert f["days_since_acceptance"] == 5.0


def test_attrition_ignores_future_rejections_leave_extensions_and_ends() -> None:
    p = _placement(status="active", actual_start_date=date(2025, 6, 1), counter_offer_at=None)
    day = AS_OF.date()
    ctx = attrition.Context(
        rejections={p.id: [day - timedelta(days=10), day + timedelta(days=3)]},
        bench={p.candidate_id: [BEFORE, AFTER]},
        pay={p.id: (900.0, ("INR", "hour"))},
        pay_medians={("INR", "hour"): 1000.0},
        leave={
            p.id: [
                (day - timedelta(days=20), 2.0, "approved", BEFORE, BEFORE),
                (day + timedelta(days=2), 3.0, "approved", BEFORE, AFTER),
            ]
        },
        extensions={p.id: [("confirmed", BEFORE), ("confirmed", AFTER)]},
        client_ends={
            p.client_id: [
                (day - timedelta(days=30), True),
                (day + timedelta(days=30), True),
                (day - timedelta(days=60), False),
            ]
        },
    )
    f = attrition.extract(p, AS_OF, ctx)
    assert f["timesheet_rejections"] == 1.0
    assert f["bench_entries"] == 1.0
    assert f["leave_days"] == 2.0
    assert f["prior_extensions"] == 1.0
    assert f["client_early_end_rate"] == 0.5
    assert round(f["pay_rate_vs_median"], 6) == 0.9


def test_training_as_of_rules() -> None:
    accepted = datetime(2026, 1, 1, tzinfo=UTC)
    assert offer_drop.training_as_of(accepted, accepted + timedelta(days=30)) == accepted + timedelta(days=7)
    assert offer_drop.training_as_of(accepted, accepted + timedelta(days=3)) == accepted + timedelta(days=2)
    p = _placement(actual_start_date=date(2026, 1, 1))
    assert attrition.training_as_of(p, datetime(2026, 6, 1, tzinfo=UTC)) == datetime(2026, 4, 2, tzinfo=UTC)
    assert attrition.training_as_of(p, datetime(2026, 2, 15, tzinfo=UTC)) == datetime(2026, 1, 31, tzinfo=UTC)
    assert attrition.training_as_of(p, datetime(2026, 1, 20, tzinfo=UTC)) is None
