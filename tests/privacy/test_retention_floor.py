"""Epic 65 (NFR-52): the oversight floor -- agent logs cannot be kept for less than 6 months."""

from tests.epic12 import Org3, h

API = "/api/v1/privacy/retention/classes"


async def test_agent_logs_oversight_floor(org3: Org3) -> None:
    c = org3.admin.client
    classes = (await c.get(API)).json()
    row = next(x for x in classes if x["key"] == "agent_logs")
    low = await c.patch(f"{API}/{row['id']}", json={"period_months": 3}, headers=h(org3.admin))
    assert low.status_code == 422 and low.json()["error"]["code"] == "retention_below_oversight_minimum"
    ok = await c.patch(f"{API}/{row['id']}", json={"period_months": 6}, headers=h(org3.admin))
    assert ok.status_code == 200, ok.text
    assert ok.json()["period_months"] == 6
