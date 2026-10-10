# ruff: noqa: E501
"""Careers apply-source resolver (Epic 75, spec A4; FR-388). A listing's ``ref`` attributes only within its own
Tenant; a ref of another Tenant's listing is ignored (the application stays ``career_portal``)."""

from app.core.database import tenant_session
from app.services.job_network.apply_resolver import resolve
from tests.job_network.conftest import Site, add_listing, cleanup, make_member


async def test_resolver_attributes_within_tenant_and_ignores_cross_tenant(site: Site, other_site: Site) -> None:
    try:
        member_a = await make_member(site.tenant_id, site.slug)
        member_b = await make_member(other_site.tenant_id, other_site.slug)
        listing_a = await add_listing(member_a, site.tenant_id)
        listing_b = await add_listing(member_b, other_site.tenant_id)

        async with tenant_session(site.tenant_id) as db:
            # a ref of this tenant's own listing attributes to job_network
            assert await resolve(db, site.tenant_id, str(listing_a)) == ("job_network", str(listing_a))
            # a ref of ANOTHER tenant's listing is ignored (stays career_portal)
            assert await resolve(db, site.tenant_id, str(listing_b)) is None
            # malformed / unknown refs are ignored
            assert await resolve(db, site.tenant_id, "not-a-uuid") is None
            assert await resolve(db, site.tenant_id, None) is None
    finally:
        await cleanup(site.tenant_id)
        await cleanup(other_site.tenant_id)
