"""Seed (or re-seed) the local super-admin platform operator for the unified-platform console.

Upserts a platform operator with KNOWN local credentials so the super-admin console
(``talentos.localhost/platform``) is usable immediately:

    email:    superadmin@talentos.local
    password: SuperAdmin!2026

Idempotent: re-running resets the password, re-activates the account and clears any login lockout.

MFA is intentionally left DISABLED. The console normally forces operators to enrol a second factor, but
``Settings.platform_operator_mfa_enforced`` suspends that policy when ``APP_ENV=development`` (as the
local stack is), so this operator can sign in and manage tenants without a TOTP dance. Staging/production
keep MFA in force, so this never weakens a real deployment.

Usage (inside the talentos-backend container, or from Backend/ with the platform DB env set):
    python scripts/seed_superadmin.py
    python scripts/seed_superadmin.py --email ops@talentos.local --password 'Secret!2026' --name "Ops"
"""

import argparse
import asyncio
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from app.core.database import platform_session  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.identity import PlatformOperator  # noqa: E402

DEFAULT_EMAIL = "superadmin@talentos.local"
DEFAULT_PASSWORD = "SuperAdmin!2026"  # noqa: S105 - local-only, documented dev credential
DEFAULT_NAME = "Super Admin"


async def seed(email: str, password: str, name: str) -> None:
    email = email.strip().lower()
    async with platform_session() as db:
        operator = (
            await db.execute(select(PlatformOperator).where(PlatformOperator.email == email))
        ).scalar_one_or_none()
        if operator is None:
            operator = PlatformOperator(email=email, name=name, password_hash=hash_password(password))
            db.add(operator)
            action = "created"
        else:
            operator.name = name
            operator.password_hash = hash_password(password)
            action = "updated"
        # Make it usable: active, no MFA, no lockout counters.
        operator.is_active = True
        operator.mfa_enabled = False
        operator.mfa_secret_encrypted = None
        operator.failed_login_count = 0
        operator.first_failed_at = None
        operator.locked_until = None
    print(f"super-admin {action}: {email} (password: {password}) — MFA disabled, active")  # noqa: T201


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the local super-admin platform operator")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--password", default=DEFAULT_PASSWORD)
    parser.add_argument("--name", default=DEFAULT_NAME)
    args = parser.parse_args(argv)
    asyncio.run(seed(args.email, args.password, args.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
