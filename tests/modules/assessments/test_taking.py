# ruff: noqa: E501
"""Epic 40 A5/A6/A7/A8: invitations, the server-timed attempt (Secure Link), delivery without keys, scoring, review,
results, proctoring, the star (human-only) decision and the section 5.1 transition matrix."""

import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.core.database import tenant_session
from app.core.errors import DomainError
from app.schemas.assessments import DecideIn
from app.schemas.assessments_delivery import FORBIDDEN_DELIVERY_FIELDS, AttemptView, PreStartOut
from app.services.assessments import invitations, lifecycle, taking
from tests.modules.assessments.helpers import (
    ASM,
    INV,
    PUBLIC,
    SESSION,
    answer_for,
    create_item,
    ctx,
    invite,
    invite_ok,
    link_token,
    make_candidate,
    published,
    save,
    shift_attempt,
    simple_assessment,
    soon,
    sql,
    start,
    submit,
)
from tests.modules.demands.helpers import DOrg, h


def _keys(obj: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k))
            out |= _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            out |= _keys(v)
    return out


def _model_fields(model: Any, seen: set[Any] | None = None) -> set[str]:
    seen = seen or set()
    if model in seen:
        return set()
    seen.add(model)
    names: set[str] = set()
    for name, f in model.model_fields.items():
        names.add(name)
        for arg in getattr(f.annotation, "__args__", (f.annotation,)):
            for inner in (*getattr(arg, "__args__", ()), arg):
                if hasattr(inner, "model_fields"):
                    names |= _model_fields(inner, seen)
    return names


def test_delivery_models_never_carry_keys() -> None:
    for model in (PreStartOut, AttemptView):
        assert not (_model_fields(model) & FORBIDDEN_DELIVERY_FIELDS), model


async def _setup(org: DOrg, kinds: tuple[str, ...] = ("single", "short_text", "code_output"), **kw: Any) -> tuple[dict[str, Any], uuid.UUID, dict[str, Any], str]:
    a, _ = await simple_assessment(org, kinds, **kw)
    cand = await make_candidate(org)
    inv = await invite_ok(org, a["id"], cand, gating=True)
    return a, cand, inv, await link_token(org, cand)


async def test_full_flow_review_results_and_human_decision(dorg: DOrg) -> None:
    c = dorg.admin.client
    a, cand, inv, token = await _setup(dorg, ("single", "short_text", "code_output", "code_order", "code_find_bug", "essay"))
    view = await start(c, token)
    session = view["session_id"]
    assert session and view["remaining_seconds"] > 29 * 60 and view["status"] == "in_progress"
    raw = str(view)
    assert not (_keys(view) & FORBIDDEN_DELIVERY_FIELDS), _keys(view) & FORBIDDEN_DELIVERY_FIELDS
    assert "Paris" not in raw and "bug_lines" not in raw
    kinds = {i["position"]: i["kind"] for i in view["items"]}
    answers = []
    for it in view["items"]:
        resp = answer_for(it, it["kind"])
        if it["kind"] == "short_text":
            resp = {"text": "Lutetia"}  # not accepted, no keywords hit -> review
        answers.append({"position": it["position"], "response": resp})
    r = await save(c, token, session, 1, answers)
    assert r.status_code == 200 and r.json()["saved"] == len(answers), r.text
    stale = await save(c, token, session, 1, [{"position": answers[0]["position"], "response": {"choice": "zzz"}}])
    assert stale.status_code == 200 and stale.json()["saved"] == 0  # same client_seq: ignored
    # 3 tab switches
    sig = await c.post(f"{PUBLIC}/{token}/assessment/attempt/signals", json={"signals": [{"kind": "tab_switch"}] * 3}, headers={SESSION: session})
    assert sig.status_code == 200 and sig.json()["accepted"] == 3
    done = await submit(c, token, session)
    assert done.status_code == 200 and done.json()["status"] == "submitted", done.text
    again = await submit(c, token, session)
    assert again.status_code == 409 and again.json()["error"]["code"] == "attempt_already_submitted"
    detail = (await c.get(f"{INV}/{inv['id']}")).json()
    assert detail["status"] == "completed" and detail["pending_reviews"] == 2  # short answer + essay
    queue = (await c.get("/api/v1/assessment-reviews")).json()
    mine = [q for q in queue if q["invitation_id"] == inv["id"]]
    assert {q["kind"] for q in mine} == {"short_text", "essay"}
    assert all("answer_key" not in q for q in mine)
    for q in mine:
        rv = await c.post(f"/api/v1/attempt-answers/{q['answer_id']}/review", json={"score": str(q["max_points"]) if q["kind"] == "short_text" else "4", "note": "ok"}, headers=h(dorg.admin))
        assert rv.status_code == 200, rv.text
    res = (await c.get(f"{INV}/{inv['id']}")).json()
    assert res["status"] == "reviewed", res
    assert Decimal(res["score"]) == Decimal("90.00")  # 9 of 10 points (essay 4 of 5)
    assert res["section_scores"]["s1"]["pct"] == "90.00" and res["percentile"] == "0.00"
    assert res["signal_counts"] == {"tab_switch": 3}
    assert kinds  # delivered
    # star: Pass / Fail human only
    for kind in ("ai", "workflow", "system", "api_key"):
        async with tenant_session(dorg.tenant_id) as db:
            with pytest.raises(DomainError) as exc:
                await invitations.decide(ctx(dorg, actor_kind=kind), db, uuid.UUID(inv["id"]), DecideIn(result="pass"))
            assert exc.value.code == "transition_not_allowed"
    ok = await c.post(f"{INV}/{inv['id']}/decide", json={"result": "pass", "note": "good"}, headers=h(dorg.admin))
    assert ok.status_code == 200 and ok.json()["status"] == "passed" and ok.json()["result"] == "pass"
    results = (await c.get(f"/api/v1/candidates/{cand}/assessment-results")).json()
    assert [x["id"] for x in results] == [inv["id"]]


