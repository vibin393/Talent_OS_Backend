# ruff: noqa: E501
"""Epic 68 story 68.2: ``GET /public/v1/event_types`` (public catalogue, internal types never listed) and
``GET /public/v1/events`` filtered to the types the caller's scopes allow."""

import uuid

from fastapi import FastAPI

from app.core import public_events as registry
from app.core.database import tenant_session
from app.models.public_events import OutboxEvent
from app.services.webhooks import common
from tests.epic3 import Org3
from tests.public_api.e67 import Api
from tests.support.oauth import install_app, make_app, mint


async def _event(tenant_id: uuid.UUID, event_type: str) -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        e = OutboxEvent(
            id=uuid.uuid4(), tenant_id=tenant_id, event_type=event_type, event_version=1, visibility="public", subject_type="x",
            subject_id=uuid.uuid4(), occurred_at=common.now(), data={"id": str(uuid.uuid4())}, source_event="Test", dispatch_status="dispatched",
        )  # fmt: skip
        db.add(e)
        await db.flush()
        return e.id


async def test_event_types_and_scoped_events(app: FastAPI, org3: Org3) -> None:
    inst = await install_app(org3.tenant_id, await make_app(scopes=("placements:read",), resources=("api",)), granted_by=org3.admin.user_id)
    api = Api(app, (await mint(inst, scopes=("placements:read",))).access_token)
    types = await api.get("/event_types")
    assert types.status_code == 200, types.text
    listed = {t["type"] for t in types.json()}
    assert listed == set(registry.catalogue())
    assert all("schema" in t for t in types.json())
    seen = await _event(org3.tenant_id, "placement.started")
    hidden = await _event(org3.tenant_id, "invoice.issued")
    page = await api.get("/events")
    assert page.status_code == 200, page.text
    ids = {e["id"] for e in page.json()["data"]}
    assert str(seen) in ids
    assert str(hidden) not in ids
    assert (await api.get(f"/events/{hidden}")).status_code == 404
    assert (await api.get(f"/events/{seen}")).status_code == 200
