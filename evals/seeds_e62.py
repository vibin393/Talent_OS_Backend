# ruff: noqa: E501
"""Epic 62 eval seeds (Compliance, BGV and Onboarding Agents). Records come from ``tests/modules/knowledge/helpers.py``.

| Seed | State | Placeholders |
|---|---|---|
| ``e62_compliance_due`` | Placement starting in 7 days, PAN card missing, portal opened, AI consent | ``candidate_id``, ``item_id``, ``placement_id`` |
| ``e62_compliance_portal_closed`` | starting in 3 days, portal never opened | same |
| ``e62_compliance_nothing`` | the PAN card is uploaded (pending review) | same |
| ``e62_bgv_manual`` / ``e62_bgv_connector`` / ``e62_bgv_limit`` | an overdue provider check (Manual / connector / 5 follow-ups sent) | ``case_id``, ``check_id``, ``provider_id`` (subject = the case) |
| ``e62_onboarding_thread`` | a pre-joining Worker's thread with a question, published "IT FAQ" + an internal article | ``thread_id``, ``chunk_id``, ``internal_chunk_id``, ``case_or_random`` (subject = the thread) |
"""

import uuid
from typing import Any

from evals.fixtures import register_async_builder

LAPTOP = "Laptops are issued on day one at the Pune office."


def _base(org: Any) -> dict[str, str]:
    return {
        "other_candidate_id": str(uuid.uuid4()),
        "random_id": str(uuid.uuid4()),
        "owner_id": str(org.admin.user_id),
    }


async def _compliance(org: Any, *, start_in: int, portal: bool, status: str = "missing") -> dict[str, str]:
    from tests.modules.knowledge.helpers import enable, grant_ai, open_portal, seed_worker

    seed = await seed_worker(org, start_in=start_in, item_status=status)
    await enable(org.tenant_id, "compliance")
    await grant_ai(org.tenant_id, seed.candidate_id)
    if portal:
        await open_portal(org.tenant_id, seed.candidate_id)
    return {**_base(org), **seed.values()}


async def compliance_due(org: Any) -> dict[str, str]:
    return await _compliance(org, start_in=7, portal=True)


async def compliance_portal_closed(org: Any) -> dict[str, str]:
    return await _compliance(org, start_in=3, portal=False)


async def compliance_nothing(org: Any) -> dict[str, str]:
    return await _compliance(org, start_in=7, portal=True, status="pending")


async def _bgv(org: Any, **kw: Any) -> dict[str, str]:
    from tests.modules.knowledge.helpers import enable, seed_bgv

    seed = await seed_bgv(org, **kw)
    await enable(org.tenant_id, "bgv")
    return {
        **_base(org), "candidate_id": str(seed.candidate_id), "case_id": str(seed.case_id), "check_id": str(seed.check_id),
        "provider_id": str(seed.provider_id), "subject_type": "bgv_case", "subject_id": str(seed.case_id),
    }  # fmt: skip


async def bgv_manual(org: Any) -> dict[str, str]:
    return await _bgv(org)


async def bgv_connector(org: Any) -> dict[str, str]:
    return await _bgv(org, connector=True)


async def bgv_limit(org: Any) -> dict[str, str]:
    return await _bgv(org, prior=5)


async def onboarding_thread(org: Any) -> dict[str, str]:
    from app.core.database import tenant_session
    from app.models.knowledge import OnboardingQaThread
    from app.services import knowledge_service
    from tests.modules.knowledge.helpers import enable, grant_ai, publish_article, seed_worker

    seed = await seed_worker(org, start_in=10)
    await enable(org.tenant_id, "onboarding")
    await grant_ai(org.tenant_id, seed.candidate_id, "ai_processing", "knowledge_assistant")
    _, chunk = await publish_article(org.tenant_id, org.ctx(), "IT FAQ", f"# IT\n\n{LAPTOP}")
    _, internal = await publish_article(
        org.tenant_id, org.ctx(), "Margin policy", "# Margins\n\nTarget margin is 20%.", audience="internal"
    )
    async with tenant_session(org.tenant_id) as db:
        elig = await knowledge_service.qa.eligibility(db, seed.candidate_id)
        assert elig is not None
        thread = OnboardingQaThread(
            id=uuid.uuid4(), tenant_id=org.tenant_id, candidate_id=seed.candidate_id, placement_id=elig.placement_id,
            onboarding_case_id=elig.case_id, audience=elig.audience, status="open", coordinator_user_id=elig.coordinator_id,
        )  # fmt: skip
        db.add(thread)
        await db.flush()
        await knowledge_service.qa.add_message(db, thread, "asker", "When do I get my laptop?")
    return {
        **_base(org), **seed.values(), "thread_id": str(thread.id), "chunk_id": str(chunk), "internal_chunk_id": str(internal),
        "subject_type": "onboarding_qa_thread", "subject_id": str(thread.id),
    }  # fmt: skip


for _name, _fn in (
    ("e62_compliance_due", compliance_due),
    ("e62_compliance_portal_closed", compliance_portal_closed),
    ("e62_compliance_nothing", compliance_nothing),
    ("e62_bgv_manual", bgv_manual),
    ("e62_bgv_connector", bgv_connector),
    ("e62_bgv_limit", bgv_limit),
    ("e62_onboarding_thread", onboarding_thread),
):
    register_async_builder(_name, _fn)
