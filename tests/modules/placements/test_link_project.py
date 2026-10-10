"""Epic 36 (spec 0.5): ``placements_service.link_project`` -- the only writer of the placement project links."""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.projects import Project, ProjectRole
from app.services import placements_service
from tests.contracts.test_contract_layer import _placement


async def _setup(app: Any) -> dict[str, uuid.UUID]:
    from tests.epic3 import create_candidate
    from tests.modules.demands.helpers import create_client
    from tests.modules.identity.conftest import join, make_tenant
    from tests.modules.legal_entities.conftest import provision

    tenant = await make_tenant(seat_limit=10)
    le = await provision(tenant)
    admin = await join(app, tenant, name="Link Admin")
    try:
        client_a = await create_client(admin, "Link Client A")
        client_b = await create_client(admin, "Link Client B")
        cand = uuid.UUID((await create_candidate(admin, full_name="Lina Link"))["id"])
    finally:
        await admin.client.aclose()
    ids = {"tenant": tenant.id, "client_a": client_a, "client_b": client_b}
    ids["placement"] = await _placement(tenant.id, le, client_a, cand)
    ids.update({k: uuid.uuid4() for k in ("prj_a", "prj_a2", "prj_b", "role_a", "role_a2")})
    async with tenant_session(tenant.id) as db:
        for k, c in (("prj_a", client_a), ("prj_a2", client_a), ("prj_b", client_b)):
            db.add(Project(id=ids[k], tenant_id=tenant.id, human_id=f"PRJ-{k}", name=k, client_id=c))
        await db.flush()
        for k, p in (("role_a", "prj_a"), ("role_a2", "prj_a2")):
            db.add(
                ProjectRole(id=ids[k], tenant_id=tenant.id, project_id=ids[p], client_id=client_a, title=k)
            )
    return ids


async def _link(ids: dict[str, uuid.UUID], project: str | None, role: str | None) -> Any:
    ctx = RequestContext(tenant_id=ids["tenant"])
    async with tenant_session(ids["tenant"]) as db:
        return await placements_service.link_project(
            ctx,
            db,
            ids["placement"],
            project_id=ids[project] if project else None,
            project_role_id=ids[role] if role else None,
        )


async def _row(ids: dict[str, uuid.UUID]) -> tuple[Any, Any]:
    async with tenant_session(ids["tenant"]) as db:
        r = (
            await db.execute(
                text("SELECT project_id, project_role_id FROM placements WHERE id = :i"),
                {"i": ids["placement"]},
            )
        ).one()
    return r.project_id, r.project_role_id


async def test_link_unlink_and_validation(app: Any) -> None:
    ids = await _setup(app)
    out = await _link(ids, "prj_a", "role_a")
    assert out.id == ids["placement"]
    assert await _row(ids) == (ids["prj_a"], ids["role_a"])
    await _link(ids, "prj_a", None)
    assert await _row(ids) == (ids["prj_a"], None)

    with pytest.raises(ValidationFailed) as exc:
        await _link(ids, "prj_b", None)
    assert exc.value.code == "project_client_mismatch"
    with pytest.raises(ValidationFailed) as exc:
        await _link(ids, "prj_a", "role_a2")
    assert exc.value.code == "project_role_mismatch"
    with pytest.raises(ValidationFailed) as exc:
        await _link(ids, None, "role_a")
    assert exc.value.code == "project_role_mismatch"
    ids["missing"] = uuid.uuid4()
    with pytest.raises(NotFound):
        await _link(ids, "missing", None)

    await _link(ids, None, None)
    assert await _row(ids) == (None, None)


async def test_closed_placement_refused(app: Any) -> None:
    ids = await _setup(app)
    async with tenant_session(ids["tenant"]) as db:
        await db.execute(
            text("UPDATE placements SET status = 'dropped' WHERE id = :i"), {"i": ids["placement"]}
        )
    with pytest.raises(ConflictError) as exc:
        await _link(ids, "prj_a", None)
    assert exc.value.code == "placement_closed"
