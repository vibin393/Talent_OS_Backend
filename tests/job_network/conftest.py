# ruff: noqa: E501, F401
"""Epic 75 test harness. Reuses the Epic 9 portal fixtures (``site`` / ``other_site`` / ``public`` / ``publish``) and
adds helpers to make a tenant a verified, active network member and to insert listed index rows directly on the
platform tables (so public-API / isolation tests do not depend on the full join + sync flow)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app.core import bot_challenge, rate_limit
from app.core.database import platform_session
from app.models.job_network import NetworkListing, NetworkMember
from tests.portal.conftest import Site, _site, origin, other_site, public, publish, site


@pytest.fixture(autouse=True)
def _jn_env() -> Iterator[None]:
    rate_limit.memory_backend().reset()
    bot_challenge.set_verifier(None)
    yield
    bot_challenge.set_verifier(None)
    rate_limit.memory_backend().reset()


async def make_member(
    tenant_id: uuid.UUID, slug: str, *, status: str = "active", verification_status: str = "verified"
) -> uuid.UUID:
    async with platform_session() as pdb:
        existing = (
            await pdb.execute(text("SELECT id FROM network_members WHERE tenant_id = :t"), {"t": str(tenant_id)})
        ).first()
        if existing is not None:
            return uuid.UUID(str(existing[0]))
        member = NetworkMember(
            id=uuid.uuid4(), tenant_id=tenant_id, public_slug=slug, display_name=f"{slug} Agency",
            careers_base_url=f"https://{slug}.careers.example", status=status,
            verification_status=verification_status, joined_at=datetime.now(UTC),
        )
        pdb.add(member)
        await pdb.commit()
        return member.id


async def add_listing(
    member_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    title: str = "Senior Data Engineer",
    status: str = "listed",
    city: str | None = "Bengaluru",
    pay_min: float | None = None,
    skills: list[str] | None = None,
) -> uuid.UUID:
    listing_id = uuid.uuid4()
    async with platform_session() as pdb:
        pdb.add(
            NetworkListing(
                id=listing_id, member_id=member_id, tenant_id=tenant_id, posting_ref=uuid.uuid4(), status=status,
                hold_reasons=[], title=title, description_html="<p>Build pipelines.</p>",
                description_text="Build pipelines with PySpark and Azure across teams.", city=city,
                location_hidden=city is None, skills=skills or ["python", "spark"], pay_min=pay_min,
                pay_currency=("INR" if pay_min else None), pay_period=("year" if pay_min else None),
                apply_url=f"https://x.careers.example/jobs/{listing_id}/apply?src=job_network&ref={listing_id}",
                posting_url=f"https://x.careers.example/jobs/{listing_id}", published_at=datetime.now(UTC),
                listed_at=datetime.now(UTC), content_sha256="0" * 64,
            )
        )
        await pdb.flush()
        await pdb.execute(
            text(
                "UPDATE network_listings SET search_tsv = "
                "setweight(to_tsvector('simple', coalesce(title,'')),'A') || "
                "setweight(to_tsvector('simple', coalesce(description_text,'')),'C') WHERE id = :id"
            ),
            {"id": str(listing_id)},
        )
        await pdb.commit()
    return listing_id


async def cleanup(tenant_id: uuid.UUID) -> None:
    async with platform_session() as pdb:
        await pdb.execute(text("DELETE FROM network_members WHERE tenant_id = :t"), {"t": str(tenant_id)})
        await pdb.commit()
