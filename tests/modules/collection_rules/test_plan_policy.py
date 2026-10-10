# ruff: noqa: E501
"""Epic 63 pure units: the collection plan (tables + hypothesis: disputed or held documents are never planned) and the
deterministic dunning policy check."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.errors import ValidationFailed
from app.services.collection_rules import policy_check
from app.services.collection_rules.plan import DocFacts, PlanFacts, StepFacts, collection_plan, in_quiet_hours

TODAY = date(2026, 10, 12)
NOW = datetime(2026, 10, 12, 10, 30, tzinfo=UTC)
TPL = uuid.uuid4()
D = Decimal
STEPS = (
    StepFacts(uuid.uuid4(), 1, -3, "courteous", "billing_contacts", TPL, allow_auto=True),
    StepFacts(uuid.uuid4(), 2, 7, "friendly", "billing_contacts", TPL),
    StepFacts(uuid.uuid4(), 3, 21, "firm", "billing_contacts", TPL),
    StepFacts(uuid.uuid4(), 4, 30, "escalation", "escalation_contact", None, create_task=True),
)


def doc(days_overdue: int, open_amount: str = "1000.00", disputed: str = "0", **kw: object) -> DocFacts:
    return DocFacts(
        kind="invoice", id=uuid.uuid4(), number=f"INV-{uuid.uuid4().hex[:4]}", due_date=TODAY - timedelta(days=days_overdue),
        currency="INR", open_amount=D(open_amount), disputed_amount=D(disputed), **kw,  # type: ignore[arg-type]
    )


def facts(*docs: DocFacts, **kw: object) -> PlanFacts:
    kw.setdefault("now_local", NOW)
    return PlanFacts(client_id=uuid.uuid4(), today=TODAY, docs=docs, steps=STEPS, **kw)  # type: ignore[arg-type]


def test_steps_tones_and_send_class() -> None:
    pre, d7, d21, d30, early = doc(-2), doc(8), doc(22), doc(31), doc(-10)
    plan = collection_plan(facts(pre, d7, d21, d30, early, max_per_week=7))
    by_step = {i.step.position: i for i in plan.items}
    assert {k: [d.doc.id for d in v.docs] for k, v in by_step.items()} == {1: [pre.id], 2: [d7.id], 3: [d21.id], 4: [d30.id]}
    assert by_step[1].send_class == "pre_due_auto_eligible"
    assert {by_step[k].send_class for k in (2, 3, 4)} == {"needs_approval"}
    assert [(e.document_id, e.reason) for e in plan.excluded] == [(early.id, "no_step_due")]


def test_grouping_one_message_per_step() -> None:
    a, b = doc(8), doc(10)
    plan = collection_plan(facts(a, b))
    assert len(plan.items) == 1 and plan.items[0].total_remindable == D("2000.00")


def test_disputes_holds_dnr_and_done() -> None:
    full = doc(8, disputed="1000.00")
    partial = doc(-1, disputed="200.00")
    held, taken = doc(8), doc(9)
    plan = collection_plan(facts(full, partial, held, taken, doc_holds={held.id: "paused", taken.id: "takeover"}, max_per_week=7))
    reasons = {e.document_id: e.reason for e in plan.excluded}
    assert reasons == {full.id: "disputed", held.id: "held", taken.id: "takeover"}
    item = plan.items[0]
    assert item.docs[0].remindable == D("800.00") and item.partially_disputed and item.send_class == "needs_approval"
    assert collection_plan(facts(doc(8), do_not_remind=True)).items == ()
    assert collection_plan(facts(doc(8), client_hold="paused")).items == ()
    done = doc(8)
    assert collection_plan(facts(done, done=frozenset({(done.id, STEPS[1].id)}))).excluded[0].reason == "no_step_due"


def test_quiet_hours_weekly_cap_recent_contact() -> None:
    late = datetime(2026, 10, 12, 19, 0, tzinfo=UTC)
    assert collection_plan(facts(doc(8), now_local=late)).excluded[0].reason == "quiet_hours"
    capped = collection_plan(facts(doc(8), doc(-2), sent_last_7_days=1, max_per_week=2))
    assert len(capped.items) == 1 and [e.reason for e in capped.excluded] == ["weekly_cap"]
    d = doc(8)
    assert collection_plan(facts(d, recently_contacted=frozenset({d.id}))).excluded[0].reason == "recently_contacted"
    assert in_quiet_hours(time(23, 0), time(18, 0), time(9, 0)) and not in_quiet_hours(time(10, 30), time(18, 0), time(9, 0))


money = st.integers(min_value=0, max_value=100000).map(lambda c: Decimal(c) / 100)


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(
    rows=st.lists(
        st.tuples(st.integers(-30, 120), money, money, st.sampled_from([None, "paused", "takeover"])), min_size=1, max_size=8
    ),
    client_hold=st.booleans(),
)
def test_disputed_or_held_documents_are_never_planned(rows: list[tuple[int, Decimal, Decimal, str | None]], client_hold: bool) -> None:
    docs, holds = [], {}
    for days, open_amount, disputed, hold in rows:
        d = doc(days, str(open_amount), str(disputed))
        docs.append(d)
        if hold:
            holds[d.id] = hold
    plan = collection_plan(facts(*docs, doc_holds=holds, client_hold="paused" if client_hold else None, max_per_week=7))
    planned = [pd for i in plan.items for pd in i.docs]
    for pd in planned:
        assert pd.doc.id not in holds and not client_hold
        assert pd.doc.disputed_amount < pd.doc.open_amount and pd.remindable > 0
    assert len({pd.doc.id for pd in planned}) + len(plan.excluded) == len(docs)


# ================================================================ policy check
AMOUNTS = {D("1000.00"), D("800.00"), D("1800.00")}
DUE = {date(2026, 10, 2)}


@pytest.mark.parametrize(
    ("text", "violation"),
    [
        ("We will initiate legal action if unpaid.", "forbidden:legal_action"),
        ("Our lawyers will contact you.", "forbidden:lawyer"),
        ("We can offer a 5% discount for early payment.", "forbidden:discount"),
        ("As a goodwill gesture we will waive the late fee.", "forbidden:waiver"),
        ("We could agree a one-time settlement.", "forbidden:settlement"),
        ("We may report this to CIBIL.", "forbidden:credit_bureau"),
        ("We will stop services next week.", "forbidden:stop_services"),
        ("The balance is actually ₹1.", "amount:1.00"),
        ("Please pay INR 5,000.00 by Friday.", "amount:5000.00"),
        ("Invoice INV-1 was due on 2026-10-05.", "date:2026-10-05"),
        ("Pay at https://evil.test/pay", "link:not_allowed"),
    ],
)
def test_policy_violations(text: str, violation: str) -> None:
    got = policy_check.check(text, amounts=AMOUNTS, due_dates=DUE, link_prefixes=("https://app.talentos.test",))
    assert violation in got, got


def test_policy_allows_plan_amounts_dates_and_portal_links() -> None:
    text = (
        "Dear Asha, invoice INV-1 (due 2026-10-02, 2 Oct 2026) has INR 1,000.00 outstanding; the total is ₹1,800.00. "
        "Could you share a payment date? Statement: https://app.talentos.test/portal/statement. Please settle at your convenience."
    )
    assert policy_check.check(text, amounts=AMOUNTS, due_dates=DUE, link_prefixes=("https://app.talentos.test",)) == []
    with pytest.raises(ValidationFailed) as exc:
        policy_check.enforce("We will take legal action.", amounts=AMOUNTS, due_dates=DUE)
    assert exc.value.code == "dunning_policy_violation"
