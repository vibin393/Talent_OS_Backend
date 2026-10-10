"""Epic 9 A9 (hard rule): no internal data in any public or candidate-facing output.

Seeds sentinels (note, match score + explanation, bill rate, hidden client, reason codes, owner email, AI
summary), then sweeps every ``/public/careers/*`` and ``/portal/*`` GET plus the export ``data.json`` for
sentinel values and forbidden keys; checks the ``Portal*`` / ``Public*`` OpenAPI schemas; and checks the
hidden-client rule both ways.
"""

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.core import secure_links
from app.core.database import tenant_session
from app.models.matching import Match
from app.models.pipeline import Application, Screening
from app.schemas import careers as careers_schemas
from app.schemas import portal as portal_schemas
from tests.modules.demands.helpers import h
from tests.portal.conftest import PORTAL, Site, apply, careers, ch, publish, sign_in

EMAIL = "priya.raman@example.com"
SENTINELS = (
    "SENTINEL_NOTE",
    "SENTINEL_CLIENT",
    "SENTINEL_MATCH",
    "SENTINEL_AI_SUMMARY",
    "9999",
    "87.3",
    "accepted_other_offer",
)
FORBIDDEN_EXACT = {
    "notes",
    "score",
    "bill_rate",
    "pay_rate",
    "client_id",
    "reason_code",
    "ai_summary",
    "suggested_outcome",
    "stage_key",
    "req_id",
}
FORBIDDEN_PREFIXES = ("scorecard", "match", "margin", "owner")


def _keys(value: Any, path: str = "") -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        for k, v in value.items():
            out.append(str(k))
            out += _keys(v, f"{path}.{k}")
    elif isinstance(value, list):
        for item in value:
            out += _keys(item, path)
    return out


def _forbidden(keys: list[str]) -> list[str]:
    return sorted({k for k in keys if k in FORBIDDEN_EXACT or k.startswith(FORBIDDEN_PREFIXES)})


def _check(text: str, data: Any, where: str, extra: tuple[str, ...] = ()) -> None:
    for s in (*SENTINELS, *extra):
        assert s not in text, f"{s} leaked in {where}"
    assert _forbidden(_keys(data)) == [], f"forbidden keys in {where}: {_forbidden(_keys(data))}"


async def _seed(site: Site, application_id: uuid.UUID, candidate_id: uuid.UUID, demand_id: str) -> uuid.UUID:
    m = site.admin
    note = await m.client.post(
        f"/api/v1/candidates/{candidate_id}/activities",
        json={"kind": "note", "body": "SENTINEL_NOTE secret"},
        headers=h(m),
    )
    assert note.status_code == 201, note.text
    async with tenant_session(site.tenant_id) as db:
        db.add(
            Match(
                id=uuid.uuid4(), tenant_id=site.tenant_id, demand_id=uuid.UUID(demand_id), candidate_id=candidate_id,
                overall=Decimal("87.3"), sub_scores={"skills": 87.3}, scoring_version="v1", input_hash="x" * 64,
                computed_at=datetime.now(UTC), explanation="SENTINEL_MATCH", explanation_status="ready",
            )
        )  # fmt: skip
        screening = Screening(
            id=uuid.uuid4(), tenant_id=site.tenant_id, application_id=application_id, candidate_id=candidate_id,
            kind="ai", status="sent", questions=[{"key": "why", "label": "Why?", "type": "text"}],
            ai_summary={"summary": "SENTINEL_AI_SUMMARY"}, notes="SENTINEL_NOTE screening", outcome="reject",
            reason_code="not_qualified",
        )  # fmt: skip
        db.add(screening)
        await db.flush()
        link, _ = await secure_links.issue(
            db, tenant_id=site.tenant_id, purpose="ai_screening", subject_type="screening", subject_id=screening.id,
            ttl=timedelta(days=3),
        )  # fmt: skip
        screening.secure_link_id = link.id
        return screening.id


