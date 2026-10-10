# ruff: noqa: E501
"""Epic 57 eval seeds: an Application of the org's candidate with a Screening Conversation (created without its own
run, the workflow path) and the Screening Agent enabled. Subject = the Application. Placeholders: ``{{conversation_id}}``,
``{{application_id}}``, ``{{q_interest}}``, ``{{q_notice}}``, ``{{q_expected}}``, ``{{random_id}}``.

Variants: ``screening_consented`` (default), ``screening_invited`` (consent pending), ``screening_posting`` (consented +
a live posting with ``work_mode=remote``), ``screening_whatsapp_closed`` (WhatsApp, session window closed),
``screening_burn_in`` (consented, agent in burn-in), ``screening_completed`` (completed with a P1 screening proposing a
candidate update)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from evals.fixtures import register_async_builder


async def _base(org: Any, status: str = "consented") -> dict[str, str]:
    from app.core.context import RequestContext
    from app.core.database import tenant_session
    from app.core.settings import get_settings
    from app.models.screening_agent import ScreeningQuestionnaireVersion
    from app.services import ai
    from app.services import screening_agent_service as svc
    from tests.modules.demands.helpers import create_client, open_demand
    from tests.modules.pipeline.helpers import add_one

    # Offline evals: the fast-tier extraction is a single gateway call, which needs a configured key (the client is the
    # Fake LLM; the test harness restores the setting after each case).
    if not get_settings().anthropic_api_key:
        get_settings().anthropic_api_key = "fake-eval-key"
    client_id = await create_client(org.admin)
    demand = await open_demand(org.admin, client_id)
    app_id = uuid.UUID(await add_one(org.admin, str(demand["id"]), str(org.candidate_id)))
    async with tenant_session(org.tenant_id) as db:
        await ai.set_features(db, org.tenant_id, {"agentic": True, "agent_screening": True, "ai_screening": True})
    async with tenant_session(org.tenant_id) as db:
        conv = await svc.conversations.start(RequestContext(tenant_id=org.tenant_id, actor_kind="system"), db, app_id, channel="link_chat", trigger_kind="workflow", run_id=uuid.uuid4())
        conv.current_run_id = None
        conv.invited_at = datetime.now(UTC)
        if status != "invited":
            conv.status = status
            conv.consented_at = datetime.now(UTC)
        ver = await db.get(ScreeningQuestionnaireVersion, conv.questionnaire_version_id)
        qs = {q["key"]: q["id"] for q in ver.questions}  # type: ignore[union-attr]
        await db.flush()
        cid = conv.id
    return {
        "conversation_id": str(cid), "application_id": str(app_id), "demand_id": str(demand["id"]),
        "q_interest": qs["interest"], "q_notice": qs["notice_period"], "q_expected": qs["expected_compensation"],
        "random_id": str(uuid.uuid4()), "subject_type": "application", "subject_id": str(app_id),
        "subject_candidate_ids": str(org.candidate_id), "candidate_id": str(org.candidate_id),
    }  # fmt: skip


async def consented(org: Any) -> dict[str, str]:
    return await _base(org)


async def invited(org: Any) -> dict[str, str]:
    return await _base(org, "invited")


async def posting(org: Any) -> dict[str, str]:
    from sqlalchemy import select

    from app.core.database import tenant_session
    from app.models.demands import Demand, DemandPosting, JdVersion

    values = await _base(org)
    async with tenant_session(org.tenant_id) as db:
        demand = await db.get(Demand, uuid.UUID(values["demand_id"]))
        assert demand is not None
        demand.work_mode = "remote"
        jd = (await db.execute(select(JdVersion).where(JdVersion.demand_id == demand.id).limit(1))).scalar_one_or_none()
        if jd is None:
            jd = JdVersion(id=uuid.uuid4(), tenant_id=org.tenant_id, demand_id=demand.id, version_no=1, kind="full", content="A remote role.", author_type="user")
            db.add(jd)
            await db.flush()
        jd_id = jd.id
        existing = (await db.execute(select(DemandPosting).where(DemandPosting.demand_id == demand.id))).scalar_one_or_none()
        if existing is not None:
            existing.is_live = True
        else:
            db.add(DemandPosting(id=uuid.uuid4(), tenant_id=org.tenant_id, demand_id=demand.id, jd_version_id=jd_id, is_live=True))
    return values


async def whatsapp_closed(org: Any) -> dict[str, str]:
    from app.core.database import tenant_session
    from app.models.screening_agent import ScreeningConversation

    values = await _base(org)
    async with tenant_session(org.tenant_id) as db:
        conv = await db.get(ScreeningConversation, uuid.UUID(values["conversation_id"]))
        assert conv is not None
        conv.channel = "whatsapp"
        conv.wa_window_until = datetime.now(UTC) - timedelta(hours=1)
    return values


async def burn_in(org: Any) -> dict[str, str]:
    from sqlalchemy import select

    from app.core.database import tenant_session
    from app.models.agent_runtime import AgentConfig

    values = await _base(org)
    async with tenant_session(org.tenant_id) as db:
        row = (await db.execute(select(AgentConfig).where(AgentConfig.agent_key == "screening"))).scalar_one_or_none()
        if row is None:
            row = AgentConfig(id=uuid.uuid4(), tenant_id=org.tenant_id, agent_key="screening", limits={}, triggers_enabled={})
            db.add(row)
        row.burn_in_until = datetime.now(UTC) + timedelta(days=14)
    return values


async def completed(org: Any) -> dict[str, str]:
    from app.core.context import RequestContext
    from app.core.database import tenant_session
    from app.models.screening_agent import ScreeningConversation, ScreeningQuestionnaireVersion
    from app.schemas.pipeline import ScreeningSummary
    from app.services.pipeline import screening as p1

    values = await _base(org)
    async with tenant_session(org.tenant_id) as db:
        conv = await db.get(ScreeningConversation, uuid.UUID(values["conversation_id"]))
        assert conv is not None
        ver = await db.get(ScreeningQuestionnaireVersion, conv.questionnaire_version_id)
        snapshot = [{k: v for k, v in q.items() if k in ("key", "label", "type", "options", "required", "maps_to")} for q in ver.questions]  # type: ignore[union-attr]
        conv.screening_id = await p1.record_agent_result(
            RequestContext(tenant_id=org.tenant_id, actor_kind="system"), db, conv.application_id, questions_snapshot=snapshot, answers={"interest": True, "notice_period": 75},
            summary=ScreeningSummary(summary="Interested; 75 days notice."), source_ref="",
        )  # fmt: skip
        conv.status = "completed"
    return values


for _name, _fn in (
    ("screening_consented", consented), ("screening_invited", invited), ("screening_posting", posting),
    ("screening_whatsapp_closed", whatsapp_closed), ("screening_burn_in", burn_in), ("screening_completed", completed),
):  # fmt: skip
    register_async_builder(_name, _fn)