async def test_server_deadline_resume_session_and_auto_submit(dorg: DOrg) -> None:
    c = dorg.admin.client
    _, _, inv, token = await _setup(dorg)
    view = await start(c, token)
    session = view["session_id"]
    # 2-minute disconnect: remaining time follows the server clock, answers intact
    first = view["items"][0]
    assert (await save(c, token, session, 1, [{"position": first["position"], "response": answer_for(first, first["kind"])}])).status_code == 200
    await shift_attempt(dorg, inv["id"], 2)
    resumed = await c.get(f"{PUBLIC}/{token}/assessment/attempt", headers={SESSION: session})
    body = resumed.json()
    assert resumed.status_code == 200 and body["session_id"] is None
    assert 27 * 60 - 5 <= body["remaining_seconds"] <= 28 * 60 + 1
    assert str(first["position"]) in {str(k) for k in body["answers"]}
    # another browser: new session, old one refused, concurrent_session logged
    other = await c.get(f"{PUBLIC}/{token}/assessment/attempt")
    new_session = other.json()["session_id"]
    assert new_session and new_session != session
    old = await save(c, token, session, 2, [])
    assert old.status_code == 409 and old.json()["error"]["code"] == "attempt_session_replaced"
    # past deadline + 30 s: writes refused, the beat auto-submits and scores
    await shift_attempt(dorg, inv["id"], 40)
    late = await save(c, token, new_session, 3, [])
    assert late.status_code == 409 and late.json()["error"]["code"] == "attempt_deadline_passed"
    assert (await submit(c, token, new_session)).json()["error"]["code"] == "attempt_deadline_passed"
    async with tenant_session(dorg.tenant_id) as db:
        assert await taking.auto_submit_due(db) >= 1
    rows = await sql(dorg, "SELECT status FROM attempts WHERE invitation_id = :i", i=uuid.UUID(inv["id"]))
    assert rows[0][0] == "auto_submitted"
    attempt_id = (await sql(dorg, "SELECT id FROM attempts WHERE invitation_id = :i", i=uuid.UUID(inv["id"])))[0][0]
    signals = (await c.get(f"/api/v1/attempts/{attempt_id}/signals")).json()
    assert signals["counts"].get("concurrent_session") == 1
    assert (await c.get(f"{INV}/{inv['id']}")).json()["status"] in ("completed", "reviewed")


async def test_accommodation_extends_deadline_and_start_is_idempotent(dorg: DOrg) -> None:
    a, _ = await simple_assessment(dorg, ("single",))
    cand = await make_candidate(dorg)
    await invite_ok(dorg, a["id"], cand, accommodation_factor="1.50")
    token = await link_token(dorg, cand)
    pre = (await dorg.admin.client.get(f"{PUBLIC}/{token}/assessment")).json()
    assert pre["time_limit_minutes"] == 45 and "tab_switch" in pre["logged_signals"]
    view = await start(dorg.admin.client, token)
    assert 44 * 60 < view["remaining_seconds"] <= 45 * 60
    again = await dorg.admin.client.post(f"{PUBLIC}/{token}/assessment/start", json={"notice_id": None, "agreed": True}, headers={SESSION: view["session_id"]})
    assert again.status_code == 200 and again.json()["session_id"] is None
    no = await dorg.admin.client.post(f"{PUBLIC}/{token}/assessment/start", json={"agreed": False})
    assert no.status_code == 422


