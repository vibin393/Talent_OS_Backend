# ruff: file-ignore[line-too-long]
"""SDK publication content (Epic 69, spec A8; consumed by Epic 74's docs generator): the JSON Schema of a Connector
Manifest and of an App manifest, and the category port method signatures (capabilities -> methods)."""

from typing import Any

from app.core.integrations import CAPABILITIES, CAPABILITY_METHODS, PERSONAL_DATA_CAPABILITIES
from app.core.integrations.capabilities import MANIFEST_CATEGORIES
from app.core.integrations.manifest import ADAPTER_MODES

_FLOW = {
    "type": "object",
    "required": ["direction", "entity", "fields", "purpose", "destination_country", "personal_data"],
    "properties": {
        "direction": {"enum": ["out", "in"]},
        "entity": {"type": "string", "maxLength": 60},
        "fields": {"type": "array", "items": {"type": "string"}},
        "purpose": {"type": "string", "maxLength": 300},
        "destination_country": {"type": "string", "pattern": "^[A-Z]{2}$"},
        "personal_data": {"type": "boolean"},
    },
}


def manifest_schema() -> dict[str, Any]:
    connector = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ConnectorManifest",
        "type": "object",
        "required": ["key", "version", "owner_epic", "name", "summary", "category", "capabilities", "auth", "adapters"],
        "properties": {
            "key": {"type": "string", "pattern": "^[a-z][a-z0-9_]{1,39}$"},
            "version": {"type": "integer", "minimum": 1},
            "name": {"type": "string"},
            "summary": {"type": "string"},
            "category": {"enum": list(MANIFEST_CATEGORIES)},
            "capabilities": {"type": "array", "items": {"enum": sorted(CAPABILITIES)}},
            "auth": {"enum": ["oauth2_code", "oauth2_client", "api_key", "basic", "hmac", "user_connection", "file", "none"]},
            "config_model": {"type": "object", "description": "Pydantic model (extra='forbid'); its JSON Schema drives the install form"},
            "secret_fields": {"type": "array", "items": {"type": "object", "required": ["name", "label", "kind"], "properties": {
                "name": {"type": "string"}, "label": {"type": "string"},
                "kind": {"enum": ["api_key", "password", "token", "private_key", "webhook_secret", "oauth_refresh"]},
                "required": {"type": "boolean"}}}},
            "data_flows": {"type": "array", "items": _FLOW},
            "egress_hosts": {"type": "array", "items": {"type": "string"}, "description": "exact hosts or *.suffix"},
            "sync_jobs": {"type": "array", "items": {"type": "object"}},
            "adapters": {"type": "object", "propertyNames": {"enum": list(ADAPTER_MODES)}, "required": ["fake"]},
            "legal_entity_scoped": {"type": "boolean"},
            "multi_install": {"type": "boolean"},
            "erasure": {"type": "boolean"},
            "publisher": {"const": "platform"},
            "docs_url": {"type": ["string", "null"]},
            "rate_per_minute": {"type": "integer"},
        },
    }  # fmt: skip
    app = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "AppManifest",
        "type": "object",
        "properties": {
            "name": {"type": "string", "maxLength": 120},
            "description": {"type": ["string", "null"]},
            "category": {"type": "string"},
            "data_flows": {"type": "array", "items": _FLOW, "maxItems": 50},
            "events_consumed": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
        },
    }
    return {"connector_manifest": connector, "app_manifest": app}


def ports() -> dict[str, Any]:
    return {
        "capabilities": {cap: list(methods) for cap, methods in sorted(CAPABILITY_METHODS.items())},
        "personal_data_capabilities": sorted(PERSONAL_DATA_CAPABILITIES),
        "categories": list(MANIFEST_CATEGORIES),
        "adapter_modes": list(ADAPTER_MODES),
        "connector_port": {"handle": "async (op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]",
                            "verify_webhook": "(headers, body: bytes) -> bool", "parse_webhook": "(headers, body: bytes) -> Mapping"},
    }  # fmt: skip


__all__ = ["manifest_schema", "ports"]
