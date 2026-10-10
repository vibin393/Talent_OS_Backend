"""Epic 33 FR-174 (AD-38): custom Career Portal domains with a mocked DNS resolver."""

import pytest
from sqlalchemy import text

from app.core.database import platform_session
from app.core.settings import get_settings
from app.services import custom_domains
from tests.epic10 import API, DOrg, h

DOMAIN = f"{API}/career-site/custom-domain"


@pytest.fixture
def dns() -> dict[str, list[str]]:
    records: dict[str, list[str]] = {}

    async def resolve(name: str) -> list[str]:
        return list(records.get(name, []))

    custom_domains.set_resolver(resolve)
    return records


async def _enable_site(org: DOrg) -> str:
    resp = await org.admin.client.patch(
        f"{API}/admin/career-site", json={"enabled": True}, headers=h(org.admin)
    )
    assert resp.status_code == 200, resp.text
    return org.tenant.slug


async def test_verify_resolve_remove_and_slug_fallback(dorg: DOrg, dns: dict[str, list[str]]) -> None:
    m = dorg.admin
    slug = await _enable_site(dorg)
    host = f"careers-{slug}.example.in"
    assert (await m.client.get(DOMAIN)).json() is None
    added = await m.client.post(DOMAIN, json={"host": host.upper()}, headers=h(m))
    assert added.status_code == 201, added.text
    body = added.json()
    assert (
        body["host"] == host
        and body["status"] == "pending"
        and body["txt_name"] == f"_talentos-verify.{host}"
    )
    assert body["certificate"] == "managed_by_hosting_pending"
    assert (
        await m.client.get(f"{API}/public/careers/resolve-host", params={"host": host})
    ).status_code == 404
    pending = await m.client.post(f"{DOMAIN}/verify-now", headers=h(m))
    assert pending.json()["status"] == "pending"
    dns[body["txt_name"]] = [body["txt_value"]]
    verified = await m.client.post(f"{DOMAIN}/verify-now", headers=h(m))
    assert verified.json()["status"] == "verified"
    assert await custom_domains.tenant_for_host(host) == dorg.tenant_id
    resolved = await m.client.get(f"{API}/public/careers/resolve-host", params={"host": host})
    assert resolved.status_code == 200 and resolved.json() == {"slug": slug}
    # careers API by Host (path slug ignored) and by slug (fallback)
    by_host = await m.client.get(f"{API}/public/careers/anything", headers={"X-Forwarded-Host": host})
    by_slug = await m.client.get(f"{API}/public/careers/{slug}")
    assert by_host.status_code == 200 and by_slug.status_code == 200, (by_host.text, by_slug.text)
    # removal invalidates the cache at once
    assert (await m.client.delete(DOMAIN, headers=h(m))).status_code == 204
    assert await custom_domains.tenant_for_host(host) is None
    assert (
        await m.client.get(f"{API}/public/careers/anything", headers={"X-Forwarded-Host": host})
    ).status_code == 404


async def test_domain_taken_platform_refused_and_txt_loss(
    dorg: DOrg, other_dorg: DOrg, dns: dict[str, list[str]]
) -> None:
    host = f"jobs-{dorg.tenant.slug}.example.in"
    first = await dorg.admin.client.post(DOMAIN, json={"host": host}, headers=h(dorg.admin))
    assert first.status_code == 201
    taken = await other_dorg.admin.client.post(DOMAIN, json={"host": host}, headers=h(other_dorg.admin))
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "domain_taken"
    platform_host = f"x.{(get_settings().frontend_url.split('//')[1].split(':')[0])}"
    refused = await other_dorg.admin.client.post(
        DOMAIN, json={"host": platform_host}, headers=h(other_dorg.admin)
    )
    assert refused.status_code == 422
    assert (
        await other_dorg.admin.client.post(DOMAIN, json={"host": "not a host"}, headers=h(other_dorg.admin))
    ).status_code == 422
    # verified, then the TXT disappears for 3 daily checks -> failed
    dns[first.json()["txt_name"]] = [first.json()["txt_value"]]
    await custom_domains.check_domains()
    dns.clear()
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    for day in (1, 2, 3):
        await custom_domains.check_domains(now + timedelta(days=day, minutes=1))
    async with platform_session() as pdb:
        status = (
            await pdb.execute(text("SELECT status FROM custom_domains WHERE host = :h"), {"h": host})
        ).scalar_one()
    assert status == "failed"
    # another tenant cannot see / remove it
    assert (await other_dorg.admin.client.get(DOMAIN)).json() is None
    assert (await other_dorg.admin.client.delete(DOMAIN, headers=h(other_dorg.admin))).status_code == 404
