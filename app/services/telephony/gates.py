# ruff: file-ignore[line-too-long, non-imperative-mood]
"""The pre-call gate (Epic 76, story 76.2, spec A3). Every check runs in Talent OS before anything is sent to the
provider; the first failure wins. Callback content never decides consent or policy.

| # | Check | Refusal code |
|---|---|---|
| 1 | a human ``user`` principal (``app``, ``api_key``, ``agent``, ``ai``, ``workflow``, ``system`` refused) | 403 ``human_only_action`` |
| 2 | Ecosystem Kill Switch | ``ecosystem_suspended`` |
| 3 | sandbox tenant -> only the ``fake`` adapter (adapter swap by the connector registry, no refusal) | -- |
| 4 | an active voice provider Installation + caller ID for the legal entity | ``no_voice_provider`` |
| 5 | the caller has a verified calling phone | ``call_phone_not_verified`` |
| 6 | Candidate not erased and ``voice_screening`` consent active | ``voice_consent_required`` |
| 7 | Do-Not-Call (``channel_opt_outs`` channel ``voice``) | ``do_not_call`` |
| 8 | calling hours in the Candidate's timezone | ``outside_calling_hours`` |
| 9 | the caller's open calls < ``max_concurrent_per_user`` | ``call_in_progress`` |
| 10 | daily cost cap: today + one max-length call <= cap | ``cost_cap_reached`` |
| 11 | a callable E.164 phone for the Candidate | ``no_phone`` |
"""

import dataclasses
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import actor_from_ctx, is_human
from app.core.context import RequestContext
from app.core.errors import Forbidden
from app.core.permissions import Principal
from app.models.connectors import ProviderConnectorConfig
from app.models.telephony import (
    OPEN_CALL_STATUSES,
    TelephonyNumber,
    TelephonySettings,
    UserCallPhone,
    VoiceCall,
)
from app.models.voice import VoiceSession
from app.services.telephony import common, costs, numbers, user_phones

MESSAGES: dict[str, str] = {
    "human_only_action": "Only a person can place a call",
    "ecosystem_suspended": "External access is paused by your admin",
    "no_voice_provider": "No calling provider set up — call outside Talent OS",
    "call_phone_not_verified": "Add and verify your calling phone first",
    "voice_consent_required": "Waiting for the candidate's consent",
    "do_not_call": "Do not call — the candidate asked not to be called",
    "outside_calling_hours": "Outside the candidate's calling hours",
    "call_in_progress": "You already have a call in progress",
    "cost_cap_reached": "Today's calling budget is used up. Calls in progress continue. You can call outside Talent OS and upload the recording.",
    "no_phone": "The candidate has no phone number to call",
}


@dataclasses.dataclass
class GateResult:
    ok: bool
    code: str | None = None
    details: dict[str, Any] = dataclasses.field(default_factory=dict)
    config: ProviderConnectorConfig | None = None
    number: TelephonyNumber | None = None
    phone_row: UserCallPhone | None = None
    candidate_phone: str | None = None
    settings: TelephonySettings | None = None
    today_minor: int = 0

    @property
    def message(self) -> str | None:
        return MESSAGES.get(self.code or "") if self.code else None  # NOSONAR

    def refuse(self, code: str, **details: Any) -> "GateResult":
        self.ok = False
        self.code = code
        self.details.update(details)
        return self


def require_caller(ctx: RequestContext) -> uuid.UUID:
    """Check 1 (★): only a human ``user`` principal places a call."""
    p = ctx.principal
    if not isinstance(p, Principal) or p.type != "user" or not is_human(actor_from_ctx(ctx)):
        raise Forbidden("Only a person can place a call", code="human_only_action")
    return p.id


async def provider_config(db: AsyncSession, legal_entity_id: uuid.UUID | None) -> ProviderConnectorConfig | None:
    rows = list(
        (
            await db.execute(
                select(ProviderConnectorConfig).where(
                    ProviderConnectorConfig.category == common.CATEGORY,
                    ProviderConnectorConfig.status == "active",
                    ProviderConnectorConfig.installation_id.is_not(None),
                    ProviderConnectorConfig.provider_key.in_(common.TELEPHONY_PROVIDERS),
                )
            )
        ).scalars()
    )
    if legal_entity_id is not None:
        found = next((r for r in rows if r.legal_entity_id == legal_entity_id), None)
        if found is not None:
            return found
    return next((r for r in rows if r.legal_entity_id is None), None) or (rows[0] if len(rows) == 1 else None)


async def candidate_phones(db: AsyncSession, candidate_id: uuid.UUID) -> list[str]:
    from app.models.candidates import CandidatePhone

    rows = (
        await db.execute(
            select(CandidatePhone.phone_e164)
            .where(CandidatePhone.candidate_id == candidate_id)
            .order_by(CandidatePhone.is_primary.desc(), CandidatePhone.created_at)
        )
    ).scalars()
    return [p for p in (common.normalize_e164(r) for r in rows) if p]


