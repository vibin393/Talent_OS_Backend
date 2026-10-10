"""Test harness.

* Points the app at the test database (TEST_DB_NAME, default ``talent_os_test``) BEFORE importing ``app``.
* Creates the test DB if missing and migrates it to head once per session (via scripts/setup_db.py, as the
  owner role). Tests then talk to it as the runtime role ``talent_app`` exactly like the API does.
"""

import os
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

BACKEND_DIR = Path(__file__).resolve().parents[1]
_DOTENV = dotenv_values(BACKEND_DIR / ".env")
TEST_DB_NAME = os.environ.get("TEST_DB_NAME") or _DOTENV.get("TEST_DB_NAME") or "talent_os_main_test"
DEV_DB_NAME = os.environ.get("DB_NAME") or _DOTENV.get("DB_NAME") or "talent_os_main"
if TEST_DB_NAME == DEV_DB_NAME:
    raise SystemExit(f"TEST_DB_NAME ({TEST_DB_NAME}) must differ from the dev DB_NAME; refusing to run tests")
os.environ["TEST_DB_NAME"] = TEST_DB_NAME
os.environ["DB_NAME"] = TEST_DB_NAME  # env vars override .env in pydantic-settings
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "true")
# Epic 5: deterministic hash-seeded embeddings; tests never download or load a sentence-transformer model.
os.environ["EMBEDDING_BACKEND"] = "fake"
# Emails go to a throwaway outbox, never to Backend/data/outbox.
os.environ["OUTBOX_DIR"] = tempfile.mkdtemp(prefix="talent_os_outbox_")
# Never deliver real email from tests, whatever Backend/.env says (it may configure SMTP for dev).
os.environ["MAILER_BACKEND"] = "outbox"
# Epic 12: rate limiting is off by default in tests (suites sign in many users from one "IP"); the rate-limit
# tests turn it on explicitly. The in-memory backend never touches Redis.
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["RATE_LIMIT_BACKEND"] = "memory"

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.core.database import dispose_engines, owner_engine  # noqa: E402
from app.core.migration_helpers import enable_rls_sql  # noqa: E402
from app.core.settings import get_settings  # noqa: E402

RLS_TEST_TABLE = "test_rls_items"


def pytest_configure(config: pytest.Config) -> None:
    get_settings.cache_clear()
    assert get_settings().db_name == TEST_DB_NAME, "tests must never run against the dev database"


@pytest.fixture(scope="session", autouse=True)
def _migrated_test_db() -> None:
    """Create + migrate the test DB once (subprocess keeps alembic's asyncio.run off the test loop)."""
    proc = subprocess.run(
        [sys.executable, str(BACKEND_DIR / "scripts" / "setup_db.py"), "--test-only"],
        cwd=BACKEND_DIR,
        env={k: v for k, v in os.environ.items() if k != "DB_NAME"},  # DB_NAME is forced to the test DB here
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.exit(f"Test DB setup failed for '{TEST_DB_NAME}':\n{proc.stderr[-2000:]}", returncode=2)


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "")


@pytest.fixture(scope="session", autouse=True)
def _socket_guard() -> Iterator[None]:
    """Epic 53 (53.4 AC, NFR-54): tests never open a network connection beyond localhost (DB, Redis, ClamAV).
    Model calls use the Fake LLM (``AI_PROVIDER`` keyless fallback / ``ai.fake_llm``)."""
    import socket

    original = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) and address else ""
        if isinstance(host, str) and host not in _LOCAL_HOSTS and not host.startswith("127."):
            msg = f"network access blocked in tests: {host}"
            raise RuntimeError(msg)
        return original(self, address)

    socket.socket.connect = guarded  # type: ignore[method-assign]
    yield
    socket.socket.connect = original  # type: ignore[method-assign]


@pytest.fixture(scope="session")
async def owner_db() -> AsyncIterator[AsyncEngine]:
    """Owner-role engine for test-only DDL. Never use it to assert isolation."""
    engine = owner_engine(poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
async def rls_table(owner_db: AsyncEngine) -> AsyncIterator[str]:
    """Create a tenant-owned table exactly like a module migration would (enable_rls + default grants)."""
    async with owner_db.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {RLS_TEST_TABLE}"))
        await conn.execute(
            text(
                f"CREATE TABLE {RLS_TEST_TABLE} ("
                " id uuid PRIMARY KEY DEFAULT gen_random_uuid(),"
                " tenant_id uuid NOT NULL,"
                " name text NOT NULL)"
            )
        )
        for stmt in enable_rls_sql(RLS_TEST_TABLE):
            await conn.execute(text(stmt))
    yield RLS_TEST_TABLE
    async with owner_db.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {RLS_TEST_TABLE}"))


@pytest.fixture(scope="session", autouse=True)
async def _dispose_app_engine() -> AsyncIterator[None]:
    yield
    await dispose_engines()


@pytest.fixture(scope="session")
def app() -> FastAPI:
    from app.main import create_app

    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
