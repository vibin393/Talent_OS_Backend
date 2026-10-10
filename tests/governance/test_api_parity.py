"""FR-186 governance (Epic 35 A9): API parity, by static route introspection (no DB).

(a) Every internal Phase 2 route depends on ``require(<permission>)`` / ``require_any(...)`` -- the closures
    built by ``app.core.permissions`` (``__qualname__`` ``require.<locals>.dependency``). Those closures
    authenticate through ``current_principal`` (session *or* API key) and refuse External Principals, so a
    route that has one is reachable with an API key holding the permission. "Phase 2" is decided by the first
    path segment after ``/api/v1`` (``P2_SEGMENTS``: the P2 domains plus the P1 admin/settings/privacy areas
    they extend); unprefixed P1 resources are out of scope here.
(b) Every portal route (``/client-portal``, ``/vendor-portal``, ``/portal/...`` incl. ``/portal/work``)
    depends on a portal principal (``require_client_user`` / ``require_vendor_user`` / ``require_candidate``)
    and never on the internal ``current_principal`` / ``require()``. Those dependencies read only their own
    session cookie (``tos_client`` / ``tos_vendor`` / ``tos_candidate``) and never the ``Authorization``
    header, so an API key gets 401. Pre-auth sign-in routes (``/auth/...``) are exempt.
(c) The sensitive permissions that need a second confirmation in the API-key UI exist in the catalogue.

EXEMPT_ROUTES carry a justification; PENDING_ROUTES name the owner that still has to add ``require()``.
"""

import inspect

import pytest
from fastapi.routing import APIRoute

from app.core.permissions import CATALOG, current_principal
from app.dependencies.candidate import require_candidate
from app.dependencies.client import require_client_user
from app.dependencies.vendor import require_vendor_user
from app.main import app

P2_SEGMENTS: frozenset[str] = frozenset({
    # finance (Epics 15-17, 21)
    "billing", "receivables", "payroll", "periods", "reconciliation", "legal-entities", "gst-registrations",
    "document-series", "bank-accounts", "fx-rates", "billing-addresses",
    # commercials (Epic 5)
    "commercials", "contracts", "contract-milestones", "contract-documents", "purchase-orders", "rate-cards",
    "rate-card-templates", "rate-checks", "payment-terms",
    # workforce (Epics 6-14)
    "placements", "extensions", "workers", "bench", "time", "expenses", "bgv", "bgv-cases", "bgv-checks",
    "bgv-discrepancies", "compliance", "credentials", "onboarding", "onboarding-cases", "onboarding-tasks",
    "offboarding", "offboarding-cases", "offboarding-items",
    # vendors / CRM (Epics 4, 3)
    "vendors", "vendor-submissions", "vendor-statements", "vendor-releases", "crm", "leads", "lead-imports",
    "opportunities",
    # platform areas (documents, connectors, SSO, messaging, SLA, approvals config, go-live, privacy, audit)
    "documents", "connectors", "connections", "sso", "comms", "sla", "approval-chains", "golive", "audit",
    "privacy", "consents", "settings", "admin", "reports", "dashboards", "exports",
})  # fmt: skip

# (method, path) -> justification
EXEMPT_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/settings/demands"): "P1 demand settings read for every signed-in user (form defaults)",
    (
        "GET",
        "/api/v1/payment-terms",
    ): "lookup list used by every commercial form; writes require billing perms",
    (
        "POST",
        "/api/v1/rate-checks/preview",
    ): "read-only rate preview for any user; no data written or exposed",
    ("GET", "/api/v1/documents/signers/mine"): "internal signatory self-service: own signing tasks only",
    ("GET", "/api/v1/documents/signers/{signer_id}"): "internal signatory: service binds signer to the user",
    (
        "GET",
        "/api/v1/documents/signers/{signer_id}/view",
    ): "internal signatory: service binds signer to the user",
    (
        "POST",
        "/api/v1/documents/signers/{signer_id}/otp",
    ): "internal signatory: service binds signer to the user",
    (
        "POST",
        "/api/v1/documents/signers/{signer_id}/consent",
    ): "internal signatory: service binds signer to user",
    (
        "POST",
        "/api/v1/documents/signers/{signer_id}/sign",
    ): "internal signatory: service binds signer to the user",
    (
        "POST",
        "/api/v1/documents/signers/{signer_id}/decline",
    ): "internal signatory: service binds signer to user",
}

