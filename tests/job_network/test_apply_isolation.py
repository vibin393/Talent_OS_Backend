# ruff: noqa: E501
"""Apply isolation (Epic 75, spec A4 -- binding). Applying from the network is a 303 redirect to the posting Tenant's
own careers apply page; the network never receives the application. No candidate data crosses tenants -- the network
holds no candidate / application tables at all, and a ref only resolves within its own Tenant (``test_source_tag``)."""

import httpx

from tests.job_network.conftest import Site, add_listing, cleanup, make_member

NET = "/api/v1/public/network"


async def test_apply_is_a_303_to_the_posting_tenant_with_source_tag(site: Site, public: httpx.AsyncClient) -> None:
    try:
        member = await make_member(site.tenant_id, site.slug)
        listing_id = await add_listing(member, site.tenant_id)
        resp = await public.get(f"{NET}/jobs/{listing_id}/apply", follow_redirects=False)
        assert resp.status_code == 303, resp.text
        location = resp.headers["location"]
        assert "src=job_network" in location and f"ref={listing_id}" in location
    finally:
        await cleanup(site.tenant_id)


async def test_network_has_no_candidate_tables() -> None:
    # Structural proof the network cannot hold applicant data: its models expose no candidate/application table.
    from app.models import job_network as m

    tables = {
        c.__tablename__
        for c in vars(m).values()
        if isinstance(c, type) and hasattr(c, "__tablename__")
    }
    assert tables == {
        "network_members", "network_listings", "network_listing_stats", "network_reports",
        "network_moderation_actions", "job_network_settings", "job_network_choices",
    }
    assert not any("candidate" in t or "application" in t for t in tables)
