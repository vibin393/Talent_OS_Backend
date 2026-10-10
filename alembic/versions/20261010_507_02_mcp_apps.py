# ruff: noqa: E501
"""Epic 73 (story 73.2, D-9, spec A3 "Launch clients"): the two platform launch Apps for MCP -- ``claude`` and
``generic-mcp-client`` (public clients, authorization code + refresh, resource ``mcp``). Data migration, idempotent by
``slug``; imports no app code. The Claude callback is the build-time default of ``mcp_launch_redirect_uris_claude``
(update path: the platform operator console, Epic 79). Downgrade deletes the rows only when nothing installed them.

Revision ID: 20261010_507_02_mcp_apps
Revises: 20261010_507_01_mcp_sessions
Create Date: 2026-10-10
"""

import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261010_507_02_mcp_apps"
down_revision: str | tuple[str, ...] | None = "20261010_507_01_mcp_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOOPBACK = [f"http://{host}:{port}/callback" for host in ("127.0.0.1", "localhost") for port in (6274, 8080, 33418)]
SCOPES = {
    "mcp:read": "Read the records you can see when you ask your assistant",
    "mcp:draft": "Create notes, tasks and drafts for you",
    "mcp:propose": "Propose actions that you approve in Talent OS",
    "offline_access": "Stay connected without signing in again",
    "candidates:read": "Find and read candidates you can see",
    "demands:read": "Read demands you can see",
    "applications:read": "Read pipelines you can see",
    "interviews:read": "Read interview schedules",
    "placements:read": "Read placements you can see",
    "reports:read": "Run reports you can see",
    "notes:write": "Add notes when you allow it",
    "tasks:write": "Create tasks when you allow it",
    "demands:write": "Draft demands when you allow it",
    "tasks:read": "Read your tasks",
    "applications:write": "Propose adding candidates to pipelines",
    "knowledge:read": "Search knowledge sources",
    "predictions:read": "Read fill and risk predictions",
    "messages:propose": "Propose messages that you approve",
    "interviews:propose": "Propose interview changes that you approve",
}
FLOW = [{"data": "tool results", "recipient": "the assistant's model provider", "country": "per the client"}]
APPS = (
    ("claude", "Claude", "tosapp_mcp_claude_launch_0000000001", ["https://claude.ai/api/mcp/auth_callback", *LOOPBACK]),
    ("generic-mcp-client", "MCP client (generic)", "tosapp_mcp_generic_launch_000000001", LOOPBACK),
)


def upgrade() -> None:
    conn = op.get_bind()
    for slug, name, client_id, redirects in APPS:
        exists = conn.execute(sa.text("SELECT 1 FROM apps WHERE slug = :s"), {"s": slug}).first()
        if exists is not None:
            continue
        manifest = {"name": name, "description": f"Use {name} with Talent OS tools through MCP.", "data_flows": FLOW}
        conn.execute(
            sa.text(
                "INSERT INTO apps (id, client_id, name, slug, publisher_kind, client_type, grant_types, redirect_uris, "
                "resources, requested_scopes, scope_reasons, manifest, manifest_sha256, status, created_by_kind) VALUES "
                "(gen_random_uuid(), :cid, :name, :slug, 'platform', 'public', "
                "ARRAY['authorization_code', 'refresh_token']::varchar[], CAST(:redirects AS text[]), "
                "ARRAY['mcp']::varchar[], CAST(:scopes AS varchar[]), CAST(:reasons AS jsonb), CAST(:manifest AS jsonb), "
                ":sha, 'published', 'platform')"
            ),
            {
                "cid": client_id, "name": name, "slug": slug, "redirects": redirects, "scopes": sorted(SCOPES),
                "reasons": json.dumps(SCOPES), "manifest": json.dumps(manifest),
                "sha": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
            },
        )  # fmt: skip


def downgrade() -> None:
    conn = op.get_bind()
    for slug, _name, _cid, _r in APPS:
        conn.execute(
            sa.text(
                "DELETE FROM apps a WHERE a.slug = :s AND NOT EXISTS (SELECT 1 FROM app_installations i WHERE i.app_id = a.id)"
            ),
            {"s": slug},
        )
