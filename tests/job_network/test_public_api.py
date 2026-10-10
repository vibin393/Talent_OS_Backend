# ruff: noqa: E501
"""Public network site API (Epic 75, spec A6; FR-387). No response ever carries ``tenant_id`` / ``member_id`` /
``posting_ref`` / ``hold_reasons`` / report counts / stats; held and suspended listings 404."""

from typing import Any

import httpx

from tests.job_network.conftest import Site, add_listing, cleanup, make_member

NET = "/api/v1/public/network"
FORBIDDEN_KEYS = {"tenant_id", "member_id", "posting_ref", "hold_reasons", "ip_hash", "content_sha256", "search_tsv"}


def _scan(obj: Any, path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k not in FORBIDDEN_KEYS, f"forbidden key {k!r} at {path}"
            _scan(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan(v, f"{path}[{i}]")


async def test_search_and_detail_never_leak_internal_keys(site: Site, public: httpx.AsyncClient) -> None:
    try:
        member = await make_member(site.tenant_id, site.slug)
        listing_id = await add_listing(member, site.tenant_id, pay_min=1800000.0)
        page = await public.get(f"{NET}/jobs?facets=1")
        assert page.status_code == 200, page.text
        _scan(page.json())
        assert page.headers.get("Cache-Control", "").startswith("public")

        detail = await public.get(f"{NET}/jobs/{listing_id}")
        assert detail.status_code == 200, detail.text
        _scan(detail.json())
        assert detail.json()["jsonld"]["directApply"] is False

        agency = await public.get(f"{NET}/agencies/{site.slug}")
        assert agency.status_code == 200, agency.text
        _scan(agency.json())
    finally:
        await cleanup(site.tenant_id)


async def test_held_listing_is_404(site: Site, public: httpx.AsyncClient) -> None:
    try:
        member = await make_member(site.tenant_id, site.slug)
        held = await add_listing(member, site.tenant_id, status="held")
        resp = await public.get(f"{NET}/jobs/{held}")
        assert resp.status_code == 404, resp.text
    finally:
        await cleanup(site.tenant_id)


async def test_report_returns_202(site: Site, public: httpx.AsyncClient) -> None:
    try:
        member = await make_member(site.tenant_id, site.slug)
        listing_id = await add_listing(member, site.tenant_id)
        resp = await public.post(f"{NET}/jobs/{listing_id}/report", json={"reason": "scam_fee", "details": "Asks for a fee. Contact 9812345610."})
        assert resp.status_code == 202, resp.text
    finally:
        await cleanup(site.tenant_id)
