"""Pure time helpers for interviews (AD-14): local wall time + IANA zone -> aware UTC, and display strings.

* ``to_utc(local, tz)`` -- ``local`` is a naive ``YYYY-MM-DDTHH:MM`` (string or naive datetime). Unknown zones
  raise 422 ``invalid_timezone``; a wall time that does not exist (DST gap) raises 422
  ``nonexistent_local_time`` (detected by a UTC round trip). Ambiguous times (DST fold) use ``fold=0``.
* ``render_for(dt_utc, tz)`` -> ``"Tue 6 Oct 2026, 10:30 IST (UTC+05:30)"``.
"""

import functools
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from app.core.errors import ValidationFailed


@functools.lru_cache(maxsize=1)
def _zones() -> frozenset[str]:
    return frozenset(available_timezones()) | {"UTC"}


def is_valid_timezone(tz: str | None) -> bool:
    return bool(tz) and tz in _zones()


def zone(tz: str) -> ZoneInfo:
    if not is_valid_timezone(tz):
        raise ValidationFailed("Unknown time zone", code="invalid_timezone", details={"timezone": tz})
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - listed but not loadable
        raise ValidationFailed(
            "Unknown time zone", code="invalid_timezone", details={"timezone": tz}
        ) from exc


def safe_zone(tz: str | None, fallback: str = "UTC") -> ZoneInfo:
    """Return a loadable zone for display: ``tz`` if valid, else ``fallback``, else UTC (never raises)."""
    for candidate in (tz, fallback):
        if candidate and is_valid_timezone(candidate):
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return ZoneInfo("UTC")


def parse_local(local: str | datetime) -> datetime:
    """``YYYY-MM-DDTHH:MM[:SS]`` -> naive datetime (422 ``invalid_local_time`` otherwise)."""
    if isinstance(local, datetime):
        if local.tzinfo is not None:
            raise ValidationFailed("start_local must not carry an offset", code="invalid_local_time")
        return local.replace(microsecond=0)
    try:
        value = datetime.fromisoformat(str(local).strip())
    except ValueError as exc:
        raise ValidationFailed("start_local must be YYYY-MM-DDTHH:MM", code="invalid_local_time") from exc
    if value.tzinfo is not None:
        raise ValidationFailed("start_local must not carry an offset", code="invalid_local_time")
    return value.replace(microsecond=0)


def to_utc(local: str | datetime, tz: str) -> datetime:
    """Wall time ``local`` in ``tz`` -> aware UTC. 422 ``invalid_timezone`` / ``nonexistent_local_time``."""
    zi = zone(tz)
    naive = parse_local(local)
    aware = naive.replace(tzinfo=zi, fold=0)
    utc = aware.astimezone(UTC)
    back = utc.astimezone(zi).replace(tzinfo=None)
    if back != naive:
        raise ValidationFailed(
            "That local time does not exist in this time zone (daylight saving change)",
            code="nonexistent_local_time",
            details={"start_local": naive.isoformat(timespec="minutes"), "timezone": tz},
        )
    return utc


def _offset(delta: timedelta | None) -> str:
    total = int((delta or timedelta(0)).total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def abbreviation(dt_utc: datetime, tz: str) -> str:
    local = dt_utc.astimezone(safe_zone(tz))
    name = local.tzname() or ""
    # Zones without a letter abbreviation report "+0530"-style names; the offset part already says it.
    return name if name and name[0].isalpha() else ""


def render_for(dt_utc: datetime, tz: str) -> str:
    """``"Tue 6 Oct 2026, 10:30 IST (UTC+05:30)"`` -- ``dt_utc`` shown in ``tz`` with abbreviation + offset."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=UTC)
    local = dt_utc.astimezone(safe_zone(tz))
    abbr = abbreviation(dt_utc, tz)
    head = f"{local:%a} {local.day} {local:%b %Y}, {local:%H:%M}"
    offset = _offset(local.utcoffset())
    return f"{head} {abbr} ({offset})" if abbr else f"{head} ({offset})"


def render_time(dt_utc: datetime, tz: str) -> str:
    """``"10:30"`` in ``tz``."""
    return f"{dt_utc.astimezone(safe_zone(tz)):%H:%M}"


def local_iso(dt_utc: datetime, tz: str) -> str:
    """Naive local ``YYYY-MM-DDTHH:MM`` of ``dt_utc`` in ``tz`` (form prefill)."""
    return dt_utc.astimezone(safe_zone(tz)).replace(tzinfo=None).isoformat(timespec="minutes")
