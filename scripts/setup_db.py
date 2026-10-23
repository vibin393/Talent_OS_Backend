"""Idempotent local DB setup: create the dev and test databases if missing and migrate both to head.

Usage (from Backend/):
    ./venv/Scripts/python.exe scripts/setup_db.py              # dev (DB_NAME) + test (TEST_DB_NAME)
    ./venv/Scripts/python.exe scripts/setup_db.py --test-only
    ./venv/Scripts/python.exe scripts/setup_db.py --reset-test # drop + recreate the TEST database first

Runs as the owner role (DB_USER). The test database is never dropped unless --reset-test is passed; if it
exists but holds a schema from another project (unknown alembic revision), the script stops and says so.
"""

import argparse
import asyncio
import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import asyncpg  # noqa: E402

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from app.core.settings import get_settings  # noqa: E402


class ForeignDatabaseError(RuntimeError):
    pass


def alembic_config(db_name: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.attributes["db_url"] = get_settings().owner_db_url(db_name)
    cfg.attributes["configure_logger"] = False
    return cfg


async def _connect(db_name: str) -> asyncpg.Connection:
    s = get_settings()
    return await asyncpg.connect(
        user=s.db_user, password=s.db_password, host=s.db_host, port=s.db_port, database=db_name
    )


async def _ensure_database(db_name: str, *, reset: bool) -> bool:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", db_name):
        msg = f"unsafe database name: {db_name!r}"
        raise ValueError(msg)
    conn = await _connect("postgres")
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db_name)
        if exists and reset:
            await conn.execute(f'DROP DATABASE "{db_name}" WITH (FORCE)')
            exists = None
        if not exists:
            await conn.execute(f'CREATE DATABASE "{db_name}"')
            return True
        return False
    finally:
        await conn.close()


async def _current_revision(db_name: str) -> str | None:
    conn = await _connect(db_name)
    try:
        has_table = await conn.fetchval("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        if not has_table:
            return None
        rev = await conn.fetchval("SELECT version_num FROM alembic_version LIMIT 1")
        return str(rev) if rev is not None else None
    finally:
        await conn.close()


def ensure_database(db_name: str, *, reset: bool = False) -> bool:
    """Create ``db_name`` if missing (drop first when ``reset``). Returns True if it was created."""
    return asyncio.run(_ensure_database(db_name, reset=reset))


def check_not_foreign(db_name: str) -> None:
    rev = asyncio.run(_current_revision(db_name))
    if rev is None:
        return
    known = {s.revision for s in ScriptDirectory.from_config(alembic_config(db_name)).walk_revisions()}
    if rev not in known:
        msg = (
            f"Database '{db_name}' is at alembic revision '{rev}', which is not part of this project. "
            "It probably belongs to another project. Refusing to touch it. Either point TEST_DB_NAME at "
            "another database or re-run with --reset-test to drop and recreate it."
        )
        raise ForeignDatabaseError(msg)


def migrate(db_name: str, schema: str | None = None) -> None:
    """Upgrade ``db_name`` to head. When ``schema`` is given, the whole Talent OS chain runs INTO that
    schema (schema-per-app / company-DB model): alembic creates the schema, pins the migration connection's
    ``search_path`` to it, and keeps ``alembic_version`` there too (see ``alembic/env.py``). A None schema
    keeps the legacy public-schema behaviour for existing tenants."""
    cfg = alembic_config(db_name)
    if schema is not None:
        cfg.attributes["schema"] = schema
    command.upgrade(cfg, "heads")


def setup(db_name: str, *, reset: bool = False) -> None:
    created = ensure_database(db_name, reset=reset)
    if not created:
        check_not_foreign(db_name)
    migrate(db_name)
    print(f"[setup_db] {db_name}: {'created, ' if created else ''}migrated to head")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dev-only", action="store_true")
    group.add_argument("--test-only", action="store_true")
    parser.add_argument("--reset-test", action="store_true", help="drop and recreate the test DB first")
    args = parser.parse_args()

    s = get_settings()
    if s.test_db_name == s.db_name:
        print("[setup_db] ERROR: TEST_DB_NAME must differ from DB_NAME", file=sys.stderr)
        return 2
    try:
        if not args.test_only:
            setup(s.db_name)
        if not args.dev_only:
            setup(s.test_db_name, reset=args.reset_test)
    except ForeignDatabaseError as exc:
        print(f"[setup_db] ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