# (method, path) -> owner that has to add require()
PENDING_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/documents/signature-requests/{request_id}/cancel"): (
        "documents (eSign): authorises the requester in the service only; add require('document:...')"
    )
}

PORTAL_PREFIXES = ("/api/v1/client-portal", "/api/v1/vendor-portal", "/api/v1/portal/")
PORTAL_PRINCIPALS = (require_client_user, require_vendor_user, require_candidate)

SENSITIVE_PERMISSIONS = (
    "identity:view",
    "invoice:issue",
    "payment:record",
    "invoice:write_off",
    "period:close",
    "bgv:adjudicate",
)
PENDING_PERMISSIONS: dict[str, str] = {}


def _calls(dependant: object, out: list[object] | None = None) -> list[object]:
    out = [] if out is None else out
    for sub in getattr(dependant, "dependencies", []):
        if sub.call is not None:
            out.append(sub.call)
        _calls(sub, out)
    return out


def _is_permission_dep(call: object) -> bool:
    return getattr(call, "__qualname__", "").startswith(("require.<locals>.", "require_any.<locals>."))


def _routes() -> list[APIRoute]:
    return [r for r in app.routes if isinstance(r, APIRoute)]


def _key(route: APIRoute) -> list[tuple[str, str]]:
    return [(m, route.path) for m in sorted(route.methods - {"HEAD", "OPTIONS"})]


def _segment(path: str) -> str | None:
    parts = path.split("/")
    return parts[3] if len(parts) > 3 and path.startswith("/api/v1/") else None


def _internal_p2_routes() -> list[APIRoute]:
    return [r for r in _routes() if _segment(r.path) in P2_SEGMENTS]


def _portal_routes() -> list[APIRoute]:
    return [r for r in _routes() if r.path.startswith(PORTAL_PREFIXES)]


# ---------------------------------------------------------------- (a) internal routes
def test_internal_phase2_routes_require_a_permission() -> None:
    routes = _internal_p2_routes()
    assert routes, "no Phase 2 routes found -- check P2_SEGMENTS"
    missing = [
        key
        for route in routes
        if not any(_is_permission_dep(c) for c in _calls(route.dependant))
        for key in _key(route)
        if key not in EXEMPT_ROUTES and key not in PENDING_ROUTES
    ]
    assert not missing, f"internal Phase 2 routes without require()/require_any(): {missing}"


def test_exempt_and_pending_routes_are_live() -> None:
    """Stale entries fail: a pending route that now has require() must leave the list; vanished routes too."""
    by_key = {key: route for route in _routes() for key in _key(route)}
    gone = [k for k in (*EXEMPT_ROUTES, *PENDING_ROUTES) if k not in by_key]
    assert not gone, f"EXEMPT/PENDING routes no longer exist: {gone}"
    fixed = [k for k in PENDING_ROUTES if any(_is_permission_dep(c) for c in _calls(by_key[k].dependant))]
    assert not fixed, f"routes now have require(): remove them from PENDING_ROUTES: {fixed}"
    if PENDING_ROUTES:
        pytest.skip(f"pending require(): {sorted(PENDING_ROUTES)}")


def test_permission_dependency_admits_api_keys_and_refuses_portals() -> None:
    """require()'s closure authenticates via ``current_principal`` (the combined session / API-key resolver)
    and refuses External Principals (AD-26)."""
    from app.core import permissions

    source = inspect.getsource(permissions.require)
    assert "Depends(current_principal)" in source
    assert "EXTERNAL_PRINCIPAL_TYPES" in source
    from app.dependencies import auth

    assert "api_key" in inspect.getsource(auth.resolve_principal)


