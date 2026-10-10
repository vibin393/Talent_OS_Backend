# ruff: noqa: E501
"""OAuth facade (Epic 66, spec 0.6; frozen signatures). Minting, authentication, revocation and the delegated
authorisation store are the foundation's; App management and the protocol endpoints are Epic 67's (same signatures)."""

from app.schemas.oauth import GrantType, IssuedTokens
from app.services.oauth.apps import (
    create_app,
    get_private_app,
    installation_for_app,
    list_connected_apps,
    list_private_apps,
    list_secrets,
    retire_app,
    revoke_secret,
    rotate_secret,
    scope_catalogue_out,
    set_app_status,
    update_app,
)
from app.services.oauth.authorizations import (
    list_my_authorizations,
    record_user_authorization,
    revoke_my_authorization,
)
from app.services.oauth.authorize import AuthorizePageError, OAuthRedirectError, check_query
from app.services.oauth.client_auth import OAuthError
from app.services.oauth.principals import (
    app_token_scopes,
    authenticate_bearer,
    clear_cache,
    delegated_permissions,
    ecosystem_suspended,
    invalid_token,
)
from app.services.oauth.protocol import (
    authorize_decision,
    authorize_request,
    introspect,
    metadata,
    revoke_token,
    token,
)
from app.services.oauth.tokens import (
    ACCESS_PREFIX,
    CODE_PREFIX,
    REFRESH_PREFIX,
    SECRET_PREFIX,
    check_scopes,
    hash_token,
    issue_token,
    purge_lookups,
    revoke_for,
)

__all__ = [
    "ACCESS_PREFIX",
    "CODE_PREFIX",
    "REFRESH_PREFIX",
    "SECRET_PREFIX",
    "AuthorizePageError",
    "GrantType",
    "IssuedTokens",
    "OAuthError",
    "OAuthRedirectError",
    "app_token_scopes",
    "authenticate_bearer",
    "authorize_decision",
    "authorize_request",
    "check_query",
    "check_scopes",
    "clear_cache",
    "create_app",
    "delegated_permissions",
    "ecosystem_suspended",
    "get_private_app",
    "hash_token",
    "installation_for_app",
    "introspect",
    "invalid_token",
    "issue_token",
    "list_connected_apps",
    "list_my_authorizations",
    "list_private_apps",
    "list_secrets",
    "metadata",
    "purge_lookups",
    "record_user_authorization",
    "retire_app",
    "revoke_for",
    "revoke_my_authorization",
    "revoke_secret",
    "revoke_token",
    "rotate_secret",
    "scope_catalogue_out",
    "set_app_status",
    "token",
    "update_app",
]
