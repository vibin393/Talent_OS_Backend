# ruff: noqa: E501
"""Epic 67 registrations (spec A1 / A7): the 67 scopes (none sensitive, resource ``api``), the notification types
(``oauth_client_secret_expiring``) and the settings-hub entry for private Apps. Imported from ``oauth/hooks.py``."""

from app.core.oauth_scopes import ScopeSpec, register_scope, scopes

# key, label, description, permissions
E67_SCOPES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("contacts:read", "Read client contacts", "read client contacts (email and phone stay hidden unless granted separately)", ("contact:read",)),
    ("submissions:read", "Read submissions to clients", "read submissions of candidates to clients", ("submission:read",)),
    ("notes:read", "Read notes on records you can see", "read notes on records you can see", ("candidate:read",)),
    ("notes:write", "Add notes", "add notes to records", ("note:write",)),
    ("tasks:read", "Read tasks", "read tasks", ("task:read",)),
    ("tasks:write", "Create and update tasks", "create and update tasks", ("task:create", "task:update")),
    ("activities:read", "Read activity history on records you can see", "read activity history on records you can see", ("candidate:read",)),
    ("activities:write", "Log calls, meetings and emails as activities", "log calls, meetings and emails as activities", ("note:write",)),
    ("pools:read", "Read talent pools and their members", "read talent pools and their members", ("pool:read",)),
    ("expenses:read", "Read expense claims", "read expense claims", ("expense:read",)),
    ("skills:read", "Read the skills catalogue", "read the skills catalogue", ()),
    ("imports:write", "Bulk-import candidates and applications", "bulk-import candidates and applications", ("candidate:create", "application:create")),
)


def register_e67_scopes() -> None:
    for key, label, what, perms in E67_SCOPES:
        if key in scopes():
            continue
        register_scope(
            ScopeSpec(
                key=key,
                label=label,
                description=f"Lets the app {what}.",
                purpose_text=f"To {what} for the integration you install.",
                permissions=perms,
                resources=frozenset({"api"}),
                owner_epic="67",
            )
        )


register_e67_scopes()


def _register_settings_entry() -> None:
    from app.services import settings_service

    settings_service.register_settings_entry(
        "integrations",
        "private_apps",
        "Private apps",
        "Register your organisation's own apps for the Public API and MCP",
        "/api/v1/apps",
        "app:develop",
        owner_epic="67",
    )


_register_settings_entry()
