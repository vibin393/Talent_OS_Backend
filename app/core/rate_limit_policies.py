"""Rate-limit policies (NFR-16; Epic 12 A9). Applied by ``app.core.middleware.rate_limit`` (anonymous and
auth endpoints -- before any password hashing) and by the principal resolver (``user`` / ``tenant``),
``api_keys_service`` (``api_key``) and ``require_ai_feature`` (``ai_interactive``).

| Policy                         | Subject                  | Limit                          |
|--------------------------------|--------------------------|--------------------------------|
| ``ip_anon``                    | IP                       | 300/min (unauthenticated)      |
| ``auth_login``                 | IP · sha256(email)       | 10/min · 5 per 15 min          |
| ``auth_reset_request``         | IP · email hash          | 5/h · 3/h                      |
| ``auth_reset_confirm``         | IP                       | 10/h                           |
| ``invite_*``                   | IP                       | 20/h                           |
| ``platform_login``             | IP                       | 5/min                          |
| ``mfa_verify``                 | pending MFA session      | 10 per 5 min                   |
| ``user`` · ``tenant``          | user · tenant            | 600/min · 3000/min             |
| ``api_key``                    | key                      | ``rate_limit_per_min``         |
| ``ai_interactive``             | user                     | 30/min                         |
"""

import dataclasses
import re
from typing import Literal

SubjectKind = Literal["ip", "email", "mfa_cookie"]


@dataclasses.dataclass(frozen=True, slots=True)
class Limit:
    subject: SubjectKind
    limit: int
    window_seconds: int


@dataclasses.dataclass(frozen=True, slots=True)
class Policy:
    name: str
    method: str
    path: re.Pattern[str]
    limits: tuple[Limit, ...]


def _p(name: str, method: str, path: str, *limits: Limit) -> Policy:
    return Policy(name, method, re.compile(path), limits)


MINUTE, HOUR = 60, 3600

ENDPOINT_POLICIES: tuple[Policy, ...] = (
    _p("auth_login", "POST", r"^/api/v1/auth/login$", Limit("ip", 10, MINUTE), Limit("email", 5, 15 * MINUTE)),
    _p(
        "auth_reset_request",
        "POST",
        r"^/api/v1/auth/password-reset/request$",
        Limit("ip", 5, HOUR),
        Limit("email", 3, HOUR),
    ),
    _p("auth_reset_confirm", "POST", r"^/api/v1/auth/password-reset/confirm$", Limit("ip", 10, HOUR)),
    _p("invite_lookup", "POST", r"^/api/v1/auth/invitations/lookup$", Limit("ip", 20, HOUR)),
    _p("invite_accept", "POST", r"^/api/v1/auth/invitations/accept$", Limit("ip", 20, HOUR)),
    _p("invite_legacy", "GET|POST", r"^/api/v1/auth/invitations/[^/]+(/accept)?$", Limit("ip", 20, HOUR)),
    _p("platform_login", "POST", r"^/api/v1/platform/auth/login$", Limit("ip", 5, MINUTE)),
    _p("mfa_verify", "POST", r"^/api/v1/(platform/)?auth/mfa/verify$", Limit("mfa_cookie", 10, 5 * MINUTE)),
    # Epic 67 (spec 0.2): OAuth token / revoke / introspect -- IP 60/min (the client 600/min is applied after auth).
    _p("oauth_token", "POST", r"^/api/oauth/(token|revoke|introspect)$", Limit("ip", 60, MINUTE)),
    # Epic 74 (spec 0.2 / A6): developer sign-in (as auth_login), sign-up (IP 5/h), MFA verify, password reset.
    _p("developer_login", "POST", r"^/api/v1/developer/auth/login$", Limit("ip", 10, MINUTE), Limit("email", 5, 15 * MINUTE)),
    _p("developer_signup", "POST", r"^/api/v1/developer/auth/signup$", Limit("ip", 5, HOUR)),
    _p("developer_mfa", "POST", r"^/api/v1/developer/auth/mfa/verify$", Limit("ip", 10, 5 * MINUTE)),
    _p("developer_reset", "POST", r"^/api/v1/developer/auth/password-reset/(request|confirm)$", Limit("ip", 5, HOUR)),
    # Epic 75 (A6): anonymous Job Network reads (120/min per IP) and visitor reports (5/day per IP; also enforced per
    # ip_hash in the service).
    _p("network_read", "GET", r"^/api/v1/public/network/.*$", Limit("ip", 120, MINUTE)),
    _p("network_report", "POST", r"^/api/v1/public/network/jobs/[^/]+/report$", Limit("ip", 5, 24 * HOUR)),
)

IP_ANON = Limit("ip", 300, MINUTE)
USER_LIMIT = (600, MINUTE)
TENANT_LIMIT = (3000, MINUTE)
AI_INTERACTIVE_LIMIT = (30, MINUTE)
# Cookies / headers that mark a request as authenticated (then the per-user/tenant limits apply instead).
AUTH_COOKIES = ("tos_session", "tos_platform", "tos_candidate", "tos_support")


def match(method: str, path: str) -> Policy | None:
    for policy in ENDPOINT_POLICIES:
        if method in policy.method.split("|") and policy.path.fullmatch(path):
            return policy
    return None
