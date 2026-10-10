# ruff: noqa: E501
"""Story 37.3 (FR-189): graph-implication suggestions, AI inference (fake client; consent granted / withdrawn,
feature off), accept / edit / reject decisions, and the client-facing "never verified" guard."""

from typing import Any

from sqlalchemy import select

from app.core.database import tenant_session
from app.models.ai import AIAction
from app.services import ai
from app.services.ai.gateway import ConsentDecision
from tests.epic3 import FakeAI, parsed_payload, resume_pdf, upload
from tests.epic5 import h
from tests.modules.demands.helpers import DOrg
from tests.modules.skills.helpers import CANDIDATES, candidate_with


async def _suggestions(m: Any, cid: str, status: str = "pending") -> dict[str, dict[str, Any]]:
    resp = await m.client.get(f"{CANDIDATES}/{cid}/skill-suggestions", params={"status": status})
    assert resp.status_code == 200, resp.text
    return {s["name"]: s for s in resp.json()}


async def _profile(m: Any, cid: str) -> dict[str, dict[str, Any]]:
    return {r["name"]: r for r in (await m.client.get(f"{CANDIDATES}/{cid}/skill-profile")).json()}


async def test_graph_suggestion_accept_is_inferred_and_reject(dorg: DOrg) -> None:
    m = dorg.admin
    cand = await candidate_with(m, "Gita Graph", ["Spring Boot", "Django"])
    sugg = await _suggestions(m, cand["id"])
    assert sugg["Java"]["source"] == "graph" and sugg["Java"]["rationale"].startswith(
        "Implied by Spring Boot"
    )
    assert "Python" in sugg
    acc = await m.client.post(
        f"{CANDIDATES}/{cand['id']}/skill-suggestions/{sugg['Java']['id']}/accept",
        json={"proficiency": 4},
        headers=h(m),
    )
    assert acc.status_code == 200 and acc.json()["status"] == "edited"
    prof = await _profile(m, cand["id"])
    assert prof["Java"]["verification"] == "inferred" and prof["Java"]["proficiency"] == 4
    again = await m.client.post(
        f"{CANDIDATES}/{cand['id']}/skill-suggestions/{sugg['Java']['id']}/accept", json={}, headers=h(m)
    )
    assert again.status_code == 409
    rej = await m.client.post(
        f"{CANDIDATES}/{cand['id']}/skill-suggestions/{sugg['Python']['id']}/reject",
        json={"reason": "no"},
        headers=h(m),
    )
    assert rej.status_code == 200 and rej.json()["status"] == "rejected"
    # a rejected graph suggestion is not proposed again on the next skills change
    await m.client.put(
        f"{CANDIDATES}/{cand['id']}/skills",
        json={"skills": [{"name": "Spring Boot"}, {"name": "Django"}, {"name": "Java"}, {"name": "Go"}]},
        headers=h(m),
    )
    assert "Python" not in await _suggestions(m, cand["id"])


def _inference_payload(content: str) -> dict[str, Any]:
    if "Recorded skills (trusted)" in content:
        return {
            "skills": [
                {"name": "Docker", "proficiency": 3, "evidence_quote": "containerised"},
                {"name": "ZZNotASkill"},
            ]
        }
    return parsed_payload(skills=[{"name": "Python", "years": 2}])


async def _enable(dorg: DOrg, on: bool = True) -> None:
    async with tenant_session(dorg.tenant_id) as db:
        await ai.set_features(db, dorg.tenant_id, {"skill_inference": on})


async def test_ai_inference_with_consent(dorg: DOrg, fake_ai: FakeAI) -> None:
    fake_ai.payload_for = _inference_payload
    await _enable(dorg)
    m = dorg.admin
    cand = await candidate_with(m, "Ari AI", ["Python"])
    resp = await upload(m.client, f"{CANDIDATES}/{cand['id']}/resumes", resume_pdf())
    assert resp.status_code in (200, 201), resp.text
    sugg = await _suggestions(m, cand["id"])
    assert sugg["Docker"]["source"] == "ai_resume" and sugg["Docker"]["proposed_proficiency"] == 3
    assert "ZZNotASkill" not in sugg
    acc = await m.client.post(
        f"{CANDIDATES}/{cand['id']}/skill-suggestions/{sugg['Docker']['id']}/accept", json={}, headers=h(m)
    )
    assert acc.status_code == 200
    assert (await _profile(m, cand["id"]))["Docker"]["verification"] == "inferred"  # never verified
    async with tenant_session(dorg.tenant_id) as db:
        action = (
            (await db.execute(select(AIAction).where(AIAction.feature == "skill_inference")))
            .scalars()
            .first()
        )
    assert action is not None and action.decision == "accepted"


async def test_consent_withdrawn_or_feature_off_graph_only(dorg: DOrg, fake_ai: FakeAI) -> None:
    fake_ai.payload_for = _inference_payload
    m = dorg.admin

    async def deny(ctx: Any, db: Any, feature: str, subject: Any) -> ConsentDecision:
        return ConsentDecision(feature != "skill_inference", "test")

    await _enable(dorg)
    ai.set_consent_checker(deny)
    cand = await candidate_with(m, "Noor NoConsent", ["Spring Boot"])
    await upload(m.client, f"{CANDIDATES}/{cand['id']}/resumes", resume_pdf())
    sugg = await _suggestions(m, cand["id"])
    assert "Docker" not in sugg and sugg["Java"]["source"] == "graph"
    async with tenant_session(dorg.tenant_id) as db:
        rows = list(
            (await db.execute(select(AIAction).where(AIAction.feature == "skill_inference"))).scalars()
        )
    assert rows == []  # no call and no skipped row
    ai.set_consent_checker(ai.allow_all_consent_checker())
    await _enable(dorg, on=False)
    other = await candidate_with(m, "Fay FeatureOff", ["Spring Boot"])
    await upload(m.client, f"{CANDIDATES}/{other['id']}/resumes", resume_pdf())
    assert "Docker" not in await _suggestions(m, other["id"])


def test_client_facing_schemas_never_expose_verification_upgrades() -> None:
    """Client Portal / submission schemas: none exposes ``verification`` except as the stored value (P1/P2 portal
    schemas have no skill verification field at all, so inferred skills can never be shown as verified)."""
    import importlib
    import pkgutil

    import app.schemas as schemas_pkg

    offenders: list[str] = []
    for mod in pkgutil.iter_modules(schemas_pkg.__path__):
        if "portal" not in mod.name and "submission" not in mod.name:
            continue
        module = importlib.import_module(f"app.schemas.{mod.name}")
        for name, obj in vars(module).items():
            fields = getattr(obj, "model_fields", None)
            if isinstance(fields, dict) and "verified" in {str(f) for f in fields}:
                offenders.append(f"{mod.name}.{name}")
    assert offenders == []
