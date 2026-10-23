# ruff: file-ignore[line-too-long]
"""Scope catalogue v1 (Epic 66, spec 0.5, FR-332/FR-376). Registered from ``oauth/hooks.py``. Later epics add scopes
only through ``core.oauth_scopes.register_scope`` from their own ``hooks.py`` (e.g. 68 ``webhooks:manage``).

Resolved permission keys (builder decisions, spec 0.5): ``postings:*`` -> ``demand:read`` / ``demand:update`` (no posting
key exists); ``invoices:read`` -> ``billing:read``; ``pii.identity:read`` -> ``identity:view``; ``pii.financial:read`` ->
``compensation:view`` (bank fields are unmasked by ``identity:view``: the financial scope maps both groups' field
masking, permission ``compensation:view`` only); ``documents:read`` -> ``document:read``; ``mcp:draft`` ->
``message:draft``.
"""

from app.core.oauth_scopes import ScopeSpec, register_scope, scopes

_P = "Lets the app {verb} {what} in your workspace."
_BASE: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "candidates:read",
        "Read candidates",
        "read candidate profiles (contact, identity and financial fields stay hidden unless granted separately)",
        ("candidate:read",),
    ),
    (
        "candidates:write",
        "Create and edit candidates",
        "create and update candidate profiles",
        ("candidate:create", "candidate:update"),
    ),
    ("applications:read", "Read applications", "read applications and their stages", ("application:read",)),
    (
        "applications:write",
        "Create and move applications",
        "create applications and move them between stages (never reject; people decide)",
        ("application:create", "application:update", "application:move"),
    ),
    ("demands:read", "Read demands", "read demands (job requirements)", ("demand:read",)),
    (
        "demands:write",
        "Create draft demands",
        "create and update demands as drafts",
        ("demand:create", "demand:update"),
    ),
    ("postings:read", "Read job postings", "read career-site job postings", ("demand:read",)),
    (
        "postings:write",
        "Publish job postings",
        "publish and close career-site job postings",
        ("demand:update",),
    ),
    ("clients:read", "Read clients", "read client accounts", ("client:read",)),
    ("interviews:read", "Read interviews", "read interview schedules", ("interview:read",)),
    ("offers:read", "Read offer status", "read the status of offers", ("offer:read",)),
    ("placements:read", "Read placements", "read placements", ("placement:read",)),
    ("workers:read", "Read workers", "read worker profiles", ("worker:read",)),
    ("timesheets:read", "Read timesheets", "read timesheets", ("timesheet:read",)),
    ("invoices:read", "Read invoices", "read invoices, credit notes and payments", ("billing:read",)),
    ("users:read", "Read users", "read the user directory", ("user:read",)),
    ("reports:read", "Read reports", "read dashboards and reports", ("report:read",)),
)
_SENSITIVE: tuple[tuple[str, str, str, tuple[str, ...], frozenset[str]], ...] = (
    (
        "pii.contact:read",
        "See contact details",
        "see email addresses and phone numbers",
        (),
        frozenset({"contact"}),
    ),
    (
        "pii.identity:read",
        "See identity details",
        "see PAN, Aadhaar references, dates of birth and bank details",
        ("identity:view",),
        frozenset({"identity"}),
    ),
    (
        "pii.financial:read",
        "See compensation",
        "see salaries, rates and bank details",
        ("compensation:view",),
        frozenset({"financial"}),
    ),
    (
        "documents:read",
        "Read documents",
        "read documents attached to records",
        ("document:read",),
        frozenset({"documents"}),
    ),
    (
        "recordings:read",
        "Access recordings",
        "access call and interview recordings and transcripts",
        (),
        frozenset({"recordings"}),
    ),
)
_MCP: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("mcp:read", "Read through an AI assistant", "read records through an AI assistant (MCP) for you", ()),
    (
        "mcp:draft",
        "Draft through an AI assistant",
        "prepare drafts through an AI assistant (MCP) that you review",
        ("message:draft",),
    ),
    (
        "mcp:propose",
        "Propose actions through an AI assistant",
        "propose actions through an AI assistant (MCP) that a person approves",
        (),
    ),
)


def register_v1() -> None:
    if "candidates:read" in scopes():
        return
    for key, label, what, perms in _BASE:
        register_scope(
            ScopeSpec(
                key=key,
                label=label,
                description=f"Lets the app {what}.",
                purpose_text=f"To {what} for the integration you install.",
                permissions=perms,
                resources=frozenset({"api", "mcp"}),
            )
        )
    for key, label, what, perms, groups in _SENSITIVE:
        register_scope(
            ScopeSpec(
                key=key,
                label=label,
                description=f"Lets the app {what}.",
                purpose_text=f"To {what}; you state the purpose when you grant it.",
                permissions=perms,
                sensitive=True,
                field_groups=groups,
                resources=frozenset({"api"})
                if key == "pii.identity:read"
                else frozenset({"api", "mcp"}),  # FR-379
                api_key_allowed=False,
            )
        )
    for key, label, what, perms in _MCP:
        register_scope(
            ScopeSpec(
                key=key,
                label=label,
                description=f"Lets the app {what}.",
                purpose_text=f"To {what}.",
                permissions=perms,
                resources=frozenset({"mcp"}),
                api_key_allowed=False,
            )
        )
    register_scope(
        ScopeSpec(
            key="offline_access",
            label="Stay connected",
            description="Lets the app keep access while you are away (refresh tokens).",
            purpose_text="To keep the integration working without asking you to sign in again.",
            permissions=(),
            resources=frozenset({"api", "mcp"}),
            api_key_allowed=False,
        )
    )


__all__ = ["register_v1"]
