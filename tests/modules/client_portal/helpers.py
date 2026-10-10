"""Shared helpers of the Epic 18 (Client Portal content) tests."""

import json
import uuid
from collections.abc import Sequence
from typing import Any

from fastapi import FastAPI

from app.core.database import tenant_session
from app.models.external_auth import ClientUser, ClientUserScope
from tests.modules.demands.helpers import create_client
from tests.modules.pipeline.helpers import (
    POrg,
    add_one,
    create_candidate,
    per_app,
    shortlist,
    submit,
    upload_parsed_resume,
)
from tests.p2 import External, make_client_user, sign_in_external

CP = "/api/v1/client-portal"
ALL_PERMS = ("submissions:review", "demands:approve", "time:approve", "placements:view", "invoices:view")
FORBIDDEN_STRINGS = ("pay_rate", "margin", "burden", "match_score", "vendor", "@example.com", "98765")


async def client_user(
    app: FastAPI,
    tenant_id: uuid.UUID,
    client_id: uuid.UUID,
    *,
    permissions: Sequence[str] = ALL_PERMS,
    **kw: Any,
) -> External:
    cu = await make_client_user(tenant_id, client_id, permissions=permissions, **kw)
    return await sign_in_external(app, "client_user", cu, tenant_id, client_id)


async def contact_of(tenant_id: uuid.UUID, cu: uuid.UUID) -> uuid.UUID:
    async with tenant_session(tenant_id) as db:
        row = await db.get(ClientUser, cu)
        assert row is not None
        return row.contact_id


async def scope_to(tenant_id: uuid.UUID, cu: uuid.UUID, kind: str, subject_id: uuid.UUID) -> None:
    async with tenant_session(tenant_id) as db:
        db.add(
            ClientUserScope(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                client_user_id=cu,
                subject_kind=kind,
                subject_id=subject_id,
            )
        )


async def sent_submission(porg: POrg, name: str = "Priya Raman") -> tuple[str, str]:
    """``(application_id, submission_id)`` of a submission sent to the porg client."""
    cand = await create_candidate(porg.admin, name)
    await upload_parsed_resume(porg.admin, porg.tenant_id, cand["id"])
    app_id = await add_one(porg.admin, porg.demand_id, cand["id"])
    await shortlist(porg.admin, app_id)
    resp = await submit(
        porg,
        [app_id],
        {
            app_id: per_app(
                resume_kind="client_formatted",
                consent_attestation={"text": "Agreed by phone", "confirmed": True},
            )
        },
    )
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["status"] == "sent", item
    return app_id, str(item["submission_id"])


async def other_client(porg: POrg) -> uuid.UUID:
    return await create_client(porg.admin, f"Other {uuid.uuid4().hex[:6]}")


def clean(payload: Any) -> None:
    text = json.dumps(payload).lower()
    for s in FORBIDDEN_STRINGS:
        assert s not in text, f"{s} leaked into client JSON"


def idem() -> dict[str, str]:
    return {"Idempotency-Key": uuid.uuid4().hex}
