"""Central (unified) module RBAC — the per-module capability layer of the Talent OS role system.

A tenant role already maps Talent OS permission -> scope (``app.core.permissions``). On top of that a role
may grant **module capabilities**: coarse feature flags for the other apps of the unified suite (Croar,
Feedback IQ, Job Portal). They are carried in the central SSO token (``central_token.mint_central_token``)
as the ``module_caps`` claim so those apps can honor them offline, without calling back to Talent OS.

The catalog here is the single source of truth for the capability keys. The SHARED CONTRACT (other apps
build to it) is::

    "module_caps": {
      "croar":      ["pilot", "sourcing", "automation", "assessments"],  # Croar's kept AI features only
      "feedbackiq": ["surveys", "recognition", "analytics", "admin"],
      "portal":     ["jobs", "candidates", "reports", "admin"]
    }

Croar is limited to its four AI features — recruiting / HR / payroll belong to Talent OS and are NOT here.
Only the caps a user actually holds appear in the claim (a restricted role yields a subset; an app with no
granted caps is omitted). The tenant Admin / owner role grants every cap dynamically (see
``effective_role_module_caps``), so existing admins keep full access with no data migration.
"""

import dataclasses
from collections.abc import Iterable, Mapping

from app.core.errors import ValidationFailed
from app.core.permissions import TENANT_ADMIN

# App keys exactly as they appear in the central-token ``module_caps`` claim (the shared contract).
CROAR = "croar"
FEEDBACKIQ = "feedbackiq"
PORTAL = "portal"


@dataclasses.dataclass(frozen=True, slots=True)
class ModuleCapability:
    key: str
    label: str
    description: str


@dataclasses.dataclass(frozen=True, slots=True)
class ModuleApp:
    key: str
    label: str
    description: str
    capabilities: tuple[ModuleCapability, ...]


# Ordered catalog, grouped by app. These keys are canonical — do not rename without updating every consumer.
MODULE_CATALOG: tuple[ModuleApp, ...] = (
    ModuleApp(
        CROAR,
        "Croar",
        "Croar's AI features embedded in the suite.",
        (
            ModuleCapability("pilot", "Pilot", "Use the Croar Pilot AI assistant."),
            ModuleCapability("sourcing", "AI Sourcing", "Run AI candidate sourcing and discovery."),
            ModuleCapability("automation", "Automation", "Build and run Croar AI automations."),
            ModuleCapability("assessments", "AI Assessments", "Create and review Croar AI assessments."),
        ),
    ),
    ModuleApp(
        FEEDBACKIQ,
        "Feedback IQ",
        "The Feedback IQ engagement and recognition app.",
        (
            ModuleCapability("surveys", "Surveys", "Create, send and respond to surveys."),
            ModuleCapability("recognition", "Recognition", "Give and manage peer recognition and rewards."),
            ModuleCapability("analytics", "Analytics", "View engagement and feedback analytics."),
            ModuleCapability("admin", "Administration", "Administer Feedback IQ settings and programs."),
        ),
    ),
    ModuleApp(
        PORTAL,
        "Job Portal",
        "The external Job Portal (recruiter / hiring surface).",
        (
            ModuleCapability("jobs", "Jobs", "Manage job postings on the portal."),
            ModuleCapability("candidates", "Candidates", "View and manage portal applicants."),
            ModuleCapability("reports", "Reports", "View portal reports and analytics."),
            ModuleCapability("admin", "Administration", "Administer portal settings and branding."),
        ),
    ),
)

# app -> ordered tuple of its capability keys. Used for validation and for the Admin's "grant all" default.
ALL_MODULE_CAPS: dict[str, tuple[str, ...]] = {
    app.key: tuple(c.key for c in app.capabilities) for app in MODULE_CATALOG
}
_VALID: dict[str, frozenset[str]] = {app: frozenset(caps) for app, caps in ALL_MODULE_CAPS.items()}

# The claim / storage shape: app -> list of granted capability keys.
ModuleCaps = dict[str, list[str]]


def catalog() -> tuple[ModuleApp, ...]:
    """The full capability catalog for the role-editor UI (``GET /admin/module-capabilities``)."""
    return MODULE_CATALOG


def _invalid(message: str) -> ValidationFailed:
    return ValidationFailed(
        message,
        code="invalid_module_cap",
        details={"fields": [{"field": "module_caps", "message": message, "type": "invalid_module_cap"}]},
    )


def validate_module_caps(raw: Mapping[str, Iterable[str]] | None) -> ModuleCaps:
    """Normalize a role's module-cap grants against the catalog.

    Unknown apps or capability keys raise 422 ``invalid_module_cap``. The result keeps only apps with at
    least one cap, each app's caps deduplicated and ordered by the catalog, so storage is canonical.
    """
    if not raw:
        return {}
    out: ModuleCaps = {}
    for app, caps in raw.items():
        if app not in _VALID:
            raise _invalid(f"Unknown app '{app}'")
        wanted = set(caps)
        unknown = sorted(wanted - _VALID[app])
        if unknown:
            raise _invalid(f"Unknown {app} capability: {', '.join(unknown)}")
        granted = [key for key in ALL_MODULE_CAPS[app] if key in wanted]  # catalog order
        if granted:
            out[app] = granted
    return out


def effective_role_module_caps(key: str, is_system: bool, stored: Mapping[str, Iterable[str]] | None) -> ModuleCaps:
    """Module caps a role confers. The system Tenant Admin always grants every current cap of every app
    (whatever JSON was stored), mirroring ``permissions.effective_role_grants``."""
    if is_system and key == TENANT_ADMIN:
        return {app: list(caps) for app, caps in ALL_MODULE_CAPS.items()}
    return validate_module_caps(stored)


def effective_module_caps(roles: Iterable[tuple[str, bool, Mapping[str, Iterable[str]] | None]]) -> ModuleCaps:
    """Union of module caps across a user's assigned roles.

    ``roles`` is an iterable of ``(role_key, is_system, stored_module_caps)``. Returns app -> sorted list of
    granted cap keys, with only apps that have at least one cap (empty app => no access, omitted).
    """
    merged: dict[str, set[str]] = {}
    for key, is_system, stored in roles:
        for app, caps in effective_role_module_caps(key, is_system, stored).items():
            merged.setdefault(app, set()).update(caps)
    return {
        app: [key for key in ALL_MODULE_CAPS[app] if key in merged[app]]  # catalog order
        for app in ALL_MODULE_CAPS
        if merged.get(app)
    }
