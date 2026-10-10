"""The projection only ever populates allow-listed public columns (Epic 75, spec A3 / A10)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.services.job_network.constants import PROJECTED_FIELDS
from app.services.job_network.listings import NetworkListingData
from tests.job_network.test_no_personal_data import NETWORK_PUBLIC_COLUMNS


def test_projected_fields_are_a_subset_of_the_column_allow_list() -> None:
    columns = NETWORK_PUBLIC_COLUMNS["network_listings"]
    assert columns >= PROJECTED_FIELDS, PROJECTED_FIELDS - columns


def test_network_listing_data_public_fields_within_allow_list() -> None:
    data = NetworkListingData(
        tenant_id=uuid.uuid4(), posting_ref=uuid.uuid4(), title="t", description_html="<p>x</p>",
        description_text="x", client_name=None, city=None, region=None, country=None, location_hidden=True,
        work_mode="remote", employment_type="permanent", experience_min=Decimal("1.0"),
        experience_max=Decimal("5.0"), skills=["python"], category=None, pay_min=None, pay_max=None,
        pay_currency=None, pay_period=None, apply_url="https://x/jobs/1/apply", posting_url="https://x/jobs/1",
        published_at=datetime.now(UTC), valid_through=None, content_sha256="0" * 64,
    )
    keys = set(data.public_fields())
    assert keys <= PROJECTED_FIELDS
    # routing keys are never part of the public field set.
    assert "tenant_id" not in keys and "posting_ref" not in keys
