"""Recruiter ("my work") tiles, seeded relative to now in the tenant timezone (IST)."""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

from tests.reporting.helpers import IST, ROrg, Seed, follow, tile


@dataclasses.dataclass
class Mine:
    demands: dict[str, uuid.UUID]
    apps: dict[str, uuid.UUID]


async def build_mine(org: ROrg) -> Mine:
    now = datetime.now(UTC)
    local_today = now.astimezone(IST).replace(hour=12, minute=0, second=0, microsecond=0).astimezone(UTC)
    rec, rec_b, admin = org.recruiter.user_id, org.recruiter_b.user_id, org.admin.user_id
    async with Seed(org.tenant_id) as s:
        c = await s.client(admin, "Initech Client")
        d = {
            "open_breached": await s.demand(c, admin, opened_at=now - timedelta(days=10), bu=org.bu_a),
            "open_at_risk": await s.demand(c, admin, opened_at=now - timedelta(days=3), bu=org.bu_a),
            "filled": await s.demand(
                c, admin, status="filled", opened_at=now - timedelta(days=30), bu=org.bu_a
            ),
            "not_mine": await s.demand(c, admin, opened_at=now - timedelta(days=5), bu=org.bu_b),
        }
        await s.sla_clock(d["open_breached"], "breached")
        await s.sla_clock(d["open_at_risk"], "at_risk")
        await s.sla_clock(d["not_mine"], "breached")
        for key in ("open_breached", "open_at_risk", "filled"):
            await s.assign(d[key], rec)
        await s.assign(d["not_mine"], rec_b)
        cands = [await s.candidate(rec, now - timedelta(days=40), f"Cand {i}") for i in range(5)]
        a = {
            "applied": await s.application(cands[0], d["open_breached"], rec, now - timedelta(days=2)),
            "shortlisted": await s.application(
                cands[1],
                d["open_at_risk"],
                rec,
                now - timedelta(days=2),
                path=[("shortlisted", now - timedelta(days=1))],
            ),
            "rejected": await s.application(
                cands[2],
                d["open_at_risk"],
                rec,
                now - timedelta(days=2),
                path=[("rejected", now - timedelta(days=1))],
            ),
            "other": await s.application(cands[3], d["not_mine"], rec_b, now - timedelta(days=2)),
        }
        # This ISO week: one submission sent by me (and one by recruiter B), interviews, an offer.
        await s.submission(a["shortlisted"], sent_at=now - timedelta(minutes=1), sent_by=rec)
        await s.submission(a["other"], sent_at=now - timedelta(minutes=1), sent_by=rec_b)
        await s.interview(a["shortlisted"], local_today, rec, participants=(rec,))
        await s.interview(a["other"], local_today, rec_b, participants=(rec,))  # I am on the panel
        await s.interview(a["other"], local_today, rec_b, participants=(rec_b,))  # not mine
        await s.interview(a["applied"], local_today, rec, status="cancelled")
        await s.interview(a["applied"], local_today + timedelta(days=8), rec)  # next week
        await s.offer(a["shortlisted"], rec, status="released", released_at=now - timedelta(minutes=1))
        # Tasks: one due later today, two overdue, one done, one of someone else.
        await s.task(rec, now + timedelta(minutes=1))
        await s.task(rec, now - timedelta(days=1))
        await s.task(rec, now - timedelta(days=2))
        await s.task(rec, now - timedelta(days=1), status="done")
        await s.task(rec_b, now - timedelta(days=1))
    return Mine(demands=d, apps=a)


async def test_my_open_demands_worst_sla_first(rorg: ROrg) -> None:
    mine = await build_mine(rorg)
    out = await tile(rorg.recruiter, "recruiter", "my_open_demands")
    assert out["value"] == 2
    assert [r["id"] for r in out["rows"]] == [
        str(mine.demands["open_breached"]),
        str(mine.demands["open_at_risk"]),
    ]
    assert [r["sla_status"] for r in out["rows"]] == ["breached", "at_risk"]
    assert out["rows"][0]["days_open"] == 10
    assert [r["pipeline_count"] for r in out["rows"]] == [1, 1]  # the rejected application is terminal
    assert out["row_links"][0]["path"] == f"/demands/{mine.demands['open_breached']}"
    assert (await follow(rorg.recruiter, out["link"]))["total"] == 2


async def test_my_pipeline_by_stage(rorg: ROrg) -> None:
    await build_mine(rorg)
    out = await tile(rorg.recruiter, "recruiter", "my_pipeline_by_stage")
    counts = {r["key"]: r["value"] for r in out["rows"]}
    assert counts["applied"] == 1
    assert counts["shortlisted"] == 1
    assert "rejected" not in counts and "hired" not in counts
    assert out["value"] == 2
    keys = [r["key"] for r in out["rows"]]
    assert keys.index("applied") < keys.index("shortlisted") < keys.index("submitted")
    for row, lk in zip(out["rows"], out["row_links"], strict=True):
        assert (await follow(rorg.recruiter, lk))["total"] == row["value"]


async def test_tasks_due_and_overdue(rorg: ROrg) -> None:
    await build_mine(rorg)
    out = await tile(rorg.recruiter, "recruiter", "tasks_due")
    values = {r["key"]: r["value"] for r in out["rows"]}
    assert values["overdue"] == 2
    overdue_link = out["row_links"][1]
    assert overdue_link == {"path": "/tasks", "query": {"view": "overdue", "owner": "me"}}
    assert (await follow(rorg.recruiter, overdue_link))["total"] == 2
    assert (await follow(rorg.recruiter, out["row_links"][0]))["total"] == values["due_today"]


async def test_interviews_today(rorg: ROrg) -> None:
    await build_mine(rorg)
    out = await tile(rorg.recruiter, "recruiter", "interviews_today")
    assert out["value"] == 2  # my application + the panel I sit on; not cancelled / other days / others'
    assert len(out["rows"]) == 2
    assert (await follow(rorg.recruiter, out["link"]))["total"] == 2


async def test_my_week(rorg: ROrg) -> None:
    await build_mine(rorg)
    out = await tile(rorg.recruiter, "recruiter", "my_week")
    values = {r["key"]: r["value"] for r in out["rows"]}
    assert values == {"submissions": 1, "interviews": 2, "offers": 1}
    for row, lk in zip(out["rows"], out["row_links"], strict=True):
        assert (await follow(rorg.recruiter, lk))["total"] == row["value"], row["key"]


async def test_recruiter_sees_only_own_numbers(rorg: ROrg) -> None:
    await build_mine(rorg)
    other = await tile(rorg.recruiter_b, "recruiter", "my_open_demands")
    assert other["value"] == 1
    week = await tile(rorg.recruiter_b, "recruiter", "my_week")
    assert {r["key"]: r["value"] for r in week["rows"]}["submissions"] == 1