async def candidate_zone(db: AsyncSession, candidate_id: uuid.UUID) -> Any:
    from app.models.candidates import Candidate

    loc = (await db.execute(select(Candidate.location).where(Candidate.id == candidate_id))).scalar_one_or_none()
    name = loc.get("timezone") if isinstance(loc, dict) else None
    return common.zone(name) if name else await common.tenant_timezone(db)


def next_window(now_local: datetime, settings: TelephonySettings) -> datetime | None:
    """None when ``now_local`` is inside the window, else the next window start (aware, local zone)."""
    start, end = settings.call_window_start, settings.call_window_end
    t = now_local.timetz().replace(tzinfo=None)
    if start <= t < end:
        return None
    day = now_local.date() if t < start else now_local.date() + timedelta(days=1)
    return datetime.combine(day, start, tzinfo=now_local.tzinfo)


async def is_erased(db: AsyncSession, candidate_id: uuid.UUID) -> bool:
    from app.models.candidates import Candidate

    erased = (await db.execute(select(Candidate.erased_at).where(Candidate.id == candidate_id))).scalar_one_or_none()
    return erased is not None


async def do_not_call(db: AsyncSession, phone: str | None) -> bool:
    from app.services.messaging.opt_outs import is_opted_out

    return bool(phone) and await is_opted_out(db, "voice", phone)


async def check(
    ctx: RequestContext,
    db: AsyncSession,
    session: VoiceSession,
    *,
    phone_choice: str = "primary",
    legal_entity_id: uuid.UUID | None = None,
) -> GateResult:
    """Run checks 2..11 (check 1 is ``require_caller``). Pure-ish: reads only."""
    from app.services import ecosystem_service
    from app.services.telephony import settings as settings_mod
    from app.services.voice.common import PURPOSE_CALL, has_consent

    res = GateResult(ok=True)
    user_id = require_caller(ctx)
    res.settings = s = await settings_mod.row(db)
    if (await ecosystem_service.is_suspended(common.tenant_of(db))).suspended:
        return res.refuse("ecosystem_suspended")
    res.config = await provider_config(db, legal_entity_id)
    res.number = await numbers.active_for(db, legal_entity_id)
    if res.config is None or res.number is None:
        return res.refuse("no_voice_provider")
    res.phone_row = await user_phones.verified_row(db, user_id)
    if res.phone_row is None:
        return res.refuse("call_phone_not_verified")
    if await is_erased(db, session.candidate_id) or not await has_consent(db, session.candidate_id, PURPOSE_CALL):
        return res.refuse("voice_consent_required")
    phones = await candidate_phones(db, session.candidate_id)
    idx = 1 if phone_choice == "secondary" else 0
    res.candidate_phone = phones[idx] if len(phones) > idx else None
    if await do_not_call(db, res.candidate_phone) or any([await do_not_call(db, p) for p in phones]):
        return res.refuse("do_not_call")
    tz = await candidate_zone(db, session.candidate_id)
    nxt = next_window(common.now().astimezone(tz), s)
    if nxt is not None:
        return res.refuse("outside_calling_hours", next_window_at=nxt.isoformat())
    open_calls = (
        await db.execute(
            select(func.count(VoiceCall.id)).where(VoiceCall.placed_by == user_id, VoiceCall.status.in_(OPEN_CALL_STATUSES))
        )
    ).scalar_one()
    if int(open_calls) >= s.max_concurrent_per_user:
        return res.refuse("call_in_progress")
    res.today_minor = await costs.today(db)
    reserve = s.max_call_minutes * res.number.rate_per_minute_minor
    if res.today_minor + reserve > s.daily_cost_cap_minor or (s.daily_cost_cap_minor == 0 and reserve == 0 and res.today_minor > 0):
        return res.refuse("cost_cap_reached")
    if res.candidate_phone is None:
        return res.refuse("no_phone")
    return res


async def recording_decision(
    ctx: RequestContext, db: AsyncSession, session: VoiceSession, *, can_notice: bool
) -> tuple[bool, str | None]:
    """Recording only when the Recording Policy allows it *now* (never from callback content), ``call_recording``
    consent is active and the provider can play the notice. Returns ``(record, unavailable_reason)``."""
    from app.services.recording_policy import gate
    from app.services.voice.common import PURPOSE_RECORDING, has_consent, reader_ctx

    jurisdiction = (session.confirmed_country, session.confirmed_region) if session.confirmed_country else None
    decision = await gate.check(
        reader_ctx(session.tenant_id, session.owner_user_id, ctx), db, purpose=PURPOSE_RECORDING,  # type: ignore[arg-type]
        candidate_id=session.candidate_id, confirmed_jurisdiction=jurisdiction,
    )  # fmt: skip
    if decision.rule in ("not_permitted", "unknown"):
        return False, "policy_not_permitted"
    if not await has_consent(db, session.candidate_id, PURPOSE_RECORDING) or not decision.recording_allowed:
        return False, "consent_missing"
    if not can_notice:
        return False, "notice_unavailable"
    return True, None
