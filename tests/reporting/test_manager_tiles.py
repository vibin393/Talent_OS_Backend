"""Manager tiles over the fixed March 2026 timeline (``timeline.EXPECTED``, hand-computed)."""

from datetime import date, timedelta

from tests.reporting.helpers import IST, ROrg, Seed, tile
from tests.reporting.timeline import EXPECTED, RANGE, build_march


async def test_manager_tiles_match_hand_computed_values(rorg: ROrg) -> None:
    await build_march(rorg)
    m = rorg.manager

    assert (await tile(m, "manager", "open_demands", **RANGE))["value"] == EXPECTED["open_demands"]

    sla = await tile(m, "manager", "sla_risk", **RANGE)
    assert [r["value"] for r in sla["rows"]] == [EXPECTED["sla_at_risk"], EXPECTED["sla_breached"]]

    ttfs = await tile(m, "manager", "time_to_first_submission", **RANGE)
    assert ttfs["value"] == EXPECTED["ttfs_median_hours"]
    assert ttfs["n"] == EXPECTED["ttfs_n"]
    assert ttfs["unit"] == "hours"

    ttf = await tile(m, "manager", "time_to_fill", **RANGE)
    assert ttf["value"] == EXPECTED["ttf_median_days"]
    assert ttf["n"] == EXPECTED["ttf_n"]

    assert (await tile(m, "manager", "db_sourced_submissions", **RANGE))["value"] == EXPECTED[
        "db_sourced_pct"
    ]
    s2i = await tile(m, "manager", "submission_to_interview", **RANGE)
    assert s2i["value"] == EXPECTED["sub_to_interview_pct"]
    assert s2i["n"] == 4


async def test_throughput_zero_filled_and_ist_midnight_buckets(rorg: ROrg) -> None:
    await build_march(rorg)
    out = await tile(rorg.manager, "manager", "throughput", **RANGE)
    assert out["granularity"] == "day"
    series = {s["key"]: s for s in out["series"]}
    assert set(series) == {"submissions", "interviews", "offers", "hires"}
    for key, expected in (
        ("submissions", EXPECTED["submissions_by_day"]),
        ("interviews", EXPECTED["interviews_by_day"]),
        ("offers", EXPECTED["offers_by_day"]),
        ("hires", EXPECTED["hires_by_day"]),
    ):
        points = series[key]["points"]
        assert len(points) == 31  # every day, zero-filled
        assert points[0]["bucket"] == "2026-03-01"
        assert {p["bucket"]: p["value"] for p in points if p["value"]} == expected, key
    # s6 was sent at 00:30 IST on 9 March (18:30 UTC on the 8th): bucketed on the 9th in the tenant timezone.
    assert next(p for p in series["submissions"]["points"] if p["bucket"] == "2026-03-08")["value"] == 0


async def test_throughput_weekly_buckets(rorg: ROrg) -> None:
    await build_march(rorg)
    out = await tile(rorg.manager, "manager", "throughput", **RANGE, granularity="week")
    subs = next(s for s in out["series"] if s["key"] == "submissions")["points"]
    # ISO weeks starting Mon 23 Feb .. Mon 30 Mar: 6 buckets; the first one is clipped to 1 March.
    assert [p["bucket"] for p in subs][:2] == ["2026-02-23", "2026-03-02"]
    assert subs[0]["from"] == "2026-03-01"
    assert {p["bucket"]: p["value"] for p in subs if p["value"]} == {"2026-03-02": 3, "2026-03-09": 1}


async def test_stage_conversion_with_skipped_custom_stage(rorg: ROrg) -> None:
    await build_march(rorg)
    out = await tile(rorg.manager, "manager", "stage_conversion", **RANGE)
    assert out["kind"] == "funnel"
    got = [(r["key"], r["value"], r["pct_of_previous"]) for r in out["rows"]]
    assert got == EXPECTED["funnel"]


async def test_source_effectiveness_with_zero_hire_rate(rorg: ROrg) -> None:
    await build_march(rorg)
    out = await tile(rorg.manager, "manager", "source_effectiveness", **RANGE)
    rows = {r["source"]: {k: v for k, v in r.items() if k != "source"} for r in out["rows"]}
    assert rows == EXPECTED["sources"]
    assert [r["source"] for r in out["rows"]] == ["career_portal", "manual", "search"]


async def test_recruiter_activity(rorg: ROrg) -> None:
    await build_march(rorg)
    out = await tile(rorg.manager, "manager", "recruiter_activity", **RANGE)
    by_id = {r["id"]: r for r in out["rows"]}
    # BU A has one recruiter; recruiter B (BU B) is out of the manager's scope.
    assert set(by_id) == {str(rorg.recruiter.user_id)}
    row = by_id[str(rorg.recruiter.user_id)]
    assert {k: row[k] for k in EXPECTED["activity_recruiter"]} == EXPECTED["activity_recruiter"]
    assert out["row_links"][0]["query"]["sent_by"] == str(rorg.recruiter.user_id)
    # The tenant admin sees both recruiters.
    admin = await tile(rorg.admin, "manager", "recruiter_activity", **RANGE)
    assert {r["id"] for r in admin["rows"]} == {str(rorg.recruiter.user_id), str(rorg.recruiter_b.user_id)}


async def test_active_recruiters_sm3(rorg: ROrg) -> None:
    today = date.today()
    from datetime import UTC, datetime

    today = datetime.now(UTC).astimezone(IST).date()
    async with Seed(rorg.tenant_id) as s:
        await s.active_days(rorg.recruiter.user_id, [today - timedelta(days=i) for i in range(4)])
        await s.active_days(rorg.recruiter_b.user_id, [today - timedelta(days=10), today - timedelta(days=9)])
    manager = await tile(rorg.manager, "manager", "active_recruiters")
    assert (manager["value"], manager["n"]) == (100.0, 1)
    admin = await tile(rorg.admin, "manager", "active_recruiters")
    assert (admin["value"], admin["n"]) == (50.0, 2)
    assert admin["link"] == {"path": "/admin/users", "query": {"role": "recruiter"}}


async def test_active_day_recorded_once_per_request_day(rorg: ROrg) -> None:
    from sqlalchemy import text

    from app.core.database import tenant_session

    for _ in range(3):
        await rorg.recruiter_b.client.get("/api/v1/dashboards")
    async with tenant_session(rorg.tenant_id) as db:
        n = (
            await db.execute(
                text("SELECT count(*) FROM user_active_days WHERE user_id = :u"),
                {"u": rorg.recruiter_b.user_id},
            )
        ).scalar_one()
    assert n == 1
