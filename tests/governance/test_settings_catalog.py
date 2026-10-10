"""FR-186 Phase 2 settings catalogue (Epic 35 story 35.8, spec A9).

* Every required Phase 2 settings page is registered (``settings_service.register_settings_entry``) with a
  real permission and an owner epic.
* ``available`` follows route existence; unavailable entries are listed in ``PENDING_SETTINGS`` (reported as
  skips; an entry that became available fails so the list stays honest).
* The hub endpoint returns the registry entries with ``permission`` / ``owner_epic`` (frontend contract).
"""

import pytest
from fastapi import FastAPI

from app.core.permissions import CATALOG
from app.services import settings_catalog_p2, settings_service
from tests.epic3 import Org3

REQUIRED_KEYS: tuple[str, ...] = (
    "legal_entities", "gst_registrations", "invoice_series", "sac_codes", "payment_terms", "leave_types",
    "holiday_calendars", "expense_policy", "timesheet_defaults", "onboarding_templates", "document_templates",
    "bgv_packages", "compliance_requirements", "provider_connectors", "client_portal", "vendor_portal", "sso",
    "retention_classes", "sla_escalations", "golive_import",
)  # fmt: skip
PENDING_SETTINGS: dict[str, str] = {}  # every required entry is routed now
FIELDS = {"category", "key", "label", "description", "endpoint", "permission", "available", "owner_epic"}


def _entries() -> dict[str, settings_service.SettingsEntry]:
    settings_catalog_p2.register()
    return {e.key: e for e in settings_service.registered_settings_entries()}


def test_required_entries_registered_with_permission_and_owner() -> None:
    entries = _entries()
    assert set(REQUIRED_KEYS) <= set(entries), sorted(set(REQUIRED_KEYS) - set(entries))
    for key in REQUIRED_KEYS:
        entry = entries[key]
        assert entry.permission in CATALOG, f"{key}: unknown permission {entry.permission}"
        assert entry.owner_epic, key
        assert entry.endpoint.startswith("/api/v1/"), key


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_entry_available_by_route_existence(app: FastAPI, key: str) -> None:
    del app  # the app fixture imports every router module
    available = settings_service.endpoint_exists(_entries()[key].endpoint)
    if key in PENDING_SETTINGS:
        if available:
            pytest.fail(f"{key} is available now -- remove it from PENDING_SETTINGS")
        pytest.skip(f"{key}: {PENDING_SETTINGS[key]}")
    assert available, f"{key}: {_entries()[key].endpoint} is not routed"


def test_unknown_endpoint_is_unavailable(app: FastAPI) -> None:
    del app
    assert settings_service.endpoint_exists("/api/v1/settings") is True
    assert settings_service.endpoint_exists("/api/v1/no-such-settings-page") is False


async def test_hub_catalog_includes_registry_entries(org3: Org3) -> None:
    resp = await org3.admin.client.get("/api/v1/settings")
    assert resp.status_code == 200, resp.text
    items = {e["key"]: e for e in resp.json()}
    assert set(REQUIRED_KEYS) <= set(items)
    for key in REQUIRED_KEYS:
        assert set(items[key]) >= FIELDS, key
        assert items[key]["owner_epic"]
    assert items["sso"]["available"] is True
    assert items["retention_classes"]["permission"] == "retention:manage"
    # P1 entries keep their shape (owner_epic null).
    assert items["company"]["available"] is True and items["company"]["owner_epic"] is None


# ---------------------------------------------------------------- Epic 52 (A5, FR-261): Phase 3 entries
def _p3_entries() -> dict[str, settings_service.SettingsEntry]:
    from app.core.hooks import autodiscover
    from app.services import settings_catalog_p3

    autodiscover("app.services")
    settings_catalog_p3.register()
    return {e.key: e for e in settings_service.registered_settings_entries()}


