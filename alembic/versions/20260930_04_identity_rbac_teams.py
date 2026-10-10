"""Identity RBAC: roles, user_roles, business_units, teams, team_members (tenant-owned, RLS); invitation
roles/teams/business unit + revocation; ``users.role_keys`` -> ``user_roles``; default roles backfilled.

Revision ID: 20260930_04
Revises: 20260930_03
Create Date: 2026-09-30

"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_helpers import enable_rls
from app.core.permissions import DEFAULT_ROLES

revision: str = "20260930_04"
down_revision: str | None = "20260930_03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def _tenant_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id"], ["tenants.id"], name=op.f(f"fk_{table}_tenant_id_tenants"), ondelete="RESTRICT"
    )


def _set_tenant(bind: sa.Connection, tenant_id: object) -> None:
    # Data statements run under FORCE RLS unless the owner is a superuser: scope them explicitly.
    bind.execute(sa.text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": str(tenant_id)})


def upgrade() -> None:
    # ---- business units / teams / roles
    op.create_table(
        "business_units",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", postgresql.CITEXT(), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        *_timestamps(),
        _tenant_fk("business_units"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_business_units")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_business_units_tenant_id_name")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_business_units_tenant_id_id")),
    )
    op.create_index(op.f("ix_business_units_tenant_id"), "business_units", ["tenant_id"])
    enable_rls("business_units")

    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", postgresql.CITEXT(), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("business_unit_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        _tenant_fk("teams"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "business_unit_id"],
            ["business_units.tenant_id", "business_units.id"],
            name="fk_teams_business_unit_id_business_units",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_teams")),
        sa.UniqueConstraint("tenant_id", "name", name=op.f("uq_teams_tenant_id_name")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_teams_tenant_id_id")),
    )
    op.create_index(op.f("ix_teams_tenant_id"), "teams", ["tenant_id"])
    op.create_index("ix_teams_business_unit_id", "teams", ["business_unit_id"])
    enable_rls("teams")

    op.create_table(
        "roles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("is_system", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "permissions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *_timestamps(),
        _tenant_fk("roles"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("tenant_id", "key", name=op.f("uq_roles_tenant_id_key")),
        sa.UniqueConstraint("tenant_id", "id", name=op.f("uq_roles_tenant_id_id")),
    )
    op.create_index(op.f("ix_roles_tenant_id"), "roles", ["tenant_id"])
    enable_rls("roles")

    # ---- users: business unit + composite key for tenant-safe references
    op.add_column("users", sa.Column("business_unit_id", sa.Uuid(), nullable=True))
    op.create_unique_constraint(op.f("uq_users_tenant_id_id"), "users", ["tenant_id", "id"])
    op.create_foreign_key(
        "fk_users_business_unit_id_business_units",
        "users",
        "business_units",
        ["tenant_id", "business_unit_id"],
        ["tenant_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_users_business_unit_id", "users", ["business_unit_id"])

    op.create_table(
        "user_roles",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _tenant_fk("user_roles"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_user_roles_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "role_id"],
            ["roles.tenant_id", "roles.id"],
            name="fk_user_roles_role_id_roles",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id", name=op.f("pk_user_roles")),
    )
    op.create_index(op.f("ix_user_roles_tenant_id"), "user_roles", ["tenant_id"])
    op.create_index("ix_user_roles_role_id", "user_roles", ["role_id"])
    enable_rls("user_roles")

    op.create_table(
        "team_members",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("is_lead", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        _tenant_fk("team_members"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "team_id"],
            ["teams.tenant_id", "teams.id"],
            name="fk_team_members_team_id_teams",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.id"],
            name="fk_team_members_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("team_id", "user_id", name=op.f("pk_team_members")),
    )
    op.create_index(op.f("ix_team_members_tenant_id"), "team_members", ["tenant_id"])
    op.create_index("ix_team_members_user_id", "team_members", ["user_id"])
    enable_rls("team_members")

    # ---- invitations: roles / business unit / teams to assign on acceptance, revocation
    uuid_array = postgresql.ARRAY(sa.Uuid())
    op.add_column("invitations", sa.Column("name", sa.String(length=200), nullable=True))
    op.add_column(
        "invitations",
        sa.Column("role_ids", uuid_array, server_default=sa.text("'{}'::uuid[]"), nullable=False),
    )
    op.add_column("invitations", sa.Column("business_unit_id", sa.Uuid(), nullable=True))
    op.add_column(
        "invitations",
        sa.Column("team_ids", uuid_array, server_default=sa.text("'{}'::uuid[]"), nullable=False),
    )
    op.add_column("invitations", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))

    # ---- data: default roles for every existing tenant; role_keys / role_key -> role ids
    # DEFAULT_ROLES is imported from app code: this backfill seeds the role set current at upgrade time.
    bind = op.get_bind()
    tenant_ids = [row[0] for row in bind.execute(sa.text("SELECT id FROM tenants"))]
    insert_role = sa.text(
        "INSERT INTO roles (id, tenant_id, key, name, description, is_system, permissions) "
        "VALUES (gen_random_uuid(), :tid, :key, :name, :description, true, CAST(:perms AS jsonb)) "
        "ON CONFLICT (tenant_id, key) DO NOTHING"
    )
    for tenant_id in tenant_ids:
        _set_tenant(bind, tenant_id)
        for template in DEFAULT_ROLES:
            bind.execute(
                insert_role,
                {
                    "tid": tenant_id,
                    "key": template.key,
                    "name": template.name,
                    "description": template.description,
                    "perms": json.dumps({k: v.value for k, v in template.permissions.items()}),
                },
            )
        bind.execute(
            sa.text(
                "INSERT INTO user_roles (tenant_id, user_id, role_id) "
                "SELECT u.tenant_id, u.id, r.id FROM users u "
                "JOIN roles r ON r.tenant_id = u.tenant_id AND r.key = ANY(u.role_keys) "
                "WHERE u.tenant_id = :tid ON CONFLICT DO NOTHING"
            ),
            {"tid": tenant_id},
        )
        bind.execute(
            sa.text(
                "UPDATE invitations i SET role_ids = ARRAY("
                "  SELECT r.id FROM roles r WHERE r.tenant_id = i.tenant_id AND r.key = i.role_key"
                ") WHERE i.tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))

    op.drop_column("users", "role_keys")
    op.drop_column("invitations", "role_key")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "role_keys",
            postgresql.ARRAY(sa.String(length=64)),
            server_default=sa.text("'{}'::varchar[]"),
            nullable=False,
        ),
    )
    op.add_column(
        "invitations",
        sa.Column("role_key", sa.String(length=64), server_default=sa.text("'viewer'"), nullable=False),
    )
    bind = op.get_bind()
    for (tenant_id,) in bind.execute(sa.text("SELECT id FROM tenants")).all():
        _set_tenant(bind, tenant_id)
        bind.execute(
            sa.text(
                "UPDATE users u SET role_keys = ARRAY("
                "  SELECT r.key FROM user_roles ur JOIN roles r ON r.id = ur.role_id"
                "  WHERE ur.user_id = u.id ORDER BY r.key"
                ") WHERE u.tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
        bind.execute(
            sa.text(
                "UPDATE invitations i SET role_key = COALESCE(("
                "  SELECT r.key FROM roles r WHERE r.id = ANY(i.role_ids) ORDER BY r.key LIMIT 1"
                "), 'viewer') WHERE i.tenant_id = :tid"
            ),
            {"tid": tenant_id},
        )
    bind.execute(sa.text("SELECT set_config('app.tenant_id', '', true)"))
    op.alter_column("invitations", "role_key", server_default=None)

    op.drop_column("invitations", "revoked_at")
    op.drop_column("invitations", "team_ids")
    op.drop_column("invitations", "business_unit_id")
    op.drop_column("invitations", "role_ids")
    op.drop_column("invitations", "name")
    op.drop_table("team_members")
    op.drop_table("user_roles")
    op.drop_index("ix_users_business_unit_id", table_name="users")
    op.drop_constraint("fk_users_business_unit_id_business_units", "users", type_="foreignkey")
    op.drop_constraint(op.f("uq_users_tenant_id_id"), "users", type_="unique")
    op.drop_column("users", "business_unit_id")
    op.drop_table("roles")
    op.drop_table("teams")
    op.drop_table("business_units")
