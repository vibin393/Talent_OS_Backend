# ruff: noqa: E501
"""Epic 79 (FR-412): the Phase 5 permission inventory and the Integration Administrator role.

Every Phase 5 permission key exists exactly once in the catalogue; ``NON_DELEGABLE`` covers the foundation list plus
``voice:call`` (76), ``campaign:copy_approve`` (78) and ``network:manage`` (75); the Integration Administrator holds
only integration / directory / own-task permissions and no business data.
"""

from app.core.permissions import CATALOG, DEFAULT_ROLES, NON_DELEGABLE

# key -> declaring epic (spec A6 / FR-412). ``portal_copilot:use`` lives in the portal catalogues, not CATALOG.
FR412_KEYS: dict[str, str] = {
    "integration:view": "66",
    "integration:manage": "66",
    "ecosystem:kill_switch": "66",
    "app:develop": "66",
    "usage:read": "66",
    "webhook:manage": "68",
    "posting:publish_external": "70",
    "hrms:sync": "71",
    "ledger:sync": "71",
    "mcp:use": "73",
    "network:manage": "75",
    "voice:call": "76",
    "campaign:copy_approve": "78",
}

NO_BUSINESS_DATA = {
    "candidate:read", "report:read", "proposal:decide", "copilot:use", "mcp:use", "voice:call",
    "invoice:issue", "payment:record", "ai:payload_read",
}


def test_fr412_keys_exist_once() -> None:
    missing = sorted(k for k in FR412_KEYS if k not in CATALOG)
    assert missing == [], f"Phase 5 permission keys missing from the catalogue: {missing}"


def test_non_delegable_covers_phase5_human_only() -> None:
    required = {
        "api_key:manage", "support_access:approve", "dsr:process", "role:manage", "user:manage",
        "settings:manage", "privacy:manage", "retention:manage",
        "integration:manage", "ecosystem:kill_switch", "app:develop",
        "voice:call", "campaign:copy_approve", "network:manage",
    }
    assert required <= NON_DELEGABLE


def test_integration_administrator_has_no_business_data() -> None:
    ia = next(r for r in DEFAULT_ROLES if r.key == "integration_administrator")
    keys = set(ia.permissions)
    assert not (keys & NO_BUSINESS_DATA), keys & NO_BUSINESS_DATA
    # It owns the console, Marketplace, webhooks and the kill switch.
    assert {"integration:view", "integration:manage", "ecosystem:kill_switch", "webhook:manage"} <= keys
    # Only directory reads and own fix-it Tasks beyond that.
    assert ia.permissions["task:create"].value == "own"
