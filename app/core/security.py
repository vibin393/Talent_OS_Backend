"""Security primitives shared by every principal (AD-10).

* Passwords: argon2id (``hash_password`` / ``verify_password``).
* Opaque tokens (sessions, invitations, resets): ``new_token()`` returns 32 random bytes (url-safe);
  only ``hash_token(token)`` (SHA-256 hex) is ever stored.
* Cookies: one httpOnly session cookie per principal (``tos_session``, ``tos_candidate``,
  ``tos_platform``), SameSite=Lax, ``Secure`` in production, plus the readable ``tos_csrf`` cookie.
* Encryption at rest: ``fernet(purpose)`` returns the Fernet for a configured key (``AUDIT_MASTER_KEY``,
  ``MFA_ENCRYPTION_KEY``); ``encrypt_str`` / ``decrypt_str`` wrap it.
* CSRF: double-submit -- a mutating request authenticated by a cookie must send ``X-CSRF-Token`` equal
  to the ``tos_csrf`` cookie (``verify_csrf``). Story 22.9 (A10): the token is bound to the session --
  ``tos_csrf = HMAC(csrf key, session token)`` (``csrf_token_for``) -- so a token minted for session A is
  refused on session B; every sign-in / re-issue mints the token from the new session (rotation).
"""

import base64
import functools
import hashlib
import hmac
import secrets
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request, Response

from app.core.errors import Forbidden
from app.core.permissions import Principal, PrincipalType
from app.core.settings import get_settings

# ``Principal`` (with resolved permissions) lives in ``app.core.permissions``; re-exported here.
__all__ = ["InvalidToken", "Principal", "PrincipalType"]

USER_COOKIE = "tos_session"
CANDIDATE_COOKIE = "tos_candidate"
PLATFORM_COOKIE = "tos_platform"
CSRF_COOKIE = "tos_csrf"
# Short-lived MFA challenge (password accepted, code pending) for users and platform operators (story 1.4).
MFA_COOKIE = "tos_mfa"
CSRF_HEADER = "X-CSRF-Token"
# Phase 2 external principals (AD-26): Client Users and Vendor Users have their own session cookies.
CLIENT_COOKIE = "tos_client"
VENDOR_COOKIE = "tos_vendor"

SESSION_COOKIES: dict[PrincipalType, str] = {
    "user": USER_COOKIE,
    "candidate": CANDIDATE_COOKIE,
    "platform": PLATFORM_COOKIE,
    "client_user": CLIENT_COOKIE,
    "vendor_user": VENDOR_COOKIE,
}

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

TOKEN_BYTES = 32
MIN_PASSWORD_LENGTH = 10


# ---------------------------------------------------------------- passwords
_hasher = PasswordHasher()  # argon2id with the library's current recommended parameters
# Verified against when the account does not exist, so unknown emails cost the same as wrong passwords.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-work check; ``password_hash=None`` still runs a full argon2 verification."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


# ---------------------------------------------------------------- tokens
def new_token() -> str:
    """32 random bytes, url-safe base64 (43 chars)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """SHA-256 hex digest; the only form in which tokens are persisted."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- encryption at rest (Fernet)
# Epic 66 (spec 0.3): ``vault`` names the Secrets Vault master (``vault_master_keys``, ``core.kms``); it has no
# Fernet key here (``key_material("vault")`` is never called), only ``dev_fernet_key("vault")`` seeds the dev master.
KeyPurpose = Literal["audit", "mfa", "identity", "connector", "sso", "demographic", "agent_payload", "vault"]
_KEY_SETTINGS: dict[KeyPurpose, str] = {
    "audit": "audit_master_key",
    "mfa": "mfa_encryption_key",
    # Epic 13 (AD-35): master key for per-tenant identity/bank encryption (HKDF-derived, ``core.encrypted``).
    "identity": "identity_encryption_key",
    # Epic 13 (AD-29): provider connector credentials and webhook secrets.
    "connector": "connector_encryption_key",
    # Epic 14 (AD-30): identity-provider secrets and SSO login-state PKCE verifiers.
    "sso": "sso_encryption_key",
    # Epic 36 (spec 0.10): per-tenant demographic self-ID master key (Epic 49, ``core.encrypted``).
    "demographic": "demographic_encryption_key",
    # Epic 53 (AD-58, NFR-47): per-tenant agent payload store master key (``agent_payloads``, ``core.encrypted``).
    "agent_payload": "agent_payload_encryption_key",
}