async def test_proctoring_rate_limit_and_signals_never_change_score(dorg: DOrg) -> None:
    c = dorg.admin.client
    _, _, inv, token = await _setup(dorg, ("single",))
    view = await start(c, token)
    s = view["session_id"]
    item = view["items"][0]
    await save(c, token, s, 1, [{"position": item["position"], "response": answer_for(item, "single")}])
    accepted = 0
    for _ in range(2):
        r = await c.post(f"{PUBLIC}/{token}/assessment/attempt/signals", json={"signals": [{"kind": "tab_switch", "detail": {"x": "y" * 600}}] * 50}, headers={SESSION: s})
        accepted += r.json()["accepted"]
    assert accepted == 60  # max_signals_per_minute
    att = (await sql(dorg, "SELECT id, dropped_signals FROM attempts WHERE invitation_id = :i", i=uuid.UUID(inv["id"])))[0]
    assert att[1] == 40
    await submit(c, token, s)
    res = (await c.get(f"{INV}/{inv['id']}")).json()
    assert res["status"] == "reviewed" and res["score"] == "100.00"
    detail = (await sql(dorg, "SELECT detail FROM proctoring_signals WHERE attempt_id = :a LIMIT 1", a=att[0]))[0][0]
    assert detail == {"truncated": True}
    from app.services.assessments import proctoring

    with pytest.raises(proctoring.AppendOnlyViolation):
        proctoring.mutate_signal()
    ann = await c.post(f"/api/v1/attempts/{att[0]}/signal-annotations", json={"note": "Candidate explained the tab switches"}, headers=h(dorg.admin))
    assert ann.status_code == 201
    assert len((await c.get(f"/api/v1/attempts/{att[0]}/signals")).json()["annotations"]) == 1


