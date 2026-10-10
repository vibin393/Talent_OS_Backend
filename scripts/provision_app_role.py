"""Provision the runtime ``talent_app`` role outside Alembic (story 22.9, A10).

Usage (from Backend/, as the owner role ``DB_USER``):
    DB_APP_PASSWORD=... ./venv/Scripts/python.exe scripts/provision_app_role.py [--database X] [--role Y]

* The password is read from the environment (``DB_APP_PASSWORD``; ``--password-env`` names another variable)
  and is never printed or logged.
* ``SET password_encryption = 'scram-sha-256'`` for the session, and the password is sent to the server only
  as a client-computed SCRAM-SHA-256 verifier (RFC 5802 / 7677), so the plaintext never appears in a
  statement, ``pg_stat_activity`` or the server log.
* The role is created (or re-asserted) ``LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION``
  and granted DML on the database (the same grants as the baseline migration). Idempotent.

In production the baseline migration's ``create_app_role`` refuses to run until this script created the role.
"""

import argparse
import asyncio
import base64
import hashlib
import hmac
import os
import secrets
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import asyncpg  # noqa: E402

from app.core.migration_helpers import _ident, _literal, grant_app_privileges_sql  # noqa: E402
from app.core.settings import get_settings  # noqa: E402

SCRAM_ITERATIONS = 4096


def scram_verifier(password: str, *, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS) -> str:
    """PostgreSQL's stored SCRAM-SHA-256 form: ``SCRAM-SHA-256$<iter>:<salt>$<StoredKey>:<ServerKey>``."""
    salt = salt if salt is not None else secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()

    def b64(raw: bytes) -> str:
        return base64.b64encode(raw).decode("ascii")

    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def role_sql(role: str, verifier: str) -> str:
    r = _ident(role)
    flags = "LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION"
    attrs = f"{flags} PASSWORD {_literal(verifier)}"
    return (
        "DO $tos_role$ BEGIN "
        f"IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_literal(r)}) THEN CREATE ROLE {r} {attrs}; "
        f"ELSE ALTER ROLE {r} {attrs}; END IF; END $tos_role$;"
    )


async def provision(role: str, password: str, database: str, *, grant: bool = True) -> None:
    if not password:
        msg = "the runtime role password is empty"
        raise ValueError(msg)
    s = get_settings()
    conn = await asyncpg.connect(
        user=s.db_user, password=s.db_password, host=s.db_host, port=s.db_port, database=database
    )
    try:
        await conn.execute("SET password_encryption = 'scram-sha-256'")
        await conn.execute(role_sql(role, scram_verifier(password)))
        if grant:
            for stmt in grant_app_privileges_sql(role):
                try:
                    await conn.execute(stmt)
                except asyncpg.UndefinedTableError:  # alembic_version absent before the first migration
                    continue
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    s = get_settings()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--role", default=s.db_app_user)
    parser.add_argument("--database", default=s.db_name)
    parser.add_argument("--password-env", default="DB_APP_PASSWORD", help="environment variable holding it")
    parser.add_argument("--no-grant", action="store_true", help="create / update the role only")
    args = parser.parse_args(argv)
    password = os.environ.get(args.password_env, "")
    if not password:
        print(f"error: set {args.password_env} (read from the environment only)", file=sys.stderr)
        return 1
    try:
        asyncio.run(provision(args.role, password, args.database, grant=not args.no_grant))
    except Exception as exc:  # the message never contains the password (only a verifier is ever sent)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"Provisioned role {args.role} (SCRAM-SHA-256) on {args.database}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
