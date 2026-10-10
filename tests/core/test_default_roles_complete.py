"""FR-81 matrix (Epic 14 A2): every Phase 2 default Role holds its domain keys once all domain epics merged.

Domain epics appended their keys to the six Roles; the ``900_merge_phase2`` gate closed the matrix.
"""

from app.core.permissions import CATALOG, DEFAULT_ROLES

FR81_MATRIX: dict[str, set[str]] = {
    "sales": {"lead:read", "opportunity:read", "client:read", "contract:read", "rate_card:read"},
    "hr_onboarding": {"placement:read", "onboarding:read", "document:read", "identity:view"},
    "compliance": {"bgv:read", "bgv:adjudicate", "credential:verify", "identity:view"},
    "finance": {"billing:read", "timesheet:read", "expense:read", "legal_entity:read"},
    "finance_admin": {
        "invoice:issue",
        "payment:record",
        "invoice:write_off",
        "period:close",
        "billing:write",
    },
    "vendor_manager": {"vendor:read", "vendor_portal:manage", "vendor_submission:review"},
}


def test_default_roles_cover_fr81_matrix() -> None:
    roles = {r.key: set(r.permissions) for r in DEFAULT_ROLES}
    missing_keys = sorted(k for keys in FR81_MATRIX.values() for k in keys if k not in CATALOG)
    assert missing_keys == []
    gaps = {
        role: sorted(keys - roles.get(role, set()))
        for role, keys in FR81_MATRIX.items()
        if keys - roles.get(role, set())
    }
    assert gaps == {}


# Epic 65 (FR-328): the Phase 4 role matrix, re-frozen at the Phase 4 close (``*_900_merge_phase4``).
P4_MATRIX: dict[str, set[str]] = {
    "ai_administrator": {"ai:govern", "ai:agent_configure", "ai:kill_switch", "role:read"},
    "recruiter": {
        "proposal:decide",
        "copilot:use",
        "message:draft",
        "message:send_templated",
        "screening:conduct",
    },
    "recruitment_manager": {"proposal:decide", "copilot:use"},
    "delivery_manager": {"proposal:decide", "copilot:use"},
    "account_manager": {"proposal:decide", "copilot:use"},
    "finance": {"proposal:decide", "copilot:use", "billing:prepare", "message:send_templated"},
    "finance_admin": {"proposal:decide", "copilot:use", "billing:prepare", "message:send_templated"},
    "hr_onboarding": {"proposal:decide", "knowledge:read", "knowledge:manage", "copilot:use"},
    "compliance": {"proposal:decide", "knowledge:read", "knowledge:manage", "bgv:follow_up", "copilot:use"},
    "campus_recruiter": {"proposal:decide", "copilot:use"},
}
NO_BUSINESS_DATA = {"candidate:read", "report:read", "proposal:decide", "copilot:use", "ai:payload_read"}


def test_default_roles_cover_p4_matrix() -> None:
    roles = {r.key: set(r.permissions) for r in DEFAULT_ROLES}
    gaps = {
        role: sorted(keys - roles.get(role, set()))
        for role, keys in P4_MATRIX.items()
        if keys - roles.get(role, set())
    }
    assert gaps == {}
    assert not roles["ai_administrator"] & NO_BUSINESS_DATA
