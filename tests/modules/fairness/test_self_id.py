# ruff: noqa: E501
"""Epic 49 A1-A3 / A11: settings, category sets, voluntary self-ID in the Candidate Portal and on the apply success
step, encryption and the ``fairness_access`` isolation policy."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.database import tenant_session
from tests.modules.demands.helpers import DOrg, h
from tests.modules.fairness.helpers import CSRF, F, PORTAL, enable, make_candidate, portal, put_answers
from tests.portal.conftest import Site, careers, origin, publish, start, submit, upload

PUB = "/api/v1/public/fairness"


async def _count_rows(tenant_id: uuid.UUID, mode: str | None = None) -> int:
    async with tenant_session(tenant_id) as db:
        if mode:
            await db.execute(text("SELECT set_config('app.fairness_access', :m, true)"), {"m": mode})
        return int((await db.execute(text("SELECT count(*) FROM demographic_self_ids"))).scalar_one())


async def test_settings_defaults_and_bounds(dorg: DOrg) -> None:
    r = await dorg.admin.client.get(f"{F}/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["self_id_enabled"] is False and body["k"] == 5 and body["notice_lead_business_days"] == 10
    assert [d["key"] for d in body["category_set"]["dimensions"]] == ["gender", "age_band", "disability"]
    for d in body["category_set"]["dimensions"]:
        assert d["options"][-1]["key"] == "prefer_not_to_say"
    bad = await dorg.admin.client.patch(f"{F}/settings", json={"k": 4}, headers=h(dorg.admin))
    assert bad.status_code == 422
    bad = await dorg.admin.client.patch(f"{F}/settings", json={"notice_lead_business_days": 9}, headers=h(dorg.admin))
    assert bad.status_code == 422


async def test_category_labels_new_version_and_no_new_dimension(dorg: DOrg) -> None:
    edits = {"dimensions": [{"key": "gender", "label": "Gender identity", "enabled": True, "options": [{"key": "woman", "label": "Female"}]}]}
    r = await dorg.admin.client.put(f"{F}/category-set", json=edits, headers=h(dorg.admin))
    assert r.status_code == 200, r.text
    cs = r.json()["category_set"]
    assert cs["version"] == 2
    gender = next(d for d in cs["dimensions"] if d["key"] == "gender")
    assert gender["label"] == "Gender identity" and gender["options"][0]["label"] == "Female"
    for key in ("religion", "caste", "race"):
        bad = await dorg.admin.client.put(
            f"{F}/category-set", json={"dimensions": [{"key": key, "label": key, "options": []}]}, headers=h(dorg.admin)
        )
        assert bad.status_code == 422, key
    bad = await dorg.admin.client.put(
        f"{F}/category-set",
        json={"dimensions": [{"key": "gender", "label": "G", "options": [{"key": "new_option", "label": "x"}]}]},
        headers=h(dorg.admin),
    )
    assert bad.status_code == 422


@pytest.mark.parametrize("key", ["religion", "caste", "race", "ethnicity"])
async def test_db_check_refuses_protected_dimensions(dorg: DOrg, key: str) -> None:
    await dorg.admin.client.get(f"{F}/settings")  # seeds the set
    with pytest.raises((IntegrityError, DBAPIError)):
        async with tenant_session(dorg.tenant_id) as db:
            set_id = (await db.execute(text("SELECT id FROM demographic_category_sets LIMIT 1"))).scalar_one()
            await db.execute(
                text(
                    "INSERT INTO demographic_dimensions (id, tenant_id, set_id, key, label, enabled, position, options) "
                    "VALUES (gen_random_uuid(), :t, :s, :k, :k, true, 9, '[]'::jsonb)"
                ),
                {"t": dorg.tenant_id, "s": set_id, "k": key},
            )


async def test_portal_self_id_change_delete_and_isolation(app: FastAPI, dorg: DOrg) -> None:
    await enable(dorg)
    a = await make_candidate(dorg, "Asha A")
    b = await make_candidate(dorg, "Bina B")
    ca, cb = await portal(app, dorg, a), await portal(app, dorg, b)
    try:
        form = await ca.get(f"{PORTAL}/self-id")
        assert form.status_code == 200, form.text
        f = form.json()
        assert f["enabled"] is True and f["answers"] is None and f["notice"]["version"] >= 1
        assert {d["key"] for d in f["dimensions"]} == {"gender", "age_band", "disability"}

        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
        try:
            r = await put_answers(ca, {"gender": "woman", "age_band": "25_34"})
        finally:
            logger.remove(sink)
        assert r.status_code == 200, r.text
        assert r.json()["answers"] == {"gender": "woman", "age_band": "25_34", "disability": "prefer_not_to_say"}
        captured = "\n".join(lines)
        assert "25_34" not in captured and '"woman"' not in captured

        # options outside the set / unknown dimensions -> 422
        assert (await put_answers(ca, {"gender": "alien"})).status_code == 422
        assert (await put_answers(ca, {"religion": "x"})).status_code == 422
        # stale notice -> 409
        stale = await ca.put(f"{PORTAL}/self-id", json={"notice_id": str(uuid.uuid4()), "answers": {}}, headers=CSRF)
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "notice_outdated"

        # B sees nothing of A
        rb = await cb.get(f"{PORTAL}/self-id")
        assert rb.status_code == 200 and rb.json()["answers"] is None

        # internal sessions read zero rows; the aggregate mode reads it
        assert await _count_rows(dorg.tenant_id) == 0
        assert await _count_rows(dorg.tenant_id, "aggregate") == 1

        # stored sealed (no plaintext), audit rows carry no values
        async with tenant_session(dorg.tenant_id) as db:
            await db.execute(text("SELECT set_config('app.fairness_access', 'aggregate', true)"))
            raw = (await db.execute(text("SELECT answers FROM demographic_self_ids"))).scalar_one()
            audits = (
                await db.execute(text("SELECT action, data, changes FROM audit_logs WHERE action LIKE 'self_id.%' OR entity_type = 'demographic_self_id'"))
            ).all()
        assert "woman" not in raw and "25_34" not in raw
        assert any(row[0] == "self_id.updated" for row in audits)
        for _, data, changes in audits:
            blob = json.dumps(data or {}) + json.dumps(changes or {})
            assert "woman" not in blob and "25_34" not in blob

        # change, then delete (withdraws the consent)
        r = await put_answers(ca, {"gender": "prefer_not_to_say", "disability": "yes"})
        assert r.status_code == 200 and r.json()["answers"]["disability"] == "yes"
        d = await ca.delete(f"{PORTAL}/self-id", headers=CSRF)
        assert d.status_code == 204, d.text
        assert (await ca.get(f"{PORTAL}/self-id")).json()["answers"] is None
        assert await _count_rows(dorg.tenant_id, "aggregate") == 0
        async with tenant_session(dorg.tenant_id) as db:
            status = (
                await db.execute(
                    text("SELECT status FROM consents WHERE candidate_id = :c AND purpose = 'demographic_self_id' ORDER BY created_at DESC LIMIT 1"),
                    {"c": a},
                )
            ).scalar_one()
        assert status == "withdrawn"
    finally:
        await ca.aclose()
        await cb.aclose()


async def test_disabled_self_id_is_not_offered(app: FastAPI, dorg: DOrg) -> None:
    cid = await make_candidate(dorg)
    c = await portal(app, dorg, cid)
    try:
        r = await c.get(f"{PORTAL}/self-id")
        assert r.status_code == 200 and r.json()["enabled"] is False and r.json()["dimensions"] == []
        put = await c.put(f"{PORTAL}/self-id", json={"notice_id": str(uuid.uuid4()), "answers": {}}, headers=CSRF)
        assert put.status_code == 404
    finally:
        await c.aclose()


async def test_consent_withdrawal_deletes_answers(app: FastAPI, dorg: DOrg) -> None:
    from app.core.context import RequestContext
    from app.services import privacy

    await enable(dorg)
    cid = await make_candidate(dorg)
    c = await portal(app, dorg, cid)
    try:
        assert (await put_answers(c, {"gender": "man"})).status_code == 200
    finally:
        await c.aclose()
    assert await _count_rows(dorg.tenant_id, "aggregate") == 1
    async with tenant_session(dorg.tenant_id) as db:
        await privacy.consent.withdraw_consent(RequestContext(tenant_id=dorg.tenant_id), db, cid, "demographic_self_id")
    assert await _count_rows(dorg.tenant_id, "aggregate") == 0


async def _applied(s: Site, public: object) -> tuple[str, str]:
    ids = await publish(s)
    r = await start(public, s.slug, ids["posting_id"])  # type: ignore[arg-type]
    assert r.status_code == 201, r.text
    token = r.json()["apply_token"]
    assert (await upload(public, s.slug, token)).status_code == 200  # type: ignore[arg-type]
    sub = await submit(public, s.slug, token, email=f"p-{uuid.uuid4().hex[:6]}@example.com")  # type: ignore[arg-type]
    assert sub.status_code in (200, 202), sub.text
    return token, ids["posting_id"]


async def test_apply_success_step_self_id(site: Site, other_site: Site, public: object) -> None:
    # applying without self-ID succeeds (nothing required)
    token, _posting = await _applied(site, public)
    form = await public.get(f"{PUB}/{site.slug}/self-id-form")  # type: ignore[attr-defined]
    assert form.status_code == 200 and form.json()["enabled"] is False
    await enable(site.org)
    form = (await public.get(f"{PUB}/{site.slug}/self-id-form")).json()  # type: ignore[attr-defined]
    assert form["enabled"] is True and len(form["dimensions"]) == 3
    assert await _count_rows(site.tenant_id, "aggregate") == 0

    body = {"apply_token": token, "notice_id": form["notice"]["id"], "answers": {"gender": "woman"}}
    # another tenant's slug -> 404
    other = await public.post(f"{PUB}/{other_site.slug}/self-id", json=body)  # type: ignore[attr-defined]
    assert other.status_code == 404
    ok = await public.post(f"{PUB}/{site.slug}/self-id", json=body)  # type: ignore[attr-defined]
    assert ok.status_code == 200, ok.text
    assert await _count_rows(site.tenant_id, "aggregate") == 1
    assert await _count_rows(site.tenant_id) == 0
    again = await public.post(f"{PUB}/{site.slug}/self-id", json=body)  # type: ignore[attr-defined]
    assert again.status_code == 409 and again.json()["error"]["code"] == "self_id_already_submitted"

    # expired session -> 410
    token2, _ = await _applied(site, public)
    async with tenant_session(site.tenant_id) as db:
        await db.execute(
            text("UPDATE apply_sessions SET expires_at = :t WHERE submitted_at IS NOT NULL AND expires_at > now() AND id NOT IN (SELECT apply_session_id FROM self_id_apply_uses)"),
            {"t": datetime.now(UTC) - timedelta(minutes=1)},
        )
    late = await public.post(f"{PUB}/{site.slug}/self-id", json={**body, "apply_token": token2})  # type: ignore[attr-defined]
    assert late.status_code == 410
    # an unsubmitted / unknown token -> 404
    unknown = await public.post(f"{PUB}/{site.slug}/self-id", json={**body, "apply_token": "x" * 40})  # type: ignore[attr-defined]
    assert unknown.status_code == 404


async def test_careers_apply_form_unchanged(site: Site, public: object) -> None:
    ids = await publish(site)
    r = await public.get(f"{careers(site.slug)}/postings/{ids['posting_id']}/apply-form", headers=origin())  # type: ignore[attr-defined]
    assert r.status_code == 200
    assert "demographic_self_id" not in {n["purpose"] for n in r.json()["notices"]}
