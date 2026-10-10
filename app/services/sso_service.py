"""Facade of the ``sso`` domain (Epic 14, FR-77, AD-30). Routers and identity call sites import from here.

Epic 50 (FR-250..FR-252, AD-48): Client Portal SSO -- per-Client identity providers (``client_admin``), the
client sign-in branch (``client``) and ``client_login_required`` (the magic-link enforcement decision).
"""

from app.services.sso import client_admin
from app.services.sso.client import discover as client_discover
from app.services.sso.client_admin import (
    approval_links_allowed,
    assert_approval_link_allowed,
    client_login_required,
)
from app.services.sso.login import (
    LoginResult,
    complete_oidc,
    complete_saml,
    discover,
    error_code,
    failure_audience,
    safe_next,
    start,
)
from app.services.sso.service import (
    assert_break_glass_preserved,
    create_provider,
    delete_provider,
    enforcement_active,
    get_provider,
    list_break_glass,
    list_providers,
    password_allowed,
    set_break_glass,
    set_group_roles,
    sp_metadata,
    test_login_url,
    update_provider,
)

__all__ = [
    "LoginResult",
    "approval_links_allowed",
    "assert_approval_link_allowed",
    "assert_break_glass_preserved",
    "client_admin",
    "client_discover",
    "client_login_required",
    "complete_oidc",
    "complete_saml",
    "create_provider",
    "delete_provider",
    "discover",
    "enforcement_active",
    "error_code",
    "failure_audience",
    "get_provider",
    "list_break_glass",
    "list_providers",
    "password_allowed",
    "safe_next",
    "set_break_glass",
    "set_group_roles",
    "sp_metadata",
    "start",
    "test_login_url",
    "update_provider",
]