# ---------------------------------------------------------------- (b) portal routes
def test_portal_routes_use_a_portal_principal_only() -> None:
    routes = _portal_routes()
    assert routes
    bad: list[tuple[str, str, str]] = []
    for route in routes:
        calls = _calls(route.dependant)
        internal = current_principal in calls or any(_is_permission_dep(c) for c in calls)
        portal = any(c in PORTAL_PRINCIPALS for c in calls)
        if internal:
            bad += [(*k, "internal principal dependency") for k in _key(route)]
        elif not portal and "/auth/" not in route.path:
            bad += [(*k, "no portal principal dependency") for k in _key(route)]
    assert not bad, f"portal routes must authenticate with a portal cookie session only: {bad}"


@pytest.mark.parametrize(
    ("dependency", "resolver"),
    [
        (require_client_user, "app.services.external_auth.sessions:resolve"),
        (require_vendor_user, "app.services.external_auth.sessions:resolve"),
        (require_candidate, "app.services.portal.candidate_auth:authenticate_candidate"),
    ],
)
def test_portal_principals_read_cookies_not_api_keys(dependency: object, resolver: str) -> None:
    import importlib

    module, name = resolver.split(":")
    assert name in inspect.getsource(dependency)  # type: ignore[arg-type]
    source = inspect.getsource(getattr(importlib.import_module(module), name))
    assert "request.cookies.get(" in source
    for forbidden in ("Authorization", "api_key", "Bearer"):
        assert forbidden not in source, f"{resolver} must not accept {forbidden}"


# ---------------------------------------------------------------- (c) sensitive permissions
@pytest.mark.parametrize("permission", SENSITIVE_PERMISSIONS)
def test_sensitive_permission_in_catalogue(permission: str) -> None:
    if permission in PENDING_PERMISSIONS:
        assert permission not in CATALOG, f"{permission} exists: remove it from PENDING_PERMISSIONS"
        pytest.skip(f"pending: {permission} ({PENDING_PERMISSIONS[permission]})")
    assert permission in CATALOG, f"sensitive permission {permission!r} missing from the catalogue"


# ---------------------------------------------------------------- Epic 52 (A5, FR-262): Phase 3 routers
P3_ROUTER_MODULES: tuple[str, ...] = tuple(
    f"app.router.{m}"
    for m in (
        "skills", "segments", "talent_pools", "campaigns", "assessments", "mobility", "referrals", "campus",
        "workforce", "projects", "immigration", "report_builder", "analytics", "fairness", "client_sso", "dsc",
        "tds_recon", "fx_feed", "semantic",
    )
)  # fmt: skip
# (method, path) -> justification: Phase 3 internal routes without a require() dependency. All of them authenticate
# through ``current_user`` (session or API key, Epic 12 resolver), so they stay API-key usable.
_SELF = "self-service for any active internal User (own records only; P1 /me pattern)"
_SVC = "permission checked in the service (mobility:manage / mobility:transfer / demand scope); Epic 41 owner note"
P3_EXEMPT_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/me/referrals"): _SELF,
    ("GET", "/api/v1/me/referrals/openings"): _SELF,
    ("POST", "/api/v1/me/referrals"): _SELF,
    ("POST", "/api/v1/me/referrals/share-links"): _SELF,
    ("POST", "/api/v1/me/referrals/{referral_id}/withdraw"): _SELF,
    ("GET", "/api/v1/mobility/openings"): _SELF,
    ("GET", "/api/v1/mobility/openings/{demand_id}"): _SELF,
    ("GET", "/api/v1/mobility/me/profile"): _SELF,
    ("POST", "/api/v1/mobility/openings/{demand_id}/apply"): _SELF,
    ("GET", "/api/v1/mobility/my-applications"): _SELF,
    ("POST", "/api/v1/mobility/applications/{mobility_application_id}/withdraw"): _SELF,
    ("GET", "/api/v1/mobility/settings"): _SVC,
    ("PATCH", "/api/v1/mobility/settings"): _SVC,
    ("GET", "/api/v1/mobility/demands/{demand_id}/rules"): _SVC,
    ("PUT", "/api/v1/mobility/demands/{demand_id}/rules"): _SVC,
    ("GET", "/api/v1/mobility/profiles"): _SVC,
    ("PUT", "/api/v1/mobility/profiles"): _SVC,
    ("POST", "/api/v1/mobility/profiles/import"): _SVC,
    ("GET", "/api/v1/mobility/applications"): _SVC,
    ("GET", "/api/v1/mobility/applications/by-application/{application_id}"): _SVC,
    ("GET", "/api/v1/mobility/applications/{mobility_application_id}/transfer-context"): _SVC,
    ("POST", "/api/v1/mobility/applications/{mobility_application_id}/confirm-transfer"): _SVC,
}


