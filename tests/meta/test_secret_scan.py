# ruff: noqa: E501
"""Epic 66 (story 66.6, NFR-63): a secret-pattern scan over the source tree and the captured logs of a vault round trip.

* No tracked text file holds an OAuth artefact (``tos_at_ / tos_rt_ / tos_ac_ / tos_cs_`` + a token), a private key
  block or an AWS access key id.
* Values stored through the vault never reach the log output, even when a caller logs them after a read
  (``core.logger`` redacts registered values and OAuth tokens).
"""

import re
import uuid
from pathlib import Path

from loguru import logger

from app.core.context import RequestContext
from app.core.database import tenant_session
from app.services import vault_service
from tests.epic3 import Org3

BACKEND = Path(__file__).resolve().parents[2]
PATTERNS = {
    "oauth_token": re.compile(r"tos_(?:at|rt|ac|cs)_[A-Za-z0-9_-]{20,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
}
SUFFIXES = {".py", ".json", ".yml", ".yaml", ".ini", ".toml", ".sh", ".md", ".txt"}
ALLOWED = {Path("tests/meta/test_secret_scan.py")}


def test_source_tree_has_no_secret_patterns() -> None:
    offenders: list[str] = []
    for root in ("app", "tests", "alembic", "scripts"):
        for path in (BACKEND / root).rglob("*"):
            if not path.is_file() or path.suffix not in SUFFIXES or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(BACKEND)
            if rel in ALLOWED:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            offenders += [f"{rel}: {name}" for name, rx in PATTERNS.items() if rx.search(text)]
    assert offenders == []


async def test_vault_values_never_reach_the_logs(org3: Org3) -> None:
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    tenant, owner = org3.tenant_id, uuid.uuid4()
    value = f"vault-secret-{uuid.uuid4().hex}"
    token = "tos_at_" + "A" * 43
    try:
        async with tenant_session(tenant) as db:
            ref = vault_service.SecretRef("installation", owner, "api_key")
            await vault_service.put(RequestContext(tenant_id=tenant), db, ref, value)
            got = await vault_service.get(RequestContext(tenant_id=tenant), db, ref, purpose="scan")
            logger.info("careless caller logs {} and {}", got, token)
    finally:
        logger.remove(sink)
    joined = "\n".join(lines)
    assert value not in joined
    assert token not in joined
