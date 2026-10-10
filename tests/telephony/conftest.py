# ruff: noqa: E501, F401
"""Epic 76 telephony test harness: the Epic 58 voice org (Priya Raman with an Application) plus a Twilio Installation
(Fake mode: ``settings.mode='fake'``), a caller ID, a verified recruiter phone, consent, a callable candidate phone in
India and a frozen clock inside calling hours. Fakes only; no socket is opened."""

import dataclasses
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.integrations import manifest, manifest_sha256
from app.core.permissions import CATALOG, Principal, Scope
from app.core.settings import get_settings
from app.models.candidates import Candidate, CandidatePhone
from app.models.connectors import ProviderConnectorConfig
from tests.agents.conftest import _agent_env, _agents_registered
from tests.agents.screening_helpers import SOrg
from tests.agents.voice.helpers import grant, make_vorg
from tests.epic3 import _isolated_files, _no_real_ai

CANDIDATE_PHONE = "+919811110001"
AGENT_PHONE = "+919800000001"
NOON_IST = datetime(2026, 10, 12, 6, 30, tzinfo=UTC)  # 12:00 IST, a Monday


@dataclasses.dataclass
class TOrg:
    org: SOrg
    installation_id: uuid.UUID
    config_id: uuid.UUID
    number_id: uuid.UUID
    otp: list[str]

    @property
    def tenant_id(self) -> uuid.UUID:
        return self.org.tenant_id

    def ctx(self, **perms: Scope) -> RequestContext:
        return self.org.ctx()


def human(org: SOrg, *, principal_type: str = "user") -> RequestContext:
    return RequestContext(
        tenant_id=org.tenant_id,
        principal=Principal(type=principal_type, id=org.admin.user_id, tenant_id=org.tenant_id, permissions=dict.fromkeys(CATALOG, Scope.TENANT)),  # type: ignore[arg-type]
    )


async def install(org: SOrg, key: str, secrets: dict[str, str], *, fake: bool = True) -> tuple[uuid.UUID, uuid.UUID]:
    from app.schemas.marketplace import InstallIn
    from app.services import marketplace_service

    data = InstallIn(
        kind="connector", connector_key=key, secrets=secrets, accountable_user_id=org.admin.user_id,
        accept_manifest_sha256=manifest_sha256(manifest(key)),
    )  # fmt: skip
    async with tenant_session(org.tenant_id) as db:
        out = await marketplace_service.install(human(org), db, data)
    async with tenant_session(org.tenant_id) as db:
        cfg = (await db.execute(select(ProviderConnectorConfig).where(ProviderConnectorConfig.installation_id == out.id))).scalar_one()
        if fake:
            cfg.settings = {**(cfg.settings or {}), "mode": "fake"}
        return out.id, cfg.id


@pytest.fixture(autouse=True)
def _telephony_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from app.services.connectors.stt.fake import FakeSpeechToText
    from app.services.connectors.voice.fake import FakeBridgedVoiceProvider
    from app.services.telephony import common, costs
    from app.services.voice import uploads

    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    monkeypatch.setattr(get_settings(), "public_webhook_base_url", "https://hooks.example.com")
    FakeBridgedVoiceProvider.reset()
    FakeSpeechToText.reset()
    costs.reset_alerts()
    uploads.set_transcription_provider(None)
    common.set_clock(lambda: NOON_IST)
    yield
    common.set_clock(None)
    from app.services.telephony import user_phones

    user_phones.set_otp_sender(None)


@pytest.fixture
async def torg(app: FastAPI) -> AsyncIterator[TOrg]:
    from app.schemas.telephony import TelephonyNumberIn
    from app.services import telephony_service as svc
    from app.services.telephony import user_phones

    org = await make_vorg(app)
    otp: list[str] = []

    async def capture(ctx: Any, db: Any, phone: str, code: str) -> None:
        otp.append(code)

    user_phones.set_otp_sender(capture)
    inst, cfg = await install(org, "twilio", {"account_sid": "AC123", "auth_token": "twilio-auth-token"})
    async with tenant_session(org.tenant_id) as db:
        from sqlalchemy import delete

        await db.execute(delete(CandidatePhone).where(CandidatePhone.candidate_id == org.candidate_id))
        db.add(CandidatePhone(id=uuid.uuid4(), tenant_id=org.tenant_id, candidate_id=org.candidate_id, phone_e164=CANDIDATE_PHONE, is_primary=True))
        await db.execute(update(Candidate).where(Candidate.id == org.candidate_id).values(location={"country": "IN", "city": "Pune"}))
        num = await svc.create_number(human(org), db, TelephonyNumberIn(installation_id=inst, number_e164="+918040000000", label="Acme Bengaluru", rate_per_minute_minor=100))
        await svc.start_phone(human(org), db, AGENT_PHONE)
    async with tenant_session(org.tenant_id) as db:
        await svc.verify_phone(human(org), db, otp[-1])
    await grant(org, "voice_screening", "call_recording")
    yield TOrg(org, inst, cfg, num.id, otp)
    await org.admin.client.aclose()
