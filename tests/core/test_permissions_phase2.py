"""Epic 14 (FR-81, AD-11, AD-16): Phase 2 permission keys owned by this epic, the sensitive set, the six
default
Roles, and the FR-82 403 contract."""

import pytest

from app.core.context import RequestContext, reset_context, set_context
from app.core.errors import Forbidden, _with_ask
from app.core.permissions import (
    CATALOG,
    DEFAULT_ROLES,
    SENSITIVE_PERMISSIONS,
    TENANT_ADMIN,
    Principal,
    Scope,
    check,
    effective_role_grants,
    validate_grants,
)

EPIC14_KEYS = {
    "sso:manage",
    "client_portal:manage",
    "vendor_portal:manage",
    "invoice:issue",
    "payment:record",
    "invoice:write_off",
    "bgv:adjudicate",
    "executive_dashboard:read",
}
SENSITIVE = {
    "identity:view",
    "invoice:issue",
    "payment:record",
    "invoice:write_off",
    "period:close",
    "bgv:adjudicate",
    # Epic 28 / Epic 30 (build plan §3 owners): finance reversals and reopen are sensitive too.
    "period:reopen_pay",
    "payment:reverse",
    "period:reopen",
}
# Phase 3 (spec-p3-referrals A1, spec-p3-predictive-analytics A1 / FR-243): the two Phase 3 sensitive keys.
P3_SENSITIVE = {"referral_reward:approve", "prediction:read_attrition"}
# Epic 53 (FR-328, C-14): Phase 4 AI governance keys (tenant-only; held by the Tenant Admin, not by default roles).
P4_SENSITIVE = {"ai:govern", "ai:agent_configure", "ai:kill_switch", "ai:payload_read"}
P4_SENSITIVE |= {"interview_capture:manage"}  # Epic 60 (spec A8: sensitive)
P4_SENSITIVE |= {"voice:upload"}  # Epic 58 (Part C: sensitive)
P5_SENSITIVE = {"integration:manage", "ecosystem:kill_switch", "app:develop"}  # Epic 66 (spec 0.16)
SENSITIVE_ALL = SENSITIVE | P3_SENSITIVE | P4_SENSITIVE | P5_SENSITIVE
P2_ROLES = {"sales", "hr_onboarding", "compliance", "finance", "finance_admin", "vendor_manager"}


def _roles() -> dict[str, dict[str, Scope]]:
    return {r.key: dict(r.permissions) for r in DEFAULT_ROLES}


def test_catalogue_keys_and_sensitive_set() -> None:
    assert set(CATALOG) >= EPIC14_KEYS
    assert SENSITIVE_PERMISSIONS == SENSITIVE_ALL
    assert {k for k, p in CATALOG.items() if p.sensitive} <= SENSITIVE_ALL
    assert all(CATALOG[k].scopes == (Scope.TENANT,) for k in EPIC14_KEYS)


def test_six_default_roles_and_their_sensitive_grants() -> None:
    roles = _roles()
    assert set(roles) >= P2_ROLES
    for role in roles.values():
        validate_grants({k: v.value for k, v in role.items()})
    sensitive_of = {key: SENSITIVE & set(perms) for key, perms in roles.items()}
    p3_sensitive_of = {key: P3_SENSITIVE & set(perms) for key, perms in roles.items()}
    assert sensitive_of["finance_admin"] == SENSITIVE - {"bgv:adjudicate"}
    assert sensitive_of["compliance"] == {"bgv:adjudicate", "identity:view"}
    assert sensitive_of["hr_onboarding"] == {"identity:view"}
    assert sensitive_of["sales"] == sensitive_of["finance"] == sensitive_of["vendor_manager"] == set()
    for p1 in (
        "recruitment_manager",
        "delivery_manager",
        "account_manager",
        "recruiter",
        "interviewer",
        "viewer",
    ):
        assert sensitive_of[p1] == set(), p1
    # Phase 3 sensitive keys go only where their specs say: Finance approves rewards, Account Manager sees
    # attrition risk; no other default role (Tenant Admin aside) holds either.
    assert {k: v for k, v in p3_sensitive_of.items() if v and k != TENANT_ADMIN} == {
        "finance": {"referral_reward:approve"},
        "account_manager": {"prediction:read_attrition"},
    }
    assert set(roles[TENANT_ADMIN]) >= SENSITIVE_ALL | EPIC14_KEYS


def test_simulated_resync_leaves_custom_roles_unchanged() -> None:
    """The phase-close resync only refreshes system roles; custom roles keep exactly their stored grants and
    never gain sensitive permissions (FR-81)."""
    custom = {"candidate:read": "tenant", "report:read": "own"}
    assert effective_role_grants("my_custom_role", False, custom) == custom
    admin = effective_role_grants(TENANT_ADMIN, True, {})
    assert set(admin) >= SENSITIVE_ALL


def test_require_details_fr82() -> None:
    principal = Principal(
        type="user", id=__import__("uuid").uuid4(), permissions={"candidate:read": Scope.OWN}
    )
    with pytest.raises(Forbidden) as exc:
        check(principal, "invoice:issue")
    details = exc.value.details
    assert details["required_permission"] == "invoice:issue" and details["permission"] == "invoice:issue"
    assert details["action"] == CATALOG["invoice:issue"].description


@pytest.mark.parametrize(
    ("kind", "ask"),
    [("user", "tenant_admin"), ("client_user", "account_manager"), ("vendor_user", "account_manager")],
)
def test_envelope_ask_by_principal(kind: str, ask: str) -> None:
    import uuid

    token = set_context(RequestContext(principal=Principal(type=kind, id=uuid.uuid4())))  # type: ignore[arg-type]
    try:
        assert _with_ask(Forbidden("no", action="x"))["ask"] == ask
        assert "ask" not in _with_ask(Forbidden("csrf", code="csrf_failed"))
        assert _with_ask(Forbidden("no", ask="tenant_admin"))["ask"] == "tenant_admin"
    finally:
        reset_context(token)
