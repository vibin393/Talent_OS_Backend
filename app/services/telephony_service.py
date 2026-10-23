# ruff: file-ignore[line-too-long]
"""Facade of the Phase 5 telephony package (Epic 76, D-21). Routers and other domains call these, never the package
internals.

* Calls: ``call_options``, ``place_call`` (★ human only), ``hangup``, ``get_call``, ``list_calls``, ``do_not_call``.
* Recruiter phones: ``my_phone``, ``start_phone``, ``verify_phone``, ``remove_phone``, ``attest_phone``.
* Settings / caller IDs: ``get_settings``, ``update_settings``, ``list_numbers``, ``create_number``,
  ``update_number``, ``deactivate_number``. Usage: ``usage``.
* Provider endpoints: ``answer`` (TwiML / Plivo XML), ``relay_exotel``.
"""

from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.telephony import UsageOut, UsageRow
from app.services.telephony import costs
from app.services.telephony.calls import call_options, do_not_call, get_call, hangup, list_calls, place_call
from app.services.telephony.ingress import answer, relay_exotel
from app.services.telephony.numbers import create as create_number
from app.services.telephony.numbers import deactivate as deactivate_number
from app.services.telephony.numbers import list_numbers
from app.services.telephony.numbers import update as update_number
from app.services.telephony.settings import get as get_settings
from app.services.telephony.settings import update as update_settings
from app.services.telephony.user_phones import attest as attest_phone
from app.services.telephony.user_phones import mine as my_phone
from app.services.telephony.user_phones import remove as remove_phone
from app.services.telephony.user_phones import start as start_phone
from app.services.telephony.user_phones import verify as verify_phone


async def usage(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    date_from: date | None = None,
    date_to: date | None = None,
    group_by: str = "day",
) -> UsageOut:
    from app.services.telephony import settings as settings_mod

    _, _, today = await costs.day_bounds(db)
    hi = date_to or today
    lo = date_from or (hi - timedelta(days=29))
    rows = await costs.usage(db, lo, hi, group_by)
    labels: dict[str, str] = {}
    if group_by == "user":
        import uuid

        ids = []
        for r in rows:
            try:
                ids.append(uuid.UUID(r["key"]))
            except ValueError:
                continue
        labels = await costs.user_names(db, ids)
    s = await settings_mod.row(db)
    spent = await costs.today(db)
    return UsageOut(
        from_date=lo,
        to_date=hi,
        group_by=group_by,  # type: ignore[arg-type]
        rows=[UsageRow(label=labels.get(r["key"]), **r) for r in rows],
        today_cost_minor=spent,
        daily_cost_cap_minor=s.daily_cost_cap_minor,
        today_cost_pct=costs.pct(spent, s.daily_cost_cap_minor),
    )


__all__ = [
    "answer",
    "attest_phone",
    "call_options",
    "create_number",
    "deactivate_number",
    "do_not_call",
    "get_call",
    "get_settings",
    "hangup",
    "list_calls",
    "list_numbers",
    "my_phone",
    "place_call",
    "relay_exotel",
    "remove_phone",
    "start_phone",
    "update_number",
    "update_settings",
    "usage",
    "verify_phone",
]
