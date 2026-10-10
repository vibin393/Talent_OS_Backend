# ruff: noqa: E501
"""Epic 69 test helpers."""

import contextlib
import dataclasses
import uuid
from collections.abc import Iterator
from typing import Any

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.integrations import manifest, manifest_sha256
from app.core.integrations import registry as manifest_registry
from app.core.permissions import Principal, Scope
from app.schemas.marketplace import InstallIn
from app.services import marketplace_service
from tests.epic3 import Org3

KEY = "fake_job_board"
PERMS = {
    "integration:manage": Scope.TENANT,
    "integration:view": Scope.TENANT,
    "ecosystem:kill_switch": Scope.TENANT,
}


def human(org: Org3, kind: str = "user") -> RequestContext:
    return RequestContext(
        tenant_id=org.tenant_id,
        principal=Principal(
            type=kind,
            id=org.admin.user_id if kind == "user" else uuid.uuid4(),
            tenant_id=org.tenant_id,
            permissions=dict(PERMS),
        ),
    )


def connector_in(org: Org3, *, api_key: str = "k-123456", key: str = KEY, **extra: Any) -> InstallIn:
    return InstallIn(
        kind="connector", connector_key=key, secrets={"api_key": api_key}, accountable_user_id=org.admin.user_id,
        accept_manifest_sha256=manifest_sha256(manifest(key)), **extra,
    )  # fmt: skip


async def install_connector(org: Org3, **kw: Any) -> Any:
    async with tenant_session(org.tenant_id) as db:
        return await marketplace_service.install(human(org), db, connector_in(org, **kw))


@contextlib.contextmanager
def swapped_manifest(key: str, **changes: Any) -> Iterator[Any]:
    """Temporarily replace a registered manifest (new version, hosts, erasure, adapters...)."""
    original = manifest(key)
    new = dataclasses.replace(original, **changes)
    manifest_registry._manifests[key] = new
    try:
        yield new
    finally:
        manifest_registry._manifests[key] = original
