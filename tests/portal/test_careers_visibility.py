"""Epic 36 (AD-50, spec A3): ``demands.visibility = 'internal'`` hides a live posting from the public careers
list, detail, sitemap and apply surfaces; ``external`` and ``both`` stay public; the default is ``external``."""

import httpx

from app.core.database import tenant_session
from app.services.demands import jd
from tests.modules.demands.helpers import DEMANDS, h
from tests.portal.conftest import Site, careers, publish, start


async def test_internal_demand_hidden_everywhere(site: Site, public: httpx.AsyncClient) -> None:
    ext = await publish(site, title="External Role")
    both = await publish(site, title="Both Role", visibility="both")
    internal = await publish(site, title="Internal Role", visibility="internal")
    base = careers(site.slug)

    listed = {i["id"] for i in (await public.get(f"{base}/postings")).json()["items"]}
    assert listed == {ext["posting_id"], both["posting_id"]}
    sitemap = {e["posting_id"] for e in (await public.get(f"{base}/sitemap")).json()}
    assert sitemap == {ext["posting_id"], both["posting_id"]}

    assert (await public.get(f"{base}/postings/{internal['posting_id']}")).status_code == 404
    assert (await public.get(f"{base}/postings/{internal['posting_id']}/apply-form")).status_code == 404
    for p in (ext, both):
        assert (await public.get(f"{base}/postings/{p['posting_id']}")).status_code == 200

    # Apply: a session for the visible posting starts; the internal posting has no apply surface.
    assert (await start(public, site.slug, ext["posting_id"])).status_code == 201

    # The Career Portal read model (jd.list_live_postings) applies the same filter.
    async with tenant_session(site.tenant_id) as db:
        live = {p.demand_id for p in await jd.list_live_postings(db)}
    assert str(internal["demand_id"]) not in {str(x) for x in live}
    assert {str(ext["demand_id"]), str(both["demand_id"])} <= {str(x) for x in live}


async def test_visibility_default_patch_and_validation(site: Site, public: httpx.AsyncClient) -> None:
    m = site.admin
    p = await publish(site, title="Toggle Role")
    got = (await m.client.get(f"{DEMANDS}/{p['demand_id']}")).json()
    assert got["visibility"] == "external"
    assert got["workforce_plan_line_id"] is None and got["project_role_id"] is None

    patched = await m.client.patch(
        f"{DEMANDS}/{p['demand_id']}", json={"visibility": "internal"}, headers=h(m)
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["visibility"] == "internal"
    assert (await public.get(f"{careers(site.slug)}/postings/{p['posting_id']}")).status_code == 404

    bad = await m.client.patch(f"{DEMANDS}/{p['demand_id']}", json={"visibility": "secret"}, headers=h(m))
    assert bad.status_code == 422
    empty = await m.client.patch(f"{DEMANDS}/{p['demand_id']}", json={"visibility": None}, headers=h(m))
    assert empty.status_code == 422
