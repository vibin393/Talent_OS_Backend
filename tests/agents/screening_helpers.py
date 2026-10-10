# ruff: noqa: E501
"""Epic 57 test helpers: a tenant with an open demand, a candidate (email + phone) and an Application owned by the
Tenant Admin; the ``agentic`` / ``agent_screening`` / ``ai_screening`` features on. Model calls use the scripted Fake LLM
keyed by prompt: ``agent_screening`` (the agent loop), ``screening_extract`` (extraction) and ``None`` (single
gateway calls such as the injection pre-screen)."""

import dataclasses
import re
import uuid
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.permissions import CATALOG, Principal, Scope
from app.models.agent_runtime import AgentRun, AgentToolCall
from app.models.screening_agent import ScreeningConversation, ScreeningTurn
from app.services import ai
from app.services.ai.fake_llm import FakeTurn
from tests.modules.identity.conftest import Member
from tests.modules.pipeline.helpers import _porg, add_one, create_candidate

API = "/api/v1"
BENIGN = FakeTurn("emit_result", result={"verdict": "benign", "reasons": []})
LINK_RE = re.compile(r"/l/([A-Za-z0-9_\-]{20,})")


@dataclasses.dataclass
class SOrg:
    tenant_id: uuid.UUID
    admin: Member
    demand_id: uuid.UUID
    candidate_id: uuid.UUID
    application_id: uuid.UUID

    def ctx(self) -> RequestContext:
        return RequestContext(
            tenant_id=self.tenant_id,
            principal=Principal(
                type="user", id=self.admin.user_id, tenant_id=self.tenant_id,
                permissions=dict.fromkeys(CATALOG, Scope.TENANT),
            ),
        )  # fmt: skip


async def make_sorg(app: FastAPI, name: str = "Ada Admin", **candidate: Any) -> SOrg:
    org = await _porg(app, name)
    cand = await create_candidate(org.admin, "Priya Raman", **candidate)
    app_id = await add_one(org.admin, org.demand_id, cand["id"])
    async with tenant_session(org.tenant_id) as db:
        await ai.set_features(db, org.tenant_id, {"agentic": True, "agent_screening": True, "ai_screening": True})
    return SOrg(org.tenant_id, org.admin, uuid.UUID(org.demand_id), uuid.UUID(cand["id"]), uuid.UUID(app_id))


def wait(conv_id: uuid.UUID) -> FakeTurn:
    return FakeTurn("await_event", wait={"condition": "reply", "ref": str(conv_id), "hours": 168, "reason": "waiting for the candidate"})


def calls(*items: tuple[str, dict[str, Any]]) -> FakeTurn:
    return FakeTurn("tool_calls", calls=tuple(items))


async def conv_of(org: SOrg, conv_id: uuid.UUID) -> ScreeningConversation:
    async with tenant_session(org.tenant_id) as db:
        row = await db.get(ScreeningConversation, conv_id)
        assert row is not None
        db.expunge(row)
        return row


async def turns_of(org: SOrg, conv_id: uuid.UUID) -> list[ScreeningTurn]:
    async with tenant_session(org.tenant_id) as db:
        rows = list(
            (await db.execute(select(ScreeningTurn).where(ScreeningTurn.conversation_id == conv_id).order_by(ScreeningTurn.seq))).scalars()
        )
        for r in rows:
            db.expunge(r)
        return rows


async def tool_calls_of(org: SOrg, run_ids: list[uuid.UUID] | None = None) -> list[AgentToolCall]:
    async with tenant_session(org.tenant_id) as db:
        stmt = select(AgentToolCall).order_by(AgentToolCall.created_at)
        if run_ids is not None:
            stmt = stmt.where(AgentToolCall.run_id.in_(run_ids))
        rows = list((await db.execute(stmt)).scalars())
        for r in rows:
            db.expunge(r)
        return rows


async def runs_of(org: SOrg, conv_id: uuid.UUID) -> list[AgentRun]:
    async with tenant_session(org.tenant_id) as db:
        rows = list(
            (await db.execute(select(AgentRun).where(AgentRun.conversation_id == conv_id).order_by(AgentRun.created_at))).scalars()
        )
        for r in rows:
            db.expunge(r)
        return rows


async def questions(org: SOrg, conv: ScreeningConversation) -> list[dict[str, Any]]:
    from app.models.screening_agent import ScreeningQuestionnaireVersion

    async with tenant_session(org.tenant_id) as db:
        ver = await db.get(ScreeningQuestionnaireVersion, conv.questionnaire_version_id)
        assert ver is not None
        return list(ver.questions)