def test_signal_invariant_no_reads_in_scoring_paths() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "app" / "services" / "assessments"
    for name in ("lifecycle.py", "scoring.py", "review.py", "results.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "ProctoringSignal" not in text and "proctoring_signals" not in text.replace("never reads proctoring signals", ""), name
        assert "import proctoring" not in text and "proctoring." not in text, name


# ---------------------------------------------------------------- lifecycle matrix (section 5.1)
STATUSES = (None, "invited", "started", "expired", "completed", "reviewed", "passed", "failed", "cancelled")
ACTORS = ("user", "workflow", "candidate", "secure_link", "system", "ai", "api_key", "recorded_by_user")


def test_transition_matrix() -> None:
    for src in STATUSES:
        for dst in STATUSES[1:]:
            for actor in ACTORS:
                ok = lifecycle.allowed(src, dst, actor, gating=True)
                assert ok == (actor in lifecycle.TRANSITIONS.get((src, dst), frozenset()) and not ((src, dst) == ("reviewed", "passed") and actor == "system")), (src, dst, actor)
    assert lifecycle.allowed(None, "invited", "workflow") and not lifecycle.allowed(None, "invited", "system")
    assert not lifecycle.allowed("reviewed", "failed", "system", gating=False, auto_pass_allowed=True, score=Decimal(99), threshold=Decimal(1))


@pytest.mark.parametrize(
    ("gating", "allow", "score", "threshold", "expected"),
    [(False, True, "80", "70", True), (False, False, "80", "70", False), (False, True, "60", "70", False), (True, True, "99", "10", False)],
)
def test_auto_pass_rule(gating: bool, allow: bool, score: str, threshold: str, expected: bool) -> None:
    assert lifecycle.allowed("reviewed", "passed", "system", gating=gating, auto_pass_allowed=allow, score=Decimal(score), threshold=Decimal(threshold)) is expected


async def test_auto_pass_non_gating_end_to_end(dorg: DOrg) -> None:
    c = dorg.admin.client
    assert (await c.put("/api/v1/assessment-settings", json={"allow_auto_pass_non_gating": True}, headers=h(dorg.admin))).status_code == 200
    a, _ = await simple_assessment(dorg, ("single",), auto_pass_threshold_pct="50")
    cand = await make_candidate(dorg)
    inv = await invite_ok(dorg, a["id"], cand, gating=False)
    token = await link_token(dorg, cand)
    view = await start(c, token)
    it = view["items"][0]
    await save(c, token, view["session_id"], 1, [{"position": it["position"], "response": answer_for(it, "single")}])
    await submit(c, token, view["session_id"])
    res = (await c.get(f"{INV}/{inv['id']}")).json()
    assert res["status"] == "passed" and res["result"] == "pass"


async def test_workflow_action_declaring_a_star_transition_is_refused() -> None:
    from app.services.assessments.workflow_actions import InviteParams
    from app.services.workflow import actions

    async def _noop(ac: Any) -> str:
        return "x"

    with pytest.raises(ValueError, match="human-only"):
        actions.register_action(
            "assessments.bad_pass",
            actions.ActionDef("Bad", InviteParams, actions._any, _noop, _noop, performs_transitions=(("assessment_invitation", "passed"),)),
        )


# ---------------------------------------------------------------- invitation rules
async def test_invite_guards_idempotency_extend_cancel_expire(dorg: DOrg) -> None:
    c = dorg.admin.client
    item = await create_item(dorg, "single")
    draft = (await c.post(ASM, json={"name": "Draft"}, headers=h(dorg.admin))).json()
    cand = await make_candidate(dorg)
    r = await invite(dorg, draft["id"], cand)
    assert r.status_code == 409 and r.json()["error"]["code"] == "assessment_not_published"
    a = await published(dorg, [{"key": "s", "title": "S", "items": [{"item_id": item["id"]}]}])
    first = await invite_ok(dorg, a["id"], cand, idempotency_key="k-1")
    assert first["gating"] is True and first["status"] == "invited"
    same = await invite(dorg, a["id"], cand, idempotency_key="k-1")
    assert same.status_code == 201 and same.json()["id"] == first["id"]
    dup = await invite(dorg, a["id"], cand)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "invitation_exists"
    # expiry beat (system) then extend (user) re-opens it
    await sql(dorg, "UPDATE assessment_invitations SET deadline_at = now() - interval '1 minute' WHERE id = :i", i=uuid.UUID(first["id"]))
    async with tenant_session(dorg.tenant_id) as db:
        assert await invitations.expire_due(db) == 1
    assert (await c.get(f"{INV}/{first['id']}")).json()["status"] == "expired"
    ext = await c.post(f"{INV}/{first['id']}/extend", json={"deadline_at": soon(2)}, headers=h(dorg.admin))
    assert ext.status_code == 200 and ext.json()["status"] == "invited" and ext.json()["extended_count"] == 1
    no_reason = await c.post(f"{INV}/{first['id']}/cancel", json={"reason": ""}, headers=h(dorg.admin))
    assert no_reason.status_code == 422
    cancel = await c.post(f"{INV}/{first['id']}/cancel", json={"reason": "Role closed"}, headers=h(dorg.admin))
    assert cancel.status_code == 200 and cancel.json()["status"] == "cancelled"
    bad = await c.post(f"{INV}/{first['id']}/decide", json={"result": "pass"}, headers=h(dorg.admin))
    assert bad.status_code == 409 and bad.json()["error"]["code"] == "transition_not_allowed"
    # bulk: created / exists
    other = await make_candidate(dorg, "Bina")
    bulk = await c.post(f"{ASM}/{a['id']}/invitations/bulk", json={"candidate_ids": [str(other), str(other)]}, headers=h(dorg.admin))
    assert bulk.status_code == 200 and [x["status"] for x in bulk.json()["results"]] == ["created"]
    bulk2 = await c.post(f"{ASM}/{a['id']}/invitations/bulk", json={"candidate_ids": [str(other)]}, headers=h(dorg.admin))
    assert bulk2.json()["results"][0]["status"] == "exists"


async def test_reminders_once(dorg: DOrg) -> None:
    a, _ = await simple_assessment(dorg, ("single",))
    cand = await make_candidate(dorg)
    await invite_ok(dorg, a["id"], cand, deadline_at=soon(0.5))  # type: ignore[arg-type]
    async with tenant_session(dorg.tenant_id) as db:
        assert await invitations.send_reminders(db) == 1
    async with tenant_session(dorg.tenant_id) as db:
        assert await invitations.send_reminders(db) == 0


async def test_cross_tenant_and_link_isolation(dorg: DOrg, other_dorg: DOrg) -> None:
    _, _, inv, token = await _setup(dorg, ("single",))
    oc = other_dorg.admin.client
    for path in (f"{INV}/{inv['id']}", f"{ASM}/{inv['assessment_id']}"):
        assert (await oc.get(path)).status_code == 404
    assert (await oc.post(f"{INV}/{inv['id']}/decide", json={"result": "pass"}, headers=h(other_dorg.admin))).status_code == 404
    assert (await oc.get(f"{PUBLIC}/{'x' * 43}/assessment")).status_code == 404
    assert (await oc.get(f"{PUBLIC}/{token}/assessment")).status_code == 200  # the token alone is the credential
