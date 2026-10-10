# ruff: noqa: E501
"""Epic 65 story 65.6 (FR-323): Phase 4 AEDT inventory seeds, the agent selection points (offered only when their
tables exist, aggregate-only runs) and the LL144 notice line listing the active agent entries."""

from datetime import date, timedelta

from app.core.database import tenant_session
from app.services.fairness import aedt_p4
from tests.modules.demands.helpers import DOrg, h
from tests.modules.fairness.helpers import F, enable


async def test_inventory_seeds(dorg: DOrg) -> None:
    inv = (await dorg.admin.client.get(f"{F}/aedt")).json()
    agents = {e["feature"]: e for e in inv if e["feature"].startswith("agent_")}
    assert set(agents) == {
        "agent_sourcing",
        "agent_screening",
        "agent_voice_screening",
        "agent_interview_assist",
    }
    assert all(
        e["status"] == "inactive" and e["used_to_filter"] is False and e["data_used"] for e in agents.values()
    )
    assert agents["agent_sourcing"]["name"] == "Sourcing Agent shortlist ranking"
    assert not any("submission" in e["feature"] for e in inv)
    # idempotent
    again = (await dorg.admin.client.get(f"{F}/aedt")).json()
    assert len(again) == len(inv)


async def test_selection_points_catalogue_and_runs(dorg: DOrg) -> None:
    await enable(dorg)
    points = (await dorg.admin.client.get(f"{F}/selection-points")).json()
    keys = {p["key"]: p for p in points}
    for key in ("agent_shortlist_added", "agent_screening_flag_advanced", "agent_draft_rating_accepted"):
        assert keys[key]["available"] is True and keys[key]["definition"] and keys[key]["label"]
    assert keys["stage_transition"]["available"] is True
    today = date.today()
    for key, params in (
        ("agent_shortlist_added", {}),
        ("agent_screening_flag_advanced", {"source": "both"}),
        ("agent_draft_rating_accepted", {"min_accepted_share": 0.5}),
    ):
        body = {
            "selection_point": key,
            "params": params,
            "date_from": str(today - timedelta(days=30)),
            "date_to": str(today),
        }
        r = await dorg.admin.client.post(f"{F}/runs", json=body, headers=h(dorg.admin))
        assert r.status_code == 202, r.text
        run = (await dorg.admin.client.get(f"{F}/runs/{r.json()['id']}")).json()
        assert run["status"] in ("completed", "failed") and run["total_applicants"] in (0, None)


async def test_point_unavailable_without_tables(dorg: DOrg, monkeypatch: object) -> None:
    async def absent(db: object, name: str, cols: tuple[str, ...]) -> bool:
        return False

    monkeypatch.setattr(aedt_p4, "_present", absent)  # type: ignore[attr-defined]
    points = {p["key"]: p for p in (await dorg.admin.client.get(f"{F}/selection-points")).json()}
    assert points["agent_draft_rating_accepted"]["available"] is False
    today = date.today()
    body = {
        "selection_point": "agent_draft_rating_accepted",
        "date_from": str(today - timedelta(days=3)),
        "date_to": str(today),
    }
    r = await dorg.admin.client.post(f"{F}/runs", json=body, headers=h(dorg.admin))
    assert r.status_code == 422 and r.json()["error"]["code"] == "fairness_point_unavailable"


async def test_notice_lines(dorg: DOrg) -> None:
    async with tenant_session(dorg.tenant_id) as db:
        await aedt_p4.ensure_seed_p4(db)
        from sqlalchemy import select, update

        from app.models.fairness import AedtInventory

        await db.execute(
            update(AedtInventory).where(AedtInventory.seed_key == "agent_screening").values(status="active")
        )
        entries = list(
            (await db.execute(select(AedtInventory).where(AedtInventory.status == "active"))).scalars()
        )
    lines = aedt_p4.notice_lines(entries)
    assert lines == [
        "AI assistants used in this process: Screening Agent summaries and flags. A person makes every decision."
    ]
    assert aedt_p4.notice_lines([e for e in entries if not e.feature.startswith("agent_")]) == []