def dev_fernet_key(purpose: str) -> str:
    """Deterministic development key for ``purpose``. Never used in production (settings refuse it)."""
    digest = hashlib.sha256(f"talent-os-dev-fernet-key:{purpose}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode()


@functools.cache
def _fernet_for_key(key: str) -> Fernet:
    return Fernet(key.encode())


def key_material(purpose: KeyPurpose) -> str:
    """The configured Fernet key (url-safe base64) for ``purpose``, or its dev key outside production."""
    s = get_settings()
    configured = str(getattr(s, _KEY_SETTINGS[purpose]))
    if not configured:
        if s.app_env == "production":  # pragma: no cover - settings validation refuses this earlier
            msg = f"{_KEY_SETTINGS[purpose].upper()} is not configured"
            raise RuntimeError(msg)
        configured = dev_fernet_key(purpose)
    return configured


def fernet(purpose: KeyPurpose) -> Fernet:
    """Return the Fernet for ``purpose``: the configured key, or the dev key outside production."""
    return _fernet_for_key(key_material(purpose))


def encrypt_str(purpose: KeyPurpose, value: str) -> str:
    return fernet(purpose).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_str(purpose: KeyPurpose, token: str) -> str:
    """Decrypt ``token``; raises ``InvalidToken`` when it was not produced with this purpose's key."""
    return fernet(purpose).decrypt(token.encode("ascii")).decode("utf-8")


# ---------------------------------------------------------------- cookies
def _secure() -> bool:
    return get_settings().app_env == "production"


def set_session_cookie(response: Response, cookie_name: str, token: str, max_age: int) -> None:
    response.set_cookie(
        cookie_name, token, max_age=max_age, path="/", httponly=True, samesite="lax", secure=_secure()
    )


def set_csrf_cookie(response: Response, token: str, max_age: int) -> None:
    # Readable by the frontend (not httpOnly) so it can echo it in the X-CSRF-Token header.
    response.set_cookie(  # NOSONAR
        CSRF_COOKIE, token, max_age=max_age, path="/", httponly=False, samesite="lax", secure=_secure()
    )


def clear_cookie(response: Response, cookie_name: str, *, httponly: bool = True) -> None:
    response.delete_cookie(cookie_name, path="/", httponly=httponly, samesite="lax", secure=_secure())


def clear_auth_cookies(response: Response, cookie_name: str) -> None:
    """Expire the principal's session cookie and the CSRF cookie."""
    clear_cookie(response, cookie_name)
    clear_cookie(response, CSRF_COOKIE, httponly=False)


# ---------------------------------------------------------------- CSRF
def csrf_failed() -> Forbidden:
    return Forbidden("CSRF token missing or invalid", code="csrf_failed")


def verify_csrf(request: Request) -> None:
    """Raise ``Forbidden(code="csrf_failed")`` unless a mutating request carries a matching CSRF token.

    Call only for cookie-authenticated principals (bearer API keys are not CSRF-prone).
    """
    if request.method.upper() in SAFE_METHODS:
        return
    cookie = request.cookies.get(CSRF_COOKIE) or ""
    header = request.headers.get(CSRF_HEADER) or ""
    if not cookie or not header or not hmac.compare_digest(cookie.encode(), header.encode()):
        raise csrf_failed()
    # Story 22.9: the token must be the HMAC of a session cookie this request presents.
    bound = [csrf_token_for(t) for name in CSRF_BOUND_COOKIES if (t := request.cookies.get(name))]
    if not any(hmac.compare_digest(cookie.encode(), b.encode()) for b in bound):
        raise csrf_failed()


# Session cookies a CSRF token may be bound to (``tos_support`` sessions are read-only but listed anyway).
CSRF_BOUND_COOKIES: tuple[str, ...] = (*SESSION_COOKIES.values(), "tos_support")


def _csrf_key() -> bytes:
    """HMAC key for CSRF tokens, derived from the configured audit master key (dev key outside production)."""
    return hashlib.sha256(b"talent-os-csrf:" + key_material("audit").encode()).digest()


def csrf_token_for(session_token: str) -> str:
    """Story 22.9 (A10): the session-bound CSRF token, ``HMAC-SHA256(csrf key, session token)``."""
    mac = hmac.new(_csrf_key(), session_token.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")


def csrf_token_from_request(request: Request, *cookie_names: str) -> str:
    """CSRF token bound to the first of ``cookie_names`` the request carries (``/csrf`` re-issue)."""
    for name in cookie_names:
        if token := request.cookies.get(name):
            return csrf_token_for(token)
    return new_token()
