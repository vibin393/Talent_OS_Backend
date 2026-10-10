# ruff: noqa: E501
"""Metering registrations (autodiscovered): an Installation change (e.g. a new ``rate_tier``) busts the cached
Rate Tier of that Installation. ``api_request_log`` registers itself as a partitioned table in ``partitions``."""

from app.core import events
from app.events.ecosystem import InstallationChanged
from app.services.metering import enforce


def _bust_tier(event: InstallationChanged) -> None:
    enforce.clear_tier_cache(event.tenant_id)


events.subscribe(InstallationChanged, _bust_tier)
