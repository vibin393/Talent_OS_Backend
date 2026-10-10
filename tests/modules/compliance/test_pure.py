"""Pure matcher and expiry status (Epic 21 A4/A5): table-driven + property checks."""

import uuid
from datetime import date, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.compliance import evaluation, expiry
from app.services.compliance.evaluation import Facts

CLIENT = uuid.uuid4()
OTHER = uuid.uuid4()


def req(items: list[tuple[str, str, bool]], client_id: uuid.UUID | None = None, **match: list[str]) -> dict:
    return {
        "id": uuid.uuid4(),
        "client_id": client_id,
        "active": True,
        "match": match,
        "items": [{"kind": k, "ref": r, "mandatory": m, "label": r.title()} for k, r, m in items],
    }


FACTS = Facts(client_id=CLIENT, industry="Banking", country="India", worker_type="contractor",
              engagement_kind="contract", role_title="Senior Data Engineer")  # fmt: skip


@pytest.mark.parametrize(
    ("requirement", "applies"),
    [
        (req([("credential", "pan_card", True)]), True),
        (req([("credential", "pan_card", True)], client_id=CLIENT), True),
        (req([("credential", "pan_card", True)], client_id=OTHER), False),
        (req([("credential", "pan_card", True)], industries=["banking"]), True),
        (req([("credential", "pan_card", True)], industries=["Retail"]), False),
        (req([("credential", "pan_card", True)], role_keywords=["data"]), True),
        (req([("credential", "pan_card", True)], role_keywords=["nurse"]), False),
        (req([("credential", "pan_card", True)], countries=["India"], worker_types=["contractor"]), True),
        (req([("credential", "pan_card", True)], engagement_kinds=["permanent"]), False),
    ],
)
def test_matching(requirement: dict, applies: bool) -> None:
    assert bool(evaluation.applicable([requirement], FACTS)) is applies


def test_inactive_and_future_requirements_do_not_apply() -> None:
    r = req([("credential", "pan_card", True)])
    assert evaluation.applicable([{**r, "active": False}], FACTS) == []
    future = {**r, "effective_from": date(2030, 1, 1)}
    assert evaluation.applicable([future], Facts(client_id=CLIENT, on_date=date(2026, 1, 1))) == []


def test_union_and_strictness() -> None:
    a = req([("credential", "pan_card", False), ("document", "nda", True)])
    b = req([("credential", "pan_card", True), ("bgv_package", str(uuid.uuid4()), True)])
    out = {(i.kind, i.ref): i for i in evaluation.applicable([a, b], FACTS)}
    assert len(out) == 3
    assert out["credential", "pan_card"].mandatory is True
    assert set(out["credential", "pan_card"].requirement_ids) == {a["id"], b["id"]}


ITEM = st.tuples(
    st.sampled_from(["credential", "document", "policy_ack"]), st.sampled_from(["a", "b", "c"]), st.booleans()
)


@settings(max_examples=60, deadline=None)
@given(st.lists(st.lists(ITEM, min_size=1, max_size=4), min_size=1, max_size=5), st.randoms())
def test_order_independent_and_idempotent(item_lists: list, rnd) -> None:  # noqa: ANN001
    reqs = [req(items) for items in item_lists]
    first = evaluation.applicable(reqs, FACTS)
    shuffled = list(reqs)
    rnd.shuffle(shuffled)
    assert evaluation.applicable(shuffled, FACTS) == first
    assert evaluation.applicable(reqs + reqs, FACTS) == first


def test_expiry_status() -> None:
    today = date(2026, 10, 5)
    offs = [60, 30, 7]
    assert expiry.status_for(None, today, offs) == "none"
    assert expiry.status_for(today + timedelta(days=61), today, offs) == "none"
    assert expiry.status_for(today + timedelta(days=60), today, offs) == "expiring_soon"
    assert expiry.status_for(today, today, offs) == "expiring_soon"
    assert expiry.status_for(today - timedelta(days=1), today, offs) == "expired"


def test_alert_plan_fires_each_offset_once() -> None:
    exp = date(2026, 12, 31)
    offs = [60, 30, 7]
    sent: set[int] = set()
    fired: list[int] = []
    for day in range(90, -3, -1):
        target, mark = expiry.alert_plan(exp, exp - timedelta(days=day), offs, sent)
        sent |= set(mark)
        if target is not None:
            fired.append(target)
    assert fired == [60, 30, 7, expiry.EXPIRED_OFFSET]
    # a credential recorded at 20 days left gets one alert (30), not two
    target, mark = expiry.alert_plan(exp, exp - timedelta(days=20), offs, set())
    assert target == 30 and set(mark) == {60, 30}
