# ruff: noqa: E501
"""Developer Portal facade (Epic 74, AD-85). Routers and other epics call this module only.

* Accounts / sessions / MFA (74.1): ``signup``, ``verify_email``, ``login``, ``mfa_enroll_start|confirm``, ``mfa_verify``,
  ``logout``, ``request_password_reset``, ``reset_password``, ``close_account``, ``me``, ``find_session``.
* Orgs, invitations, audit, verification (74.1); partner App credentials (74.4); submissions (74.5).
* Sandboxes, handoff, playground (74.3), conformance runs (74.4) and usage (74.5) take an ``Owner`` built by
  ``partner_owner(dctx)`` (partner workspace) or ``tenant_owner(ctx)`` (Tenant Developers with ``app:develop``).
* ``sandbox_owners(tenant_id)`` is for Epic 79's inactivity purge warnings.
"""

from app.services.developer_portal.accounts import (
    SESSION_PREFIX,
    close_account,
    find_session,
    login,
    logout,
    me,
    mfa_enroll_confirm,
    mfa_enroll_start,
    mfa_verify,
    orgs_of,
    purge_unverified,
    request_password_reset,
    reset_password,
    signup,
    verify_email,
)
from app.services.developer_portal.apps import (
    create_org_app,
    get_org_app,
    list_org_app_secrets,
    list_org_apps,
    retire_org_app,
    revoke_org_app_secret,
    rotate_org_app_secret,
    update_org_app,
)
from app.services.developer_portal.audit_log import LABELS as AUDIT_LABELS
from app.services.developer_portal.conformance.runner import (
    download_report,
    finalize_run,
    finish_run,
    get_run,
    list_runs,
    start_run,
)
from app.services.developer_portal.erasure import erase_account
from app.services.developer_portal.handoff import consume_handoff, open_sandbox
from app.services.developer_portal.orgs import (
    accept_invitation,
    create_org,
    expire_invitations,
    get_org,
    invite,
    members,
    org_audit,
    remove_member,
    set_member_role,
    update_org,
)
from app.services.developer_portal.owner import Owner
from app.services.developer_portal.owner import partner as partner_owner
from app.services.developer_portal.owner import tenant as tenant_owner
from app.services.developer_portal.playground import playground_exchange, playground_token
from app.services.developer_portal.sandboxes import (
    create_sandbox,
    delete_sandbox,
    install_in_sandbox,
    list_sandboxes,
    reset_sandbox,
    sandbox_owners,
)
from app.services.developer_portal.submissions import (
    listing_draft,
    save_version,
    submissions,
    submit,
    withdraw,
)
from app.services.developer_portal.usage import app_usage
from app.services.developer_portal.verification import (
    decide_verification,
    expire_verifications,
    get_request,
    get_verification,
    review_queue,
    save_verification,
    submit_verification,
)

__all__ = [
    "AUDIT_LABELS",
    "SESSION_PREFIX",
    "Owner",
    "accept_invitation",
    "app_usage",
    "close_account",
    "consume_handoff",
    "create_org",
    "create_org_app",
    "create_sandbox",
    "decide_verification",
    "delete_sandbox",
    "download_report",
    "erase_account",
    "expire_invitations",
    "expire_verifications",
    "finalize_run",
    "find_session",
    "finish_run",
    "get_org",
    "get_org_app",
    "get_request",
    "get_run",
    "get_verification",
    "install_in_sandbox",
    "invite",
    "list_org_app_secrets",
    "list_org_apps",
    "list_runs",
    "list_sandboxes",
    "listing_draft",
    "login",
    "logout",
    "me",
    "members",
    "mfa_enroll_confirm",
    "mfa_enroll_start",
    "mfa_verify",
    "open_sandbox",
    "org_audit",
    "orgs_of",
    "partner_owner",
    "playground_exchange",
    "playground_token",
    "purge_unverified",
    "remove_member",
    "request_password_reset",
    "reset_password",
    "reset_sandbox",
    "retire_org_app",
    "review_queue",
    "revoke_org_app_secret",
    "rotate_org_app_secret",
    "sandbox_owners",
    "save_verification",
    "save_version",
    "set_member_role",
    "signup",
    "start_run",
    "submissions",
    "submit",
    "submit_verification",
    "tenant_owner",
    "update_org",
    "update_org_app",
    "verify_email",
    "withdraw",
]
