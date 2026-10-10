# ruff: noqa: E501
"""Visitor reports (Epic 75, spec A5; FR-389). Anonymous: the reporter's identity is never collected. ``ip_hash`` is an
HMAC of the IP with a monthly-rotated platform key, used only for rate limiting, purged after 30 days. 3 open reports on
one listing within 7 days hold the listing pending moderator review. Always 202 (no enumeration)."""

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import platform_session
from app.core.settings import get_settings
from app.models.job_network import REPORT_REASONS, NetworkListing, NetworkReport
from app.services.job_network import checks
from app.services.job_network.constants import (
    REPORT_HOLD_THRESHOLD,
    REPORT_HOLD_WINDOW_DAYS,
    REPORT_RETENTION_DAYS,
)


def ip_hash(ip: str | None) -> str:
    """HMAC-SHA256(ip, platform key + current month). Rotated monthly; rate limiting only."""
    key = (get_settings().network_report_ip_key or "talent-os-dev-network-report").encode()
    month = datetime.now(UTC).strftime("%Y%m")
    return hmac.new(key, f"{month}:{ip or 'unknown'}".encode(), hashlib.sha256).hexdigest()


async def _daily_count(pdb: AsyncSession, ip_h: str) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=1)
    return int(
        (
            await pdb.execute(
                select(func.count())
                .select_from(NetworkReport)
                .where(NetworkReport.ip_hash == ip_h, NetworkReport.created_at >= cutoff)
            )
        ).scalar_one()
    )


async def create(listing_id: uuid.UUID, *, reason: str, details: str | None, ip: str | None) -> None:
    """Record a report. Invalid reason / unknown listing / over the daily cap are swallowed (always 202)."""
    if reason not in REPORT_REASONS:
        return
    ip_h = ip_hash(ip)
    async with platform_session() as pdb:
        listing = (
            await pdb.execute(select(NetworkListing.id, NetworkListing.status).where(NetworkListing.id == listing_id))
        ).first()
        if listing is None:
            return
        if await _daily_count(pdb, ip_h) >= 5:
            return
        pdb.add(
            NetworkReport(
                id=uuid.uuid4(), listing_id=listing_id, reason=reason,
                details=checks.redact_contacts(details), ip_hash=ip_h, status="open",
                created_at=datetime.now(UTC),
            )
        )
        await pdb.flush()
        await _maybe_hold(pdb, listing_id)
        await pdb.commit()


async def _maybe_hold(pdb: AsyncSession, listing_id: uuid.UUID) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=REPORT_HOLD_WINDOW_DAYS)
    open_reports = int(
        (
            await pdb.execute(
                select(func.count())
                .select_from(NetworkReport)
                .where(
                    NetworkReport.listing_id == listing_id,
                    NetworkReport.status == "open",
                    NetworkReport.created_at >= cutoff,
                )
            )
        ).scalar_one()
    )
    if open_reports >= REPORT_HOLD_THRESHOLD:
        listing = (
            await pdb.execute(
                select(NetworkListing).where(NetworkListing.id == listing_id).with_for_update()
            )
        ).scalar_one_or_none()
        if listing is not None and listing.status == "listed":
            listing.status = "held"
            listing.hold_reasons = sorted({*(listing.hold_reasons or []), "reported"})


async def purge_expired() -> int:
    """Delete report rows older than the 30-day retention window (``ip_hash`` purged)."""
    from sqlalchemy import delete

    cutoff = datetime.now(UTC) - timedelta(days=REPORT_RETENTION_DAYS)
    async with platform_session() as pdb:
        result = await pdb.execute(delete(NetworkReport).where(NetworkReport.created_at < cutoff))
        await pdb.commit()
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = ["create", "ip_hash", "purge_expired"]