def test_p3_entries_registered_with_permission_and_owner() -> None:
    from app.services.settings_catalog_p3 import REQUIRED_P3_SETTINGS

    entries = _p3_entries()
    missing = sorted(set(REQUIRED_P3_SETTINGS) - set(entries))
    assert missing == [], f"Phase 3 settings entries missing: {missing}"
    for key, owner in REQUIRED_P3_SETTINGS.items():
        entry = entries[key]
        assert entry.permission in CATALOG, f"{key}: unknown permission {entry.permission}"
        assert entry.owner_epic == owner, (key, entry.owner_epic)
        assert entry.endpoint.startswith("/api/v1/"), key


def test_p3_entries_are_routed(app: FastAPI) -> None:
    from app.services.settings_catalog_p3 import REQUIRED_P3_SETTINGS

    del app
    entries = _p3_entries()
    unrouted = sorted(k for k in REQUIRED_P3_SETTINGS if not settings_service.endpoint_exists(entries[k].endpoint))
    assert unrouted == [], f"Phase 3 settings endpoints not routed: {unrouted}"


# ---------------------------------------------------------------- Epic 65 (A8): Phase 4 entries
# Owner keys that shipped under another name (recorded in the Epic 65 notes) and keys still pending (shrink-only).
P4_KEY_ALIASES: dict[str, str] = {}
PENDING_P4_SETTINGS: dict[str, str] = {}


def _p4_entries() -> dict[str, settings_service.SettingsEntry]:
    from app.core.hooks import autodiscover
    from app.services.ai_governance import settings_catalog_p4

    autodiscover("app.services")
    settings_catalog_p4.register()
    return {e.key: e for e in settings_service.registered_settings_entries()}


def test_p4_entries_registered_with_permission() -> None:
    from app.services.ai_governance.settings_catalog_p4 import P4_REQUIRED_SETTINGS

    entries = _p4_entries()
    missing = sorted(
        k for k in P4_REQUIRED_SETTINGS if P4_KEY_ALIASES.get(k, k) not in entries and k not in PENDING_P4_SETTINGS
    )
    assert missing == [], f"Phase 4 settings entries missing: {missing}"
    for key in P4_REQUIRED_SETTINGS:
        entry = entries.get(P4_KEY_ALIASES.get(key, key))
        if entry is None:
            continue
        assert entry.permission is None or entry.permission in CATALOG, f"{key}: unknown permission {entry.permission}"
        assert entry.endpoint.startswith("/api/v1/"), key


def test_p4_entries_are_routed(app: FastAPI) -> None:
    from app.services.ai_governance.settings_catalog_p4 import P4_ENTRIES

    del app
    entries = _p4_entries()
    unrouted = sorted(k for _c, k, *_ in P4_ENTRIES if not settings_service.endpoint_exists(entries[k].endpoint))
    assert unrouted == [], f"Phase 4 settings endpoints not routed: {unrouted}"


def test_p4_pending_settings_empty_at_close() -> None:
    assert PENDING_P4_SETTINGS == {}


# ================================================================ Epic 79 (Phase 5 settings hub, FR-412)
PENDING_P5_SETTINGS: dict[str, str] = {}


def _p5_entries() -> dict[str, settings_service.SettingsEntry]:
    from app.core.hooks import autodiscover
    from app.services.ecosystem_governance import settings_catalog_p5

    autodiscover("app.services")
    settings_catalog_p5.register()
    return {e.key: e for e in settings_service.registered_settings_entries()}


def test_p5_entries_registered_with_permission_and_owner() -> None:
    from app.services.ecosystem_governance.settings_catalog_p5 import REQUIRED_P5_SETTINGS

    entries = _p5_entries()
    missing = sorted(k for k in REQUIRED_P5_SETTINGS if k not in entries and k not in PENDING_P5_SETTINGS)
    assert missing == [], f"Phase 5 settings entries missing: {missing}"
    for key in REQUIRED_P5_SETTINGS:
        entry = entries.get(key)
        if entry is None:
            continue
        assert entry.permission is None or entry.permission in CATALOG, f"{key}: unknown permission {entry.permission}"
        assert entry.endpoint.startswith("/api/v1/"), key
        assert entry.owner_epic, key


def test_p5_pending_settings_empty_at_close() -> None:
    assert PENDING_P5_SETTINGS == {}