def _p3_internal_routes() -> list[APIRoute]:
    return [
        r
        for r in _routes()
        if getattr(r.endpoint, "__module__", "") in P3_ROUTER_MODULES and not r.path.startswith(PORTAL_PREFIXES)
        and not r.path.startswith(("/api/v1/public/", "/api/v1/hooks/"))
    ]


def test_p3_internal_routes_require_a_permission() -> None:
    routes = _p3_internal_routes()
    assert len({r.endpoint.__module__ for r in routes}) >= 15, "Phase 3 routers not mounted?"
    missing = [
        key
        for route in routes
        if not any(_is_permission_dep(c) for c in _calls(route.dependant))
        for key in _key(route)
        if key not in P3_EXEMPT_ROUTES
    ]
    assert not missing, f"Phase 3 routes without require()/require_any() (not API-key usable): {missing}"
    by_key = {key: route for route in routes for key in _key(route)}
    stale = [k for k in P3_EXEMPT_ROUTES if k not in by_key]
    assert not stale, f"P3_EXEMPT_ROUTES no longer exist: {stale}"
    from app.dependencies.auth import current_user

    for key in P3_EXEMPT_ROUTES:
        assert current_user in _calls(by_key[key].dependant), f"{key}: must authenticate via current_user"


def test_self_id_answers_only_on_candidate_portal() -> None:
    """NFR-38: individual self-ID answers are readable only by the Candidate through ``portal_self_id`` (candidate
    principal); no API-key reachable route path contains ``self-id``."""
    for route in _routes():
        if "self-id" not in route.path and "self_id" not in route.path:
            continue
        module = getattr(route.endpoint, "__module__", "")
        calls = _calls(route.dependant)
        api_key_reachable = current_principal in calls or any(_is_permission_dep(c) for c in calls)
        assert not api_key_reachable, f"{route.path} exposes self-ID to internal / API-key principals"
        if route.methods & {"GET"} and not route.path.endswith("/self-id-form"):  # the form = question labels only
            assert module == "app.router.portal_self_id", f"{route.path}: self-ID read outside the candidate portal"
            assert require_candidate in calls, f"{route.path} must authenticate the candidate"


