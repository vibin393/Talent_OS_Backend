# ruff: noqa: E501
"""Epic 58 test helpers: the Epic 57 org (open demand, candidate Priya Raman, an Application owned by the Tenant Admin)
with the ``agentic`` / ``agent_voice_screening`` / ``agent_screening`` features on. Model calls use the scripted Fake LLM
keyed by prompt: ``agent_voice_screening`` (the agent loop) and ``screening_extract`` (Epic 57 extraction)."""

import re
import uuid
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from app.core.database import tenant_session
from app.models.comms import Email
from app.models.voice import VoiceSession
from app.services import ai
from app.services import voice_service as svc
from app.services.ai.fake_llm import FakeTurn
from tests.agents.screening_helpers import SOrg, make_sorg
from tests.modules.identity.conftest import Member, csrf

API = "/api/v1"
LINK_RE = re.compile(r"/l/([A-Za-z0-9_\-]{20,})")
BENIGN = FakeTurn("emit_result", result={"verdict": "benign", "reasons": []})
TRANSCRIPT = """WEBVTT

00:00:01.000 --> 00:00:04.000
<v Recruiter>Are you interested in this role?

00:00:05.000 --> 00:00:09.000
<v Candidate>Yes, I am very interested.

00:00:10.000 --> 00:00:13.000
<v Recruiter>What is your notice period?

00:00:14.000 --> 00:00:20.000
<v Candidate>My notice period is 60 days.
"""
UNLABELLED = """Speaker 1: Are you interested in this role?
Speaker 2: Yes, very interested.
"""


async def make_vorg(app: FastAPI, **features: bool) -> SOrg:
    org = await make_sorg(app)
    async with tenant_session(org.tenant_id) as db:
        from app.services.recording_policy.gate import seed_tenant_defaults

        await seed_tenant_defaults(db)  # tenant provisioning seeds IN / candidate_consent (test tenants skip it)
        await ai.set_features(
            db,
            org.tenant_id,
            {"agentic": True, "agent_voice_screening": True, "agent_screening": True, **features},
        )
    return org


async def agent_off(org: SOrg) -> None:
    async with tenant_session(org.tenant_id) as db:
        await ai.set_features(db, org.tenant_id, {"agent_voice_screening": False})


def h(m: Member) -> dict[str, str]:
    return csrf(m.client)


def calls(*items: tuple[str, dict[str, Any]]) -> FakeTurn:
    return FakeTurn("tool_calls", calls=tuple(items))


def done(outcome: str = "done", **extra: Any) -> FakeTurn:
    return FakeTurn("emit_result", result={"outcome": outcome, "summary": "ok", **extra})


async def session(org: SOrg, sid: uuid.UUID) -> VoiceSession:
    async with tenant_session(org.tenant_id) as db:
        row = await db.get(VoiceSession, sid)
        assert row is not None
        db.expunge(row)
        return row


async def last_link(org: SOrg) -> str:
    async with tenant_session(org.tenant_id) as db:
        mail = (await db.execute(select(Email).order_by(Email.created_at.desc()).limit(1))).scalar_one()
        m = LINK_RE.search(mail.body_html)
        assert m is not None
        return m.group(1)


async def request(
    org: SOrg, mode: str = "upload", provider_key: str | None = "manual", fake: Any = None, turns: Any = None
) -> uuid.UUID:
    """Request a voice screen through the service; ``turns(sid)`` are queued for the agent before the commit."""
    async with tenant_session(org.tenant_id) as db:
        out = await svc.request(org.ctx(), db, org.application_id, mode=mode, provider_key=provider_key)
        if fake is not None and turns is not None:
            fake._by_prompt["agent_voice_screening"].extend(turns(out.id))
        return out.id


async def consent(
    org: SOrg, *, country: str = "IN", voice: bool = True, recording: bool = True
) -> dict[str, Any]:
    token = await last_link(org)
    return await svc.consent_decide(token, country, None, voice, recording)


async def grant(org: SOrg, *purposes: str) -> None:
    from app.core.context import RequestContext
    from app.services.privacy import consent as pc

    async with tenant_session(org.tenant_id) as db:
        for p in purposes:
            await pc.grant_consent(
                RequestContext(tenant_id=org.tenant_id, actor_kind="system"),
                db,
                org.candidate_id,
                p,
                source="voice_consent_link",
            )


async def upload_transcript(org: SOrg, sid: uuid.UUID, text: str = TRANSCRIPT, *, attest: bool = True) -> Any:
    return await org.admin.client.post(
        f"{API}/voice-sessions/{sid}/transcript", files={"file": ("call.vtt" if text.startswith("WEBVTT") else "call.txt", text.encode(), "text/plain")},
        data={"consent_obtained": str(attest).lower(), "policy_followed": str(attest).lower()}, headers=h(org.admin),
    )  # fmt: skip


async def upload_recording(
    org: SOrg, sid: uuid.UUID, data: bytes | None = None, ctype: str = "audio/wav", name: str = "call.wav"
) -> Any:
    from app.services.connectors.voice.fake import silence_wav

    return await org.admin.client.post(
        f"{API}/voice-sessions/{sid}/recording", files={"file": (name, data if data is not None else silence_wav(), ctype)},
        data={"consent_obtained": "true", "policy_followed": "true"}, headers=h(org.admin),
    )  # fmt: skip


async def questions(org: SOrg, sid: uuid.UUID) -> list[dict[str, Any]]:
    from app.models.screening_agent import ScreeningQuestionnaireVersion

    row = await session(org, sid)
    async with tenant_session(org.tenant_id) as db:
        ver = (
            await db.execute(
                select(ScreeningQuestionnaireVersion).where(
                    ScreeningQuestionnaireVersion.questionnaire_id == row.questionnaire_id,
                    ScreeningQuestionnaireVersion.version == row.questionnaire_version,
                )
            )
        ).scalar_one()
        return list(ver.questions)


def q(qs: list[dict[str, Any]], key: str) -> str:
    return next(x["id"] for x in qs if x["key"] == key)
