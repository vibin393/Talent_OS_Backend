"""Drill-down links: every tile link parses against its list endpoint, and count KPIs / rows list exactly the
counted rows."""

from typing import Any

from tests.reporting.helpers import API, ROrg, follow, tile
from tests.reporting.timeline import RANGE, build_march

LIST_PREFIXES = ("/demands", "/reports/", "/tasks", "/candidates")


def _followable(link: dict[str, Any] | None) -> bool:
    return bool(link) and link["path"].startswith(LIST_PREFIXES) and link["path"].count("/") <= 2  # type: ignore[index]


async def test_every_tile_link_parses(rorg: ROrg) -> None:
    await build_march(rorg)
    listing = (await rorg.admin.client.get(f"{API}/dashboards")).json()["items"]
    followed = 0
    for dashboard in listing:
        if dashboard["key"] not in ("recruiter", "manager", "account"):
            continue  # Epic 34 dashboards: covered by tests/reporting/p2 (their tiles need Phase 2 data)
        for t in dashboard["tiles"]:
            out = await tile(rorg.admin, dashboard["key"], t["key"], **RANGE)
            assert out["status"] == "ok", (dashboard["key"], t["key"], out)
            links = [out.get("link"), *(out.get("row_links") or [])]
            links += [s.get("link") for s in out.get("series") or []]
            for lk in links:
                if _followable(lk):
                    await follow(rorg.admin, lk)
                    followed += 1
    assert followed > 20


async def test_kpi_values_equal_list_totals(rorg: ROrg) -> None:
    await build_march(rorg)
    for member, dashboard, key in (
        (rorg.manager, "manager", "open_demands"),
        (rorg.admin, "manager", "open_demands"),
        (rorg.account, "account", "awaiting_feedback"),
        (rorg.account, "account", "offers_pending"),
        (rorg.admin, "account", "offers_pending"),
    ):
        out = await tile(member, dashboard, key, **RANGE)
        assert (await follow(member, out["link"]))["total"] == out["value"], (dashboard, key)


async def test_row_links_equal_row_values(rorg: ROrg) -> None:
    await build_march(rorg)
    m = rorg.manager
    for key in ("sla_risk", "stage_conversion", "db_sourced_submissions", "submission_to_interview"):
        out = await tile(m, "manager", key, **RANGE)
        for row, lk in zip(out["rows"], out["row_links"], strict=True):
            assert (await follow(m, lk))["total"] == row["value"], (key, row)
    sources = await tile(m, "manager", "source_effectiveness", **RANGE)
    for row, lk in zip(sources["rows"], sources["row_links"], strict=True):
        assert (await follow(m, lk))["total"] == row["applications"], row


async def test_sla_link_lists_exactly_the_at_risk_demands(rorg: ROrg) -> None:
    march = await build_march(rorg)
    out = await tile(rorg.manager, "manager", "sla_risk", **RANGE)
    at_risk = await follow(rorg.manager, out["row_links"][0])
    assert [i["id"] for i in at_risk["items"]] == [str(march.demands["D2"])]
    assert out["row_links"][0]["query"] == {"sla": "at_risk"}
