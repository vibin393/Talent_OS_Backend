# ruff: noqa: E501
"""Epic 66 (story 66.10, spec 0.6 / 0.15): the Phase 5 contract stubs keep their frozen signatures.

``oauth.issue_token`` and ``metering.record`` are implemented by the foundation; ``marketplace.install`` ships a
minimal working body (69 extends it); ``webhooks.enqueue_delivery`` is filled by Epic 68;
``expose_tool`` is the registry Epic 73 / 77 call. A signature change needs a spine amendment."""

import inspect

import pytest

from app.core.agentic import exposure
from app.core.agentic.types import ToolSpec
from app.services import (
    agent_runtime_service,
    ecosystem_service,
    integrations_service,
    marketplace_service,
    metering_service,
    oauth_service,
    public_events_service,
    vault_service,
    webhooks_service,
)

PINNED: dict[str, tuple[object, list[str]]] = {
    "oauth.issue_token": (
        oauth_service.issue_token,
        ["db", "installation_id", "grant_type", "resource", "scopes", "user_id", "user_authorization_id", "family_id",
         "parent_id", "with_refresh", "issued_from_code_id"],
    ),
    "oauth.authenticate_bearer": (oauth_service.authenticate_bearer, ["request", "token", "resource"]),
    "oauth.revoke_for": (oauth_service.revoke_for, ["db", "installation_id", "user_id", "app_id", "family_id", "reason"]),
    "oauth.set_app_status": (oauth_service.set_app_status, ["ctx", "db", "app_id", "status", "reason"]),
    "oauth.create_app": (oauth_service.create_app, ["ctx", "db", "data", "publisher"]),
    "marketplace.install": (marketplace_service.install, ["ctx", "db", "data"]),
    "marketplace.upgrade": (marketplace_service.upgrade, ["ctx", "db", "installation_id", "accept_manifest_sha256"]),
    "marketplace.uninstall": (marketplace_service.uninstall, ["ctx", "db", "installation_id", "reason"]),
    "webhooks.enqueue_delivery": (webhooks_service.enqueue_delivery, ["ctx", "db", "subscription_id", "event_id", "replay"]),
    "metering.record": (
        metering_service.record, ["ctx", "unit", "quantity", "installation_id", "api_key_id", "route_group", "error", "app_id"]
    ),
    "mcp.expose_tool": (exposure.expose_tool, ["exp"]),
    "public_events.register_sink": (public_events_service.register_sink, ["name", "fn", "event_types", "include_internal"]),
    "public_events.dispatch": (public_events_service.dispatch, ["tenant_id", "batch"]),
    "vault.put": (vault_service.put, ["ctx", "db", "ref", "value", "meta", "expires_at"]),
    "vault.get": (vault_service.get, ["ctx", "db", "ref", "purpose"]),
    "vault.open_bag": (vault_service.open_bag, ["ctx", "db", "owner_type", "owner_id", "purpose"]),
    "vault.seal_field": (vault_service.seal_field, ["db", "tenant_id", "context", "plaintext"]),
    "ecosystem.is_suspended": (ecosystem_service.is_suspended, ["tenant_id", "app_id"]),
    "integrations.adapter_for": (integrations_service.adapter_for, ["ctx", "db", "installation_id", "config_id"]),
    "integrations.register_sync_handler": (integrations_service.register_sync_handler, ["connector_key_or_category", "job_key", "fn"]),
    "agents.execute_external_call": (agent_runtime_service.execute_external_call, ["ctx", "db", "data"]),
    "agents.open_external_session": (agent_runtime_service.open_external_session, ["ctx", "db", "data"]),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(PINNED))
def test_signature_pinned(name: str) -> None:
    fn, params = PINNED[name]
    assert list(inspect.signature(fn).parameters) == params  # type: ignore[arg-type]


def test_webhooks_enqueue_delivery_filled_by_epic_68() -> None:
    """Epic 68 filled the stub (same pinned signature); it no longer raises ``NotImplementedError``."""
    source = inspect.getsource(webhooks_service.enqueue_delivery)
    assert "NotImplementedError" not in source


def test_toolspec_mcp_exposed_flag_defaults_false() -> None:
    field = ToolSpec.__dataclass_fields__["mcp_exposed"]
    assert field.default is False


def test_external_call_out_carries_candidate_ids() -> None:
    """D-25(b): the bulk-read detector (Epic 73) reads ``candidate_ids``."""
    assert "candidate_ids" in agent_runtime_service.ExternalCallOut.model_fields
