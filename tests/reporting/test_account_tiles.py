"""Account manager tiles over the March 2026 timeline: only the clients the account manager owns."""

from tests.reporting.helpers import ROrg, follow, tile
from tests.reporting.timeline import EXPECTED, RANGE, build_march


async def test_demands_by_client_only_owned_clients(rorg: ROrg) -> None:
    march = await build_march(rorg)
    out = await tile(rorg.account, "account", "demands_by_client", **RANGE)
    assert out["status"] == "ok", out
    assert [r["client_id"] for r in out["rows"]] == [str(march.c1)]  # C2 (Globex) is not theirs
    row = out["rows"][0]
    assert {k: row[k] for k in EXPECTED["client_row"]} == EXPECTED["client_row"]
    assert out["row_links"][0] == {"path": "/demands", "query": {"client_id": str(march.c1)}}


async def test_awaiting_feedback_and_offers_pending(rorg: ROrg) -> None:
    await build_march(rorg)
    awaiting = await tile(rorg.account, "account", "awaiting_feedback")
    assert awaiting["value"] == EXPECTED["awaiting_feedback_account"]
    assert awaiting["link"]["query"] == {"feedback": "none", "older_than": "3", "client_owner": "me"}
    assert (await follow(rorg.account, awaiting["link"]))["total"] == awaiting["value"]
    pending = await tile(rorg.account, "account", "offers_pending")
    assert pending["value"] == EXPECTED["offers_pending_account"]
    assert (await follow(rorg.account, pending["link"]))["total"] == pending["value"]


async def test_tenant_admin_account_view_is_tenant_wide(rorg: ROrg) -> None:
    march = await build_march(rorg)
    out = await tile(rorg.admin, "account", "demands_by_client", **RANGE)
    assert {r["client_id"] for r in out["rows"]} == {str(march.c1), str(march.c2)}
    # Admin: s1, s3, s6 and recruiter B's s4 have no feedback.
    assert (await tile(rorg.admin, "account", "awaiting_feedback"))["value"] == 4
