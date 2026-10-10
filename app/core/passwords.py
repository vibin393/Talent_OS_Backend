"""Password policy shared by every principal: at least ``MIN_PASSWORD_LENGTH`` characters and not one of
the 10k most common passwords (``data/common-passwords.txt``, SecLists ``10k-most-common.txt``, MIT).

The comparison is case-insensitive so ``Password1234`` is rejected like ``password1234``.
"""

from functools import lru_cache
from pathlib import Path

from app.core.security import MIN_PASSWORD_LENGTH

COMMON_PASSWORDS_FILE = Path(__file__).resolve().parent / "data" / "common-passwords.txt"
MAX_PASSWORD_LENGTH = 1024


@lru_cache(maxsize=1)
def common_passwords() -> frozenset[str]:
    """Load the bundled list (once). Raises if it is missing or empty; loaded at startup by the lifespan."""
    lines = COMMON_PASSWORDS_FILE.read_text(encoding="utf-8").splitlines()
    words = frozenset(line.strip().lower() for line in lines if line.strip())
    if not words:
        msg = f"common-password list is empty: {COMMON_PASSWORDS_FILE}"
        raise RuntimeError(msg)
    return words


def password_policy_violation(password: str) -> str | None:
    """Human-readable reason ``password`` is not acceptable, or None if it satisfies the policy."""
    stripped = password.strip()  # padding with spaces must not satisfy the length rule
    if len(stripped) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters (not counting surrounding spaces)"
    if len(password) > MAX_PASSWORD_LENGTH:
        return f"Password must be at most {MAX_PASSWORD_LENGTH} characters"
    if stripped.lower() in common_passwords():
        return "This password is too common; choose a less guessable one"
    return None
