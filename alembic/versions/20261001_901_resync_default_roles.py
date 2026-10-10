"""Re-sync the stored permission JSON of every tenant's SYSTEM non-admin default roles with the Phase 1
``DEFAULT_ROLES`` definitions (Epics 2/6/7/9/10 added permissions after early tenants were seeded).

The role definitions are FROZEN here (copied from ``app/core/permissions.py`` at Phase 1 close-out) -- the
migration never imports app code, so re-running it later always applies exactly this snapshot.

Touches only rows with ``is_system = true`` whose ``key`` is one of the frozen keys below. Custom roles
(``is_system = false``) and the Tenant Admin role (``tenant_admin``; its grants are computed from the
catalog at runtime) are never modified. Downgrade is a no-op (the previous per-tenant JSON is not kept).

Revision ID: 20261001_901
Revises: 20261001_900
Create Date: 2026-10-01

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261001_901"
down_revision: str | tuple[str, ...] | None = "20261001_900"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_ADMIN_KEY = "tenant_admin"

# role key -> {permission: scope} -- frozen snapshot of DEFAULT_ROLES (Tenant Admin excluded).
FROZEN_DEFAULT_ROLES: dict[str, dict[str, str]] = {
    "recruitment_manager": {
        "application:create": "bu",
        "application:delete": "bu",
        "application:move": "bu",
        "application:read": "bu",
        "application:reject": "bu",
        "application:update": "bu",
        "business_unit:read": "tenant",
        "candidate:create": "tenant",
        "candidate:delete": "bu",
        "candidate:merge": "bu",
        "candidate:read": "tenant",
        "candidate:update": "bu",
        "client:read": "tenant",
        "compensation:view": "tenant",
        "connection:manage_own": "own",
        "contact:read": "tenant",
        "demand:approve": "bu",
        "demand:assign": "bu",
        "demand:read": "bu",
        "demand:update": "bu",
        "email:send": "own",
        "email_template:read": "tenant",
        "interview:configure": "tenant",
        "interview:create": "bu",
        "interview:delete": "bu",
        "interview:outcome": "bu",
        "interview:read": "bu",
        "interview:schedule": "bu",
        "interview:update": "bu",
        "match:read": "tenant",
        "match:write": "tenant",
        "note:write": "tenant",
        "offer:approve": "bu",
        "offer:create": "bu",
        "offer:read": "bu",
        "offer:release": "bu",
        "offer:update": "bu",
        "offer:withdraw": "bu",
        "offer:write": "bu",
        "report:export": "bu",
        "report:read": "bu",
        "role:read": "tenant",
        "scorecard:read": "bu",
        "scorecard:submit": "bu",
        "screening:write": "bu",
        "submission:create": "bu",
        "submission:read": "bu",
        "submission:update": "bu",
        "task:create": "bu",
        "task:delete": "bu",
        "task:read": "bu",
        "task:update": "bu",
        "team:read": "tenant",
        "user:read": "tenant",
    },
    "delivery_manager": {
        "application:move": "bu",
        "application:read": "bu",
        "application:reject": "bu",
        "application:update": "bu",
        "business_unit:read": "tenant",
        "candidate:read": "tenant",
        "client:read": "bu",
        "compensation:view": "tenant",
        "connection:manage_own": "own",
        "contact:create": "bu",
        "contact:read": "bu",
        "contact:update": "bu",
        "demand:approve": "bu",
        "demand:assign": "bu",
        "demand:create": "bu",
        "demand:read": "bu",
        "demand:update": "bu",
        "email:send": "own",
        "email_template:read": "tenant",
        "interview:create": "bu",
        "interview:outcome": "bu",
        "interview:read": "bu",
        "interview:schedule": "bu",
        "interview:update": "bu",
        "match:read": "tenant",
        "match:write": "tenant",
        "note:write": "tenant",
        "offer:create": "bu",
        "offer:read": "bu",
        "offer:release": "bu",
        "offer:update": "bu",
        "offer:withdraw": "bu",
        "offer:write": "bu",
        "report:export": "bu",
        "report:read": "bu",
        "scorecard:read": "bu",
        "scorecard:submit": "bu",
        "screening:write": "bu",
        "submission:create": "bu",
        "submission:read": "bu",
        "submission:update": "bu",
        "task:create": "bu",
        "task:read": "bu",
        "task:update": "bu",
        "team:read": "tenant",
        "user:read": "tenant",
    },
    "account_manager": {
        "application:read": "team",
        "business_unit:read": "tenant",
        "candidate:read": "tenant",
        "client:create": "tenant",
        "client:delete": "own",
        "client:read": "tenant",
        "client:update": "own",
        "compensation:view": "tenant",
        "connection:manage_own": "own",
        "contact:create": "tenant",
        "contact:delete": "own",
        "contact:read": "tenant",
        "contact:update": "own",
        "demand:create": "own",
        "demand:read": "own",
        "demand:update": "own",
        "email:send": "own",
        "email_template:read": "tenant",
        "interview:read": "team",
        "match:read": "tenant",
        "note:write": "tenant",
        "offer:read": "team",
        "report:read": "own",
        "scorecard:submit": "own",
        "submission:read": "team",
        "submission:update": "team",
        "task:create": "own",
        "task:read": "own",
        "task:update": "own",
        "team:read": "tenant",
    },
    "recruiter": {
        "application:create": "team",
        "application:move": "team",
        "application:read": "team",
        "application:reject": "team",
        "application:update": "team",
        "business_unit:read": "tenant",
        "candidate:create": "tenant",
        "candidate:read": "tenant",
        "candidate:update": "team",
        "client:read": "tenant",
        "connection:manage_own": "own",
        "contact:read": "tenant",
        "demand:read": "team",
        "email:send": "own",
        "email_template:read": "tenant",
        "interview:create": "team",
        "interview:outcome": "team",
        "interview:read": "team",
        "interview:schedule": "team",
        "interview:update": "team",
        "match:read": "tenant",
        "match:write": "tenant",
        "note:write": "tenant",
        "offer:read": "team",
        "report:read": "own",
        "scorecard:read": "team",
        "scorecard:submit": "team",
        "screening:write": "team",
        "submission:create": "team",
        "submission:read": "team",
        "task:create": "own",
        "task:delete": "own",
        "task:read": "own",
        "task:update": "own",
        "team:read": "tenant",
    },
    "interviewer": {
        "candidate:read": "own",
        "connection:manage_own": "own",
        "interview:read": "own",
        "scorecard:read": "own",
        "scorecard:submit": "own",
        "task:read": "own",
        "task:update": "own",
    },
    "viewer": {
        "application:read": "tenant",
        "business_unit:read": "tenant",
        "candidate:read": "tenant",
        "client:read": "tenant",
        "contact:read": "tenant",
        "demand:read": "tenant",
        "interview:read": "tenant",
        "match:read": "tenant",
        "offer:read": "tenant",
        "report:read": "tenant",
        "scorecard:read": "tenant",
        "submission:read": "tenant",
        "task:read": "tenant",
        "team:read": "tenant",
    },
    "grievance_officer": {
        "candidate:read": "tenant",
        "consent:read": "tenant",
        "dsr:process": "tenant",
        "dsr:read": "tenant",
    },
}


def upgrade() -> None:
    assert TENANT_ADMIN_KEY not in FROZEN_DEFAULT_ROLES
    bind = op.get_bind()
    params = {"map": json.dumps(FROZEN_DEFAULT_ROLES, sort_keys=True), "admin": TENANT_ADMIN_KEY}
    update_sql = (
        "UPDATE roles AS r SET permissions = m.value, updated_at = now() "
        "FROM jsonb_each(CAST(:map AS jsonb)) AS m "
        "WHERE r.key = m.key AND r.is_system = true AND r.key <> :admin "
        "AND r.permissions IS DISTINCT FROM m.value"
    )
    bypasses_rls = bind.execute(
        sa.text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).scalar()
    if bypasses_rls:
        bind.execute(sa.text(update_sql), params)
        return
    # FORCE RLS applies to a non-superuser owner: scope the statement to one tenant at a time.
    tenant_ids = [row[0] for row in bind.execute(sa.text("SELECT id FROM tenants"))]
    for tenant_id in tenant_ids:
        bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})
        bind.execute(sa.text(update_sql + " AND r.tenant_id = :tid"), {**params, "tid": tenant_id})


def downgrade() -> None:
    # Data-only re-sync: the previous stored JSON is not recoverable; nothing to undo structurally.
    pass
