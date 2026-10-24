# ruff: file-ignore[line-too-long, unused-function-argument]
"""Enablement checks (Epic 65 story 65.3, spec A3 table). Read-only; each runs inside a savepoint (``checklist``).

Owner tables are read with plain counts through ``text()`` -- read-only probes of the owners' published tables, so a
missing owner table (an epic not merged yet) shows as ``warning`` instead of failing the page.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.agentic.agents import get_agent
from app.core.context import RequestContext
from app.core.settings import get_settings
from app.services.ai_governance.checklist import CheckItem, register_enablement_check

AEDT_AGENTS = ("sourcing", "screening", "voice_screening", "interview_assist")
CONSENT_PURPOSE = {
    "screening": "ai_agent_communication",
    "voice_screening": "voice_screening",
    "interview_assist": "interview_recording",
}


async def _count(db: AsyncSession, sql: str, **params: object) -> int:
    return int((await db.execute(text(sql), params)).scalar_one() or 0)


# ---------------------------------------------------------------- every agent
async def master_switch(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:
    from app.services.agent_runtime import state

    g = await state.gates(db)
    if g.agentic_on:
        return CheckItem("master_switch", "AI agents turned on for this tenant", "ready", "AI agents are on.")
    return CheckItem("master_switch", "AI agents turned on for this tenant", "missing",
                     "Turn on AI agents and accept the enablement notice.", "/admin/ai/agents")  # fmt: skip


async def provider_configured(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:  # NOSONAR
    s = get_settings()
    label = "AI provider configured"
    if s.anthropic_api_key and s.ai_provider != "fake":
        return CheckItem("provider_configured", label, "ready", "The AI provider is configured.")
    if s.app_env == "production":
        return CheckItem("provider_configured", label, "missing", "No AI provider key is configured.")
    return CheckItem("provider_configured", label, "warning",
                     "No AI provider key: this environment uses the test model.")  # fmt: skip


async def tenant_budget(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:
    from app.models.ai import AITenantSettings

    budget = (await db.execute(select(AITenantSettings.monthly_token_budget))).scalar_one_or_none()
    if budget:
        return CheckItem(
            "tenant_budget", "Tenant AI budget set", "ready", f"Monthly budget: {budget} tokens."
        )
    return CheckItem(
        "tenant_budget",
        "Tenant AI budget set",
        "warning",
        "No tenant monthly token budget is set.",
        "/admin/ai",
    )


async def owner_resolvable(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:
    from app.models.tasks import Task

    name = get_agent(agent_key).name
    since = datetime.now(UTC) - timedelta(days=7)
    n = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.title == f"Agent could not run: no owner ({name})"[:200], Task.created_at >= since
                )
            )
        ).scalar_one()
    )
    label = "Owners can be resolved for this agent's records"
    if n == 0:
        return CheckItem(
            "owner_resolvable", label, "ready", "No runs were skipped for a missing owner in 7 days."
        )
    return CheckItem(
        "owner_resolvable",
        label,
        "warning",
        f"{n} runs could not start in 7 days because no owner was found.",
    )


async def eval_passing(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:
    from app.services.ai_governance import evals

    row = await evals.latest_for_agent(db, agent_key)
    label = "Latest evaluation passing"
    if row is None or row.live is None:
        return CheckItem(
            "eval_passing", label, "warning", "Not run: no live evaluation result yet.", "/admin/ai/evals"
        )
    if row.status == "passing":
        return CheckItem(
            "eval_passing", label, "ready", f"Live score {row.live.score} (threshold {row.live.threshold})."
        )
    return CheckItem(
        "eval_passing", label, "warning", "The latest evaluation is below the threshold.", "/admin/ai/evals"
    )


# ---------------------------------------------------------------- agent specific
async def intake_mailbox(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:
    label = "Intake mailbox connected"
    mode = (
        await db.execute(text("SELECT intake_mode FROM demand_agent_settings LIMIT 1"))
    ).scalar_one_or_none()
    if mode in ("connected_mailbox", "platform_address"):
        return CheckItem("intake_mailbox", label, "ready", "Demand intake is set up.")
    return CheckItem(
        "intake_mailbox",
        label,
        "missing",
        "Connect a mailbox or turn on the intake address.",
        "/admin/settings",
    )


async def calendars(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:
    label = "Interviewers have connected calendars"
    active = await _count(db, "SELECT count(DISTINCT user_id) FROM user_connections WHERE status = 'active'")
    users = await _count(db, "SELECT count(*) FROM users WHERE is_active")
    if active >= 1:
        return CheckItem("calendars", label, "ready", f"{active} of {users} users have a connected calendar.")
    return CheckItem(
        "calendars", label, "missing", "No interviewer has connected a calendar.", "/settings/connections"
    )


async def knowledge_sources(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:
    label = "Published Knowledge Sources"
    n = await _count(db, "SELECT count(*) FROM knowledge_sources WHERE status = 'published'")
    if n >= 1:
        return CheckItem("knowledge_sources", label, "ready", f"{n} published Knowledge Sources.")
    return CheckItem(
        "knowledge_sources", label, "missing", "Publish at least one Knowledge Source.", "/knowledge"
    )


async def recording_policy(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:
    label = "Recording Policy reviewed"
    total = await _count(db, "SELECT count(*) FROM recording_policies")
    reviewed = await _count(db, "SELECT count(*) FROM recording_policies WHERE updated_by IS NOT NULL")
    if reviewed >= 1:
        return CheckItem("recording_policy", label, "ready", f"{total} jurisdictions configured.")
    if total >= 1:
        return CheckItem(
            "recording_policy",
            label,
            "warning",
            "Only the default policy exists; review it.",
            "/admin/settings",
        )
    return CheckItem("recording_policy", label, "warning", "No Recording Policy exists.", "/admin/settings")


async def questionnaires(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:
    label = "Approved screening questionnaire"
    n = await _count(db, "SELECT count(*) FROM screening_questionnaire_versions WHERE status = 'approved'")
    if n >= 1:
        return CheckItem("questionnaires", label, "ready", f"{n} approved questionnaire versions.")
    return CheckItem(
        "questionnaires", label, "missing", "Approve a screening questionnaire.", "/admin/settings"
    )


async def consent_notice(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:
    from app.services.privacy import consent

    label = "Candidate notice published"
    purpose = CONSENT_PURPOSE[agent_key]
    assert ctx.tenant_id is not None
    notice = await consent.active_notice(db, ctx.tenant_id, purpose)
    if notice is not None:
        return CheckItem("consent_notice", label, "ready", f"Notice '{purpose}' is published.")
    return CheckItem(
        "consent_notice", label, "missing", f"Publish the '{purpose}' candidate notice.", "/admin/privacy"
    )


async def aedt_entry(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:
    from app.services.fairness import aedt_p4

    label = "AEDT inventory entry confirmed"
    status = await aedt_p4.entry_status(db, agent_key)
    if status == "active":
        return CheckItem("aedt_entry", label, "ready", "Compliance confirmed the AEDT inventory entry.")
    from app.services.agent_runtime import state

    if (await state.gates(db)).agent_on(
        agent_key
    ):  # an enabled AEDT agent with an inactive entry -> Compliance Task
        await aedt_p4.request_confirmation(db, agent_key)
    return CheckItem(
        "aedt_entry",
        label,
        "warning",
        "Compliance has not confirmed the AEDT inventory entry yet.",
        "/fairness",
    )


async def collection_rules(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:
    label = "Collection rules reviewed"
    n = await _count(db, "SELECT count(*) FROM collection_rules WHERE client_id IS NULL AND active")
    if n >= 1:
        return CheckItem("collection_rules", label, "ready", "The default collection rule exists.")
    return CheckItem(
        "collection_rules", label, "warning", "Review the default collection rule.", "/admin/settings"
    )


async def copilot_settings(ctx: RequestContext, db: AsyncSession, agent_key: str) -> CheckItem:
    label = "Copilot retention and limits reviewed"
    n = await _count(db, "SELECT count(*) FROM copilot_settings")
    if n >= 1:
        return CheckItem("copilot_settings", label, "ready", "Copilot settings were saved.")
    return CheckItem(
        "copilot_settings", label, "warning", "Review the Copilot retention and limits.", "/admin/settings"
    )


def register() -> None:
    prod = get_settings().app_env == "production"
    register_enablement_check(
        "*", "master_switch", "AI agents turned on for this tenant", master_switch, blocking=True
    )
    register_enablement_check(
        "*", "provider_configured", "AI provider configured", provider_configured, blocking=prod
    )
    register_enablement_check("*", "tenant_budget", "Tenant AI budget set", tenant_budget)
    register_enablement_check(
        "*", "owner_resolvable", "Owners can be resolved for this agent's records", owner_resolvable
    )
    register_enablement_check("*", "eval_passing", "Latest evaluation passing", eval_passing)
    register_enablement_check(
        "demand", "intake_mailbox", "Intake mailbox connected", intake_mailbox, blocking=True
    )
    register_enablement_check(
        "scheduling", "calendars", "Interviewers have connected calendars", calendars, blocking=True
    )
    register_enablement_check(
        "onboarding", "knowledge_sources", "Published Knowledge Sources", knowledge_sources, blocking=True
    )
    register_enablement_check(
        "voice_screening", "recording_policy", "Recording Policy reviewed", recording_policy
    )
    for key in ("screening", "voice_screening"):
        register_enablement_check(
            key, "questionnaires", "Approved screening questionnaire", questionnaires, blocking=True
        )
    for key in CONSENT_PURPOSE:
        register_enablement_check(
            key, "consent_notice", "Candidate notice published", consent_notice, blocking=True
        )
    for key in AEDT_AGENTS:
        register_enablement_check(key, "aedt_entry", "AEDT inventory entry confirmed", aedt_entry)
    register_enablement_check(
        "collections", "collection_rules", "Collection rules reviewed", collection_rules
    )
    register_enablement_check(
        "copilot", "copilot_settings", "Copilot retention and limits reviewed", copilot_settings
    )


register()

__all__ = ["AEDT_AGENTS", "CONSENT_PURPOSE", "register"]