# ---------------------------------------------------------------- Epic 65 (A8, FR-329): Phase 4 routers
P4_ROUTER_MODULES: tuple[str, ...] = tuple(
    f"app.router.{m}"
    for m in (
        "agents", "agent_proposals", "ai_governance", "demand_intake", "demand_insights", "sourcing_agent",
        "screening_agent", "voice", "recording_policy", "scheduling_agent", "interview_captures", "submission_packs",
        "knowledge", "chasing", "timesheet_flags", "collection_rules",
    )
)  # fmt: skip
_OWN = "authenticated internal User; the service checks owner / subject read (foundation 0.16)"
_ME = "self-service for the signed-in User (own records only; P1 /me pattern)"
_PART = "interview participant check in the service (Epic 59 / 60 owner rule)"
P4_EXEMPT_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/interviews/{interview_id}/reschedule-requests"): _PART,
    ("GET", "/api/v1/me/scheduling-preferences"): _ME,
    ("PUT", "/api/v1/me/scheduling-preferences"): _ME,
    ("GET", "/api/v1/interviews/{interview_id}/scorecard-draft"): _PART,
    ("POST", "/api/v1/interviews/{interview_id}/scorecard-draft/opened"): _PART,
    ("POST", "/api/v1/interviews/{interview_id}/scorecard-draft/dismiss"): _PART,
    ("GET", "/api/v1/me/interview-recording-ack"): _ME,
    ("POST", "/api/v1/me/interview-recording-ack"): _ME,
    ("DELETE", "/api/v1/me/interview-recording-ack"): _ME,
    ("GET", "/api/v1/agent-runs"): _OWN,
    ("GET", "/api/v1/agent-runs/{run_id}"): _OWN,
    ("POST", "/api/v1/agent-runs"): _OWN + "; human only",
    ("POST", "/api/v1/agent-runs/{run_id}/cancel"): _OWN + "; human only",
    ("GET", "/api/v1/proposals/{proposal_id}"): _OWN,
    ("POST", "/api/v1/proposals/{proposal_id}/confirm"): "Copilot draft owner only; human only",
    ("POST", "/api/v1/proposals/{proposal_id}/discard"): "Copilot draft owner only; human only",
}
# FR-329: API keys get 403 human_only_action on these (the handler calls require_human / the router's _human()).
P4_HUMAN_ONLY_ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/api/v1/proposals/{proposal_id}/decide"),
    ("POST", "/api/v1/proposals/bulk-decide"),
    ("POST", "/api/v1/proposals/{proposal_id}/confirm"),
    ("POST", "/api/v1/proposals/{proposal_id}/discard"),
    ("POST", "/api/v1/ai/kill-switch"),
    ("POST", "/api/v1/ai/agentic/enable"),
    ("PUT", "/api/v1/agents/{key}/enabled"),
    ("PATCH", "/api/v1/agents/{key}"),
    ("PUT", "/api/v1/agents/{key}/actions/{tool_key}"),
    ("POST", "/api/v1/agents/{key}/burn-in/end"),
    ("POST", "/api/v1/agents/{key}/checklist/acknowledge"),
    ("POST", "/api/v1/agent-runs/{run_id}/payload"),
    ("POST", "/api/v1/ai-governance/proposals/{proposal_id}/opened"),
)


def _p4_internal_routes() -> list[APIRoute]:
    return [
        r
        for r in _routes()
        if getattr(r.endpoint, "__module__", "") in P4_ROUTER_MODULES and not r.path.startswith(PORTAL_PREFIXES)
        and not r.path.startswith(("/api/v1/public/", "/api/v1/hooks/", "/api/v1/inbound/"))
    ]


def test_p4_internal_routes_require_a_permission() -> None:
    routes = _p4_internal_routes()
    assert len({r.endpoint.__module__ for r in routes}) >= 10, "Phase 4 routers not mounted?"
    missing = [
        key
        for route in routes
        if not any(_is_permission_dep(c) for c in _calls(route.dependant))
        for key in _key(route)
        if key not in P4_EXEMPT_ROUTES
    ]
    assert not missing, f"Phase 4 routes without require()/require_any() (not API-key usable): {missing}"


def test_p4_human_only_routes_guard_humans() -> None:
    by_key = {key: route for route in _routes() for key in _key(route)}
    absent = [k for k in P4_HUMAN_ONLY_ROUTES if k not in by_key]
    assert absent == [], f"human-only routes missing: {absent}"
    for key in P4_HUMAN_ONLY_ROUTES:
        source = inspect.getsource(by_key[key].endpoint)
        assert "_human()" in source or "require_human" in source, f"{key}: no human-only guard"