async def test_response_sweep_and_export(app: FastAPI, site: Site, public: httpx.AsyncClient) -> None:
    p = await publish(
        site,
        questions=[{"key": "notice", "label": "Notice?", "type": "number", "maps_to": "notice_period_days"}],
    )
    p2 = await publish(site, title="Second Role")
    assert (await apply(public, site.slug, p["posting_id"], answers={"notice": 15})).status_code == 202
    assert (await apply(public, site.slug, p2["posting_id"])).status_code == 202
    async with tenant_session(site.tenant_id) as db:
        apps = list((await db.execute(select(Application).order_by(Application.created_at))).scalars())
    first, second = apps
    screening_id = await _seed(site, first.id, first.candidate_id, p["demand_id"])
    client = await sign_in(app, site.slug, EMAIL)
    try:
        w = await client.post(
            f"{PORTAL}/applications/{second.id}/withdraw",
            json={"reason_code": "accepted_other_offer"},
            headers=ch(client),
        )
        assert w.status_code == 200, w.text
        owner_email = site.admin.email
        public_gets = [
            careers(site.slug),
            f"{careers(site.slug)}/postings",
            f"{careers(site.slug)}/postings/{p['posting_id']}",
            f"{careers(site.slug)}/postings/{p['posting_id']}/apply-form",
            f"{careers(site.slug)}/sitemap",
        ]
        for url in public_gets:
            r = await public.get(url)
            assert r.status_code == 200, (url, r.text)
            _check(r.text, r.json(), url, (owner_email, p["req_id"]))
        portal_gets = [
            "/me", "/profile", "/resumes", "/applications", f"/applications/{first.id}", f"/applications/{second.id}",
            "/interviews?upcoming=false", "/ai-screenings", f"/ai-screenings/{screening_id}", "/consents",
            "/data-exports", "/privacy-requests", "/withdraw-reasons",
        ]  # fmt: skip
        for path in portal_gets:
            r = await client.get(f"{PORTAL}{path}")
            assert r.status_code == 200, (path, r.text)
            extra = (owner_email,) if path != "/withdraw-reasons" else ()
            text = r.text if path != "/withdraw-reasons" else r.text.replace("accepted_other_offer", "")
            _check(text, r.json(), path, extra)

        assert (await client.post(f"{PORTAL}/data-exports", headers=ch(client))).status_code == 202
        job = (await client.get(f"{PORTAL}/data-exports")).json()[0]
        assert job["status"] == "ready"
        zipped = await client.get(job["download_url"])
        with zipfile.ZipFile(io.BytesIO(zipped.content)) as zf:
            raw = zf.read("data.json").decode()
        data = json.loads(raw)
        _check(raw, data, "export data.json", (owner_email,))
        assert data["application_answers"][0]["answers"] == [{"question": "Notice?", "answer": 15}]
    finally:
        await client.aclose()


def test_openapi_portal_and_public_schemas_have_no_forbidden_fields(app: FastAPI) -> None:
    ours = {
        name
        for module in (careers_schemas, portal_schemas)
        for name in dir(module)
        if name.startswith(("Public", "Portal")) and isinstance(getattr(module, name), type)
    }
    schemas = app.openapi()["components"]["schemas"]
    checked = 0
    for name, schema in schemas.items():
        if name not in ours or name.endswith("In"):  # request bodies (e.g. a withdraw reason) are not output
            continue
        checked += 1
        props = list((schema.get("properties") or {}).keys())
        assert _forbidden(props) == [], f"{name}: {_forbidden(props)}"
    assert checked >= 20


async def test_hidden_client_absent_everywhere_and_shown_when_set(
    app: FastAPI, site: Site, public: httpx.AsyncClient
) -> None:
    hidden = await publish(site, title="Hidden Client Role")
    assert (await apply(public, site.slug, hidden["posting_id"])).status_code == 202
    client = await sign_in(app, site.slug, EMAIL)
    try:
        for url in (
            f"{careers(site.slug)}/postings",
            f"{careers(site.slug)}/postings/{hidden['posting_id']}",
        ):
            assert "SENTINEL_CLIENT" not in (await public.get(url)).text
        assert "SENTINEL_CLIENT" not in (await client.get(f"{PORTAL}/applications")).text

        shown = await publish(site, title="Shown Client Role", show_client_name=True)
        detail = (await public.get(f"{careers(site.slug)}/postings/{shown['posting_id']}")).json()
        assert detail["client_name"] == "SENTINEL_CLIENT"
        assert detail["jsonld"]["hiringOrganization"]["name"] == "SENTINEL_CLIENT"
        r = await client.post(f"{PORTAL}/data-exports", headers=ch(client))
        assert r.status_code == 202
    finally:
        await client.aclose()
    # Apply to the shown one as well: the portal shows the client name only for that application.
    assert (await apply(public, site.slug, shown["posting_id"])).status_code == 202
    client = await sign_in(app, site.slug, EMAIL)
    try:
        names = {a["title"]: a["client_name"] for a in (await client.get(f"{PORTAL}/applications")).json()}
        assert names == {"Hidden Client Role": None, "Shown Client Role": "SENTINEL_CLIENT"}
    finally:
        await client.aclose()
