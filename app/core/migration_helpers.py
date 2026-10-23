"""SQL helpers used from Alembic migrations (run as the owner role).

``enable_rls(table)`` MUST be called in the migration of every tenant-owned table (AD-2).
The policy uses ``NULLIF(current_setting('app.tenant_id', true), '')`` because a pooled connection
that previously ran ``SET LOCAL app.tenant_id`` reports ``''`` (not NULL) afterwards; without NULLIF
the ``::uuid`` cast would raise instead of returning zero rows.
"""

import dataclasses
import json
import re
from collections.abc import Sequence

from alembic import op

TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"
POLICY_NAME = "tenant_isolation"

_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _ident(name: str) -> str:
    if not _IDENT.match(name):
        msg = f"unsafe SQL identifier: {name!r}"
        raise ValueError(msg)
    return name


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# ---------------------------------------------------------------- RLS
def enable_rls_sql(table: str) -> list[str]:
    t = _ident(table)
    return [
        f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {POLICY_NAME} ON {t} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})",
    ]


def disable_rls_sql(table: str) -> list[str]:
    t = _ident(table)
    return [
        f"DROP POLICY IF EXISTS {POLICY_NAME} ON {t}",
        f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY",
        f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY",
    ]


MIXED_PREDICATE = (
    "(tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid) OR "
    "(tenant_id IS NULL AND NULLIF(current_setting('app.tenant_id', true), '') IS NULL)"
)


def enable_mixed_rls_sql(table: str) -> list[str]:
    """RLS for a table holding tenant rows and platform rows (``tenant_id`` NULL): a tenant session sees
    and writes only its rows; a session without tenant context only platform rows."""
    t = _ident(table)
    return [
        f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY",
        f"CREATE POLICY {POLICY_NAME} ON {t} USING ({MIXED_PREDICATE}) WITH CHECK ({MIXED_PREDICATE})",
    ]


def enable_mixed_rls(table: str) -> None:
    for stmt in enable_mixed_rls_sql(table):
        op.execute(stmt)


def enable_rls(table: str) -> None:
    for stmt in enable_rls_sql(table):
        op.execute(stmt)


def disable_rls(table: str) -> None:
    for stmt in disable_rls_sql(table):
        op.execute(stmt)


# ---------------------------------------------------------------- runtime role
def create_app_role_sql(role: str, password: str) -> str:
    """Idempotent: create (or re-assert) the runtime LOGIN role without superuser / RLS bypass."""
    r = _ident(role)
    if not password:
        msg = "the runtime role password must not be empty (set DB_APP_PASSWORD)"
        raise ValueError(msg)
    if "$tos_role$" in password:
        msg = "password contains a reserved token"
        raise ValueError(msg)
    attrs = (
        f"LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {_literal(password)}"
    )
    return f"""
    DO $tos_role$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_literal(r)}) THEN
            CREATE ROLE {r} {attrs};
        ELSE
            ALTER ROLE {r} {attrs};
        END IF;
    END
    $tos_role$;
    """


def grant_app_privileges_sql(role: str) -> list[str]:
    """DML on existing + future tables in the migration's CURRENT schema (future = created by the migrating
    owner role). ``current_schema()`` is ``public`` for the legacy dedicated-DB model and the app schema
    (e.g. ``talentos``) for the schema-per-app / company-DB model -- because the migration connection's
    ``search_path`` was pinned to it (``alembic/env.py``). So the same baseline migration grants on whichever
    schema it is run into, with no per-schema branching."""
    r = _ident(role)
    rl = _literal(r)
    return [
        f"DO $tos_grant$ DECLARE s text := current_schema(); BEGIN "
        f"EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), {rl}); "
        f"EXECUTE format('GRANT USAGE ON SCHEMA %I TO %I', s, {rl}); "
        f"EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO %I', s, {rl}); "
        f"EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO %I', s, {rl}); "
        f"EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', s, {rl}); "
        f"EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO %I', "
        f"s, {rl}); "
        f"END $tos_grant$;",
        # The runtime role has no business touching migration state (resolved via search_path to the schema's
        # own alembic_version in the schema-per-app model).
        f"REVOKE ALL ON TABLE alembic_version FROM {r}",
    ]


def revoke_app_privileges_sql(role: str) -> list[str]:
    r = _ident(role)
    rl = _literal(r)
    return [
        f"DO $tos_revoke$ DECLARE s text := current_schema(); BEGIN "
        f"EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM %I', s, {rl}); "
        f"EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA %I REVOKE USAGE, SELECT ON SEQUENCES FROM %I', "
        f"s, {rl}); "
        f"EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %I', s, {rl}); "
        f"EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %I', s, {rl}); "
        f"EXECUTE format('REVOKE USAGE ON SCHEMA %I FROM %I', s, {rl}); "
        f"EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM %I', current_database(), {rl}); "
        f"END $tos_revoke$;",
    ]


# ---------------------------------------------------------------- per-app role isolation (company-DB model)
# Target silo model: ONE database ``company_<slug>`` with ONE schema per app. Each app eventually connects
# as ITS OWN restricted role so one app's connection cannot read/write a sibling app's schema in the same
# database (defence in depth on top of the shared owner role that builds every schema). ``schema -> role``.
COMPANY_APP_SCHEMA_ROLES: dict[str, str] = {
    "talentos": "talentos_app",
    "croar": "croar_app",
    "feedbackiq": "feedbackiq_app",
}


def company_role_isolation_sql(
    db_name: str,
    schema_roles: dict[str, str],
    password: str,
    *,
    dml_schemas: Sequence[str] = (),
) -> list[str]:
    """Idempotent DDL that confines each app to its own schema inside a company DB (run as the owner role).

    For every ``schema -> role`` pair it: (re)creates ``role`` as a LOGIN NOSUPERUSER NOBYPASSRLS role (reusing
    :func:`create_app_role_sql`), ensures the schema exists (``talentos`` is already built by the migration
    chain; ``croar`` / ``feedbackiq`` are pre-created EMPTY so each spoke builds its tables inside on first
    access), grants the role CONNECT on ``db_name``, USAGE on ``public`` (shared extensions) and USAGE+CREATE
    on its OWN schema, then REVOKEs ALL on every SIBLING app schema -- so e.g. ``croar_app`` has no privilege
    on the ``talentos`` schema in the same database.

    A schema listed in ``dml_schemas`` additionally gets SELECT/INSERT/UPDATE/DELETE on all current + future
    tables and USAGE/SELECT on sequences (plus matching default privileges). Used for ``talentos``, whose
    tables are owned by the migrating owner role, so the restricted role needs explicit DML to serve the app;
    ``croar`` / ``feedbackiq`` own the tables they create on first access, so they need no table grant. Safe
    to re-run (every statement is CREATE ... IF NOT EXISTS / GRANT / REVOKE / ALTER ROLE)."""
    db = _ident(db_name)
    schemas = list(schema_roles)
    stmts: list[str] = []
    # Create EVERY app schema first so the sibling REVOKEs below never reference a not-yet-created schema
    # (``talentos`` already exists from the migration chain; the others are pre-created EMPTY).
    for schema in schemas:
        stmts.append(f'CREATE SCHEMA IF NOT EXISTS "{_ident(schema)}"')
    for schema, role in schema_roles.items():
        s = _ident(schema)
        r = _ident(role)
        stmts.append(create_app_role_sql(role, password))
        stmts.append(f'GRANT CONNECT ON DATABASE "{db}" TO "{r}"')
        stmts.append(f'GRANT USAGE ON SCHEMA public TO "{r}"')
        stmts.append(f'GRANT USAGE, CREATE ON SCHEMA "{s}" TO "{r}"')
        for other in schemas:
            if other != schema:
                stmts.append(f'REVOKE ALL ON SCHEMA "{_ident(other)}" FROM "{r}"')
        if schema in dml_schemas:
            stmts += [
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{s}" TO "{r}"',
                f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{s}" TO "{r}"',
                f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{s}" '
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO "{r}"',
                f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{s}" GRANT USAGE, SELECT ON SEQUENCES TO "{r}"',
            ]
    return stmts


def _is_production() -> bool:
    from app.core.settings import get_settings

    return get_settings().app_env == "production"


def _role_exists(role: str) -> bool:
    from sqlalchemy import text

    found = op.get_bind().execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first()
    return found is not None


def create_app_role(role: str, password: str) -> None:
    """Create the runtime role (dev / test only).

    Story 22.9 (A10): in production the password never goes through Alembic DDL -- provision the role with
    ``scripts/provision_app_role.py`` (SCRAM) first; the migration refuses to run without it and never
    re-asserts its password."""
    if _is_production():
        if not _role_exists(role):
            msg = (
                f"refusing to create the runtime role {role!r} from a migration in production: "
                "run scripts/provision_app_role.py first"
            )
            raise RuntimeError(msg)
        return
    op.execute(create_app_role_sql(role, password))


def grant_app_privileges(role: str) -> None:
    for stmt in grant_app_privileges_sql(role):
        op.execute(stmt)


def revoke_app_privileges(role: str) -> None:
    for stmt in revoke_app_privileges_sql(role):
        op.execute(stmt)


# ================================================================ Phase 2 guards (Epic 13: AD-21, AD-22, AD-23, AD-26)
# The generic plpgsql trigger functions are created by migration 20261002_200_01 (``create_guard_functions``);
# the helpers below attach them to tables. Every helper is idempotent (``CREATE OR REPLACE`` /
# ``DROP ... IF EXISTS``) so later epics can re-call them with wider allow-lists; each has a ``disable_*``.
#
# Errors: ``TOS01`` ``period_closed:<legal_entity_id>:<yyyy-mm>``; ``TOS02`` ``document_immutable:<table>``
# (translated to 409s by ``core.db_errors``); ``TOS03`` ``org_parent_missing:<table>:<parent>``.
#
# The guard functions that read other tables (periods, posting ledgers, parent documents) run with
# ``SET app.org_kind = 'internal'`` (function-level GUC, restored on exit): an external session must not be
# able to bypass a lock because it cannot *see* the ledger row. Tenant RLS still applies, and every lookup
# is additionally filtered by the row's own ``tenant_id``. ``tos_inherit_org`` keeps the caller's org scope:
# a parent the caller cannot see counts as missing (fail-closed).

ORG_POLICY_NAME = "org_isolation"
_ORG_KIND = "current_setting('app.org_kind', true)"
_ORG_ID = "NULLIF(current_setting('app.org_id', true), '')::uuid"


@dataclasses.dataclass(frozen=True)
class ParentRef:
    table: str
    fk_column: str
    status_column: str
    mutable_states: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class GrantsRef:
    subject_kind: str
    id_column: str = "id"


@dataclasses.dataclass(frozen=True)
class InheritRef:
    parent_table: str
    fk_column: str
    columns: tuple[str, ...]


def _json_arg(value: object) -> str:
    return _literal(json.dumps(value, sort_keys=True))


# ---------------------------------------------------------------- generic trigger functions (200_01)
GUARD_FUNCTIONS_SQL: tuple[str, ...] = (
    # tos_immutable: BEFORE UPDATE OR DELETE (+ INSERT on child tables). One JSON argument:
    # {table, status_column, mutable_states, allowed_columns, allowed_transitions, transition_columns, parent}.
    r"""
CREATE OR REPLACE FUNCTION tos_immutable() RETURNS trigger LANGUAGE plpgsql
SET app.org_kind = 'internal' AS $fn$
DECLARE
    cfg jsonb := TG_ARGV[0]::jsonb;
    tbl text := cfg->>'table';
    st text := NULLIF(cfg->>'status_column', '');
    mutable text[] := ARRAY(SELECT jsonb_array_elements_text(COALESCE(cfg->'mutable_states', '[]'::jsonb)));
    allowed text[] := ARRAY(SELECT jsonb_array_elements_text(COALESCE(cfg->'allowed_columns', '[]'::jsonb)))
                      || ARRAY['updated_at'];
    tcols text[] := ARRAY(SELECT jsonb_array_elements_text(COALESCE(cfg->'transition_columns', '[]'::jsonb)));
    parent jsonb := cfg->'parent';
    pmutable text[];
    old_j jsonb;
    new_j jsonb;
    old_status text;
    new_status text;
    p_status text;
    p_id uuid;
    pair jsonb;
BEGIN
    IF parent IS NOT NULL AND jsonb_typeof(parent) = 'object' THEN
        -- Child row: mutability follows the parent's status (parent read FOR SHARE). A missing parent
        -- (cascade delete of the parent itself) passes; the FK guards inserts.
        pmutable := ARRAY(SELECT jsonb_array_elements_text(parent->'mutable_states'));
        IF TG_OP IN ('UPDATE', 'DELETE') THEN
            p_id := (to_jsonb(OLD)->>(parent->>'fk_column'))::uuid;
            EXECUTE format('SELECT %I::text FROM %I WHERE id = $1 AND tenant_id = $2 FOR SHARE',
                           parent->>'status_column', parent->>'table')
                INTO p_status USING p_id, (to_jsonb(OLD)->>'tenant_id')::uuid;
            IF p_status IS NOT NULL AND NOT (p_status = ANY(pmutable)) THEN
                IF TG_OP = 'DELETE' OR (to_jsonb(NEW) - allowed) <> (to_jsonb(OLD) - allowed) THEN
                    RAISE EXCEPTION USING ERRCODE = 'TOS02', MESSAGE = 'document_immutable:' || tbl;
                END IF;
            END IF;
        END IF;
        IF TG_OP IN ('INSERT', 'UPDATE') THEN
            p_id := (to_jsonb(NEW)->>(parent->>'fk_column'))::uuid;
            IF TG_OP = 'INSERT' OR p_id IS DISTINCT FROM (to_jsonb(OLD)->>(parent->>'fk_column'))::uuid THEN
                EXECUTE format('SELECT %I::text FROM %I WHERE id = $1 AND tenant_id = $2 FOR SHARE',
                               parent->>'status_column', parent->>'table')
                    INTO p_status USING p_id, (to_jsonb(NEW)->>'tenant_id')::uuid;
                IF p_status IS NOT NULL AND NOT (p_status = ANY(pmutable)) THEN
                    RAISE EXCEPTION USING ERRCODE = 'TOS02', MESSAGE = 'document_immutable:' || tbl;
                END IF;
            END IF;
        END IF;
        IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'INSERT' THEN
        RETURN NEW;
    END IF;
    IF st IS NOT NULL THEN
        old_status := to_jsonb(OLD)->>st;
        IF old_status = ANY(mutable) THEN
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END IF;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION USING ERRCODE = 'TOS02', MESSAGE = 'document_immutable:' || tbl;
    END IF;
    old_j := to_jsonb(OLD) - allowed;
    new_j := to_jsonb(NEW) - allowed;
    IF old_j = new_j THEN
        RETURN NEW;
    END IF;
    IF st IS NOT NULL THEN
        new_status := to_jsonb(NEW)->>st;
        FOR pair IN SELECT * FROM jsonb_array_elements(COALESCE(cfg->'allowed_transitions', '[]'::jsonb)) LOOP
            IF pair->>0 = old_status AND pair->>1 = new_status
               AND ((old_j - st) - tcols) = ((new_j - st) - tcols) THEN
                RETURN NEW;
            END IF;
        END LOOP;
    END IF;
    RAISE EXCEPTION USING ERRCODE = 'TOS02', MESSAGE = 'document_immutable:' || tbl;
END
$fn$
""",
    # tos_period_guard: BEFORE INSERT OR UPDATE OR DELETE. One JSON argument:
    # {table, date_column, legal_entity_column, allowed_columns}.
    r"""
CREATE OR REPLACE FUNCTION tos_period_guard() RETURNS trigger LANGUAGE plpgsql
SET app.org_kind = 'internal' AS $fn$
DECLARE
    cfg jsonb := TG_ARGV[0]::jsonb;
    dcol text := cfg->>'date_column';
    lcol text := cfg->>'legal_entity_column';
    allowed text[] := ARRAY(SELECT jsonb_array_elements_text(COALESCE(cfg->'allowed_columns', '[]'::jsonb)))
                      || ARRAY['updated_at'];
    rec jsonb;
    d date;
    le uuid;
    st text;
    checks jsonb[];
BEGIN
    IF TG_OP = 'INSERT' THEN
        checks := ARRAY[to_jsonb(NEW)];
    ELSIF TG_OP = 'DELETE' THEN
        checks := ARRAY[to_jsonb(OLD)];
    ELSE
        IF (to_jsonb(NEW) - allowed) = (to_jsonb(OLD) - allowed) THEN
            RETURN NEW;
        END IF;
        checks := ARRAY[to_jsonb(OLD), to_jsonb(NEW)];
    END IF;
    FOREACH rec IN ARRAY checks LOOP
        d := (rec->>dcol)::date;
        le := (rec->>lcol)::uuid;
        CONTINUE WHEN d IS NULL OR le IS NULL;
        st := NULL;
        SELECT p.status INTO st FROM accounting_periods p
         WHERE p.tenant_id = (rec->>'tenant_id')::uuid AND p.legal_entity_id = le
           AND p.year = extract(year FROM d)::int AND p.month = extract(month FROM d)::int
           FOR SHARE;
        IF st IS NULL OR st <> 'open' THEN
            RAISE EXCEPTION USING ERRCODE = 'TOS01',
                MESSAGE = 'period_closed:' || le::text || ':' || to_char(d, 'YYYY-MM');
        END IF;
    END LOOP;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$fn$
""",
    # tos_source_lock: BEFORE UPDATE OR DELETE. One JSON argument: {table, source_kind, id_column,
    # allowed_columns}. Reads the posting ledgers (AD-23): a source row with a non-released posting is locked.
    r"""
CREATE OR REPLACE FUNCTION tos_source_lock() RETURNS trigger LANGUAGE plpgsql
SET app.org_kind = 'internal' AS $fn$
DECLARE
    cfg jsonb := TG_ARGV[0]::jsonb;
    allowed text[] := ARRAY(SELECT jsonb_array_elements_text(COALESCE(cfg->'allowed_columns', '[]'::jsonb)))
                      || ARRAY['updated_at'];
    sid uuid := (to_jsonb(OLD)->>(cfg->>'id_column'))::uuid;
    tid uuid := (to_jsonb(OLD)->>'tenant_id')::uuid;
    locked boolean;
BEGIN
    IF TG_OP = 'UPDATE' AND (to_jsonb(NEW) - allowed) = (to_jsonb(OLD) - allowed) THEN
        RETURN NEW;
    END IF;
    SELECT EXISTS (SELECT 1 FROM billing_postings b WHERE b.tenant_id = tid
                     AND b.source_kind = cfg->>'source_kind' AND b.source_id = sid AND b.state <> 'released')
        OR EXISTS (SELECT 1 FROM payable_postings p WHERE p.tenant_id = tid
                     AND p.source_kind = cfg->>'source_kind' AND p.source_id = sid AND p.state <> 'released')
      INTO locked;
    IF locked THEN
        RAISE EXCEPTION USING ERRCODE = 'TOS02', MESSAGE = 'document_immutable:' || (cfg->>'table');
    END IF;
    IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
    RETURN NEW;
END
$fn$
""",
    # tos_inherit_org: BEFORE INSERT OR UPDATE OF <fk>. One JSON argument: {parent_table, fk_column, columns}.
    r"""
CREATE OR REPLACE FUNCTION tos_inherit_org() RETURNS trigger LANGUAGE plpgsql AS $fn$
DECLARE
    cfg jsonb := TG_ARGV[0]::jsonb;
    parent_row jsonb;
    patch jsonb := '{}'::jsonb;
    col text;
BEGIN
    EXECUTE format('SELECT to_jsonb(p) FROM %I p WHERE p.id = $1', cfg->>'parent_table')
        INTO parent_row USING (to_jsonb(NEW)->>(cfg->>'fk_column'))::uuid;
    IF parent_row IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = 'TOS03',
            MESSAGE = 'org_parent_missing:' || TG_TABLE_NAME || ':' || (cfg->>'parent_table');
    END IF;
    FOR col IN SELECT jsonb_array_elements_text(cfg->'columns') LOOP
        patch := patch || jsonb_build_object(col, parent_row->col);
    END LOOP;
    NEW := jsonb_populate_record(NEW, patch);
    RETURN NEW;
END
$fn$
""",
)

GUARD_FUNCTION_NAMES: tuple[str, ...] = (
    "tos_immutable",
    "tos_period_guard",
    "tos_source_lock",
    "tos_inherit_org",
)


def create_guard_functions() -> None:
    for stmt in GUARD_FUNCTIONS_SQL:
        op.execute(stmt)


def drop_guard_functions() -> None:
    for name in GUARD_FUNCTION_NAMES:
        op.execute(f"DROP FUNCTION IF EXISTS {name}() CASCADE")


# ---------------------------------------------------------------- immutability (AD-21)
def enable_immutability_sql(
    table: str,
    *,
    status_column: str | None,
    mutable_states: Sequence[str] = ("draft",),
    allowed_columns: Sequence[str] = (),
    allowed_transitions: Sequence[tuple[str, str]] = (),
    parent: ParentRef | None = None,
    transition_columns: Sequence[str] = (),
) -> list[str]:
    """``transition_columns``: columns that may change together with an ``allowed_transitions`` status change
    (e.g. ``cancel_basis`` / ``cancelled_at`` with ``issued -> cancelled``)."""
    t = _ident(table)
    cfg: dict[str, object] = {
        "table": t,
        "status_column": _ident(status_column) if status_column else "",
        "mutable_states": list(mutable_states),
        "allowed_columns": [_ident(c) for c in allowed_columns],
        "allowed_transitions": [[a, b] for a, b in allowed_transitions],
        "transition_columns": [_ident(c) for c in transition_columns],
        "parent": (
            {
                "table": _ident(parent.table),
                "fk_column": _ident(parent.fk_column),
                "status_column": _ident(parent.status_column),
                "mutable_states": list(parent.mutable_states),
            }
            if parent is not None
            else None
        ),
    }
    when = "INSERT OR UPDATE OR DELETE" if parent is not None else "UPDATE OR DELETE"
    return [
        f"DROP TRIGGER IF EXISTS tos_immutable ON {t}",
        f"CREATE TRIGGER tos_immutable BEFORE {when} ON {t} FOR EACH ROW "
        f"EXECUTE FUNCTION tos_immutable({_json_arg(cfg)})",
    ]


def enable_immutability(
    table: str,
    *,
    status_column: str | None,
    mutable_states: Sequence[str] = ("draft",),
    allowed_columns: Sequence[str] = (),
    allowed_transitions: Sequence[tuple[str, str]] = (),
    parent: ParentRef | None = None,
    transition_columns: Sequence[str] = (),
) -> None:
    for stmt in enable_immutability_sql(
        table,
        status_column=status_column,
        mutable_states=mutable_states,
        allowed_columns=allowed_columns,
        allowed_transitions=allowed_transitions,
        parent=parent,
        transition_columns=transition_columns,
    ):
        op.execute(stmt)


def disable_immutability_sql(table: str) -> list[str]:
    return [f"DROP TRIGGER IF EXISTS tos_immutable ON {_ident(table)}"]


def disable_immutability(table: str) -> None:
    for stmt in disable_immutability_sql(table):
        op.execute(stmt)


# ---------------------------------------------------------------- period guard (AD-22)
def enable_period_guard_sql(
    table: str,
    *,
    date_column: str,
    legal_entity_column: str = "legal_entity_id",
    allowed_columns: Sequence[str] = (),
) -> list[str]:
    t = _ident(table)
    cfg = {
        "table": t,
        "date_column": _ident(date_column),
        "legal_entity_column": _ident(legal_entity_column),
        "allowed_columns": [_ident(c) for c in allowed_columns],
    }
    return [
        f"DROP TRIGGER IF EXISTS tos_period_guard ON {t}",
        f"CREATE TRIGGER tos_period_guard BEFORE INSERT OR UPDATE OR DELETE ON {t} FOR EACH ROW "
        f"EXECUTE FUNCTION tos_period_guard({_json_arg(cfg)})",
    ]


def enable_period_guard(
    table: str,
    *,
    date_column: str,
    legal_entity_column: str = "legal_entity_id",
    allowed_columns: Sequence[str] = (),
) -> None:
    for stmt in enable_period_guard_sql(
        table,
        date_column=date_column,
        legal_entity_column=legal_entity_column,
        allowed_columns=allowed_columns,
    ):
        op.execute(stmt)


def disable_period_guard_sql(table: str) -> list[str]:
    return [f"DROP TRIGGER IF EXISTS tos_period_guard ON {_ident(table)}"]


def disable_period_guard(table: str) -> None:
    for stmt in disable_period_guard_sql(table):
        op.execute(stmt)


# ---------------------------------------------------------------- source lock (AD-21 + AD-23)
def enable_source_lock_sql(
    table: str, *, source_kind: str, id_column: str = "id", allowed_columns: Sequence[str] = ()
) -> list[str]:
    t = _ident(table)
    cfg = {
        "table": t,
        "source_kind": source_kind,
        "id_column": _ident(id_column),
        "allowed_columns": [_ident(c) for c in allowed_columns],
    }
    return [
        f"DROP TRIGGER IF EXISTS tos_source_lock ON {t}",
        f"CREATE TRIGGER tos_source_lock BEFORE UPDATE OR DELETE ON {t} FOR EACH ROW "
        f"EXECUTE FUNCTION tos_source_lock({_json_arg(cfg)})",
    ]


def enable_source_lock(
    table: str, *, source_kind: str, id_column: str = "id", allowed_columns: Sequence[str] = ()
) -> None:
    for stmt in enable_source_lock_sql(
        table, source_kind=source_kind, id_column=id_column, allowed_columns=allowed_columns
    ):
        op.execute(stmt)


def disable_source_lock_sql(table: str) -> list[str]:
    return [f"DROP TRIGGER IF EXISTS tos_source_lock ON {_ident(table)}"]


def disable_source_lock(table: str) -> None:
    for stmt in disable_source_lock_sql(table):
        op.execute(stmt)


# ---------------------------------------------------------------- organization isolation (AD-26)
def org_predicate(
    table: str,
    *,
    client_col: str | None = None,
    vendor_col: str | None = None,
    worker_col: str | None = None,
    grants: GrantsRef | None = None,
    self_org: bool = False,
) -> str:
    """The restrictive policy expression. Branches whose column is not given are omitted (no columns =
    internal-only). An unset GUC makes every branch NULL/false: no rows (fail-closed)."""
    t = _ident(table)
    branches = [f"{_ORG_KIND} = 'internal'"]
    for kind, col in (("client", client_col), ("vendor", vendor_col), ("worker", worker_col)):
        if col:
            branches.append(f"({_ORG_KIND} = '{kind}' AND {t}.{_ident(col)} = {_ORG_ID})")
    if grants is not None:
        branches.append(
            "(EXISTS (SELECT 1 FROM org_grants g WHERE g.subject_kind = "
            f"{_literal(grants.subject_kind)} AND g.subject_id = {t}.{_ident(grants.id_column)} "
            f"AND g.org_kind = {_ORG_KIND} AND g.org_id = {_ORG_ID} "
            "AND (g.expires_at IS NULL OR g.expires_at > now())))"
        )
    if self_org:
        branches.append(f"({t}.org_kind = {_ORG_KIND} AND {t}.org_id = {_ORG_ID})")
    return "(" + " OR ".join(branches) + ")"


def enable_org_rls_sql(
    table: str,
    *,
    client_col: str | None = None,
    vendor_col: str | None = None,
    worker_col: str | None = None,
    grants: GrantsRef | None = None,
    inherit_from: InheritRef | None = None,
    self_org: bool = False,
) -> list[str]:
    t = _ident(table)
    expr = org_predicate(
        t,
        client_col=client_col,
        vendor_col=vendor_col,
        worker_col=worker_col,
        grants=grants,
        self_org=self_org,
    )
    stmts = [
        f"DROP POLICY IF EXISTS {ORG_POLICY_NAME} ON {t}",
        f"CREATE POLICY {ORG_POLICY_NAME} ON {t} AS RESTRICTIVE FOR ALL USING {expr} WITH CHECK {expr}",
        f"DROP TRIGGER IF EXISTS tos_inherit_org ON {t}",
    ]
    if inherit_from is not None:
        cfg = {
            "parent_table": _ident(inherit_from.parent_table),
            "fk_column": _ident(inherit_from.fk_column),
            "columns": [_ident(c) for c in inherit_from.columns],
        }
        stmts.append(
            f"CREATE TRIGGER tos_inherit_org BEFORE INSERT OR UPDATE OF {_ident(inherit_from.fk_column)} "
            f"ON {t} FOR EACH ROW EXECUTE FUNCTION tos_inherit_org({_json_arg(cfg)})"
        )
    return stmts


def enable_org_rls(
    table: str,
    *,
    client_col: str | None = None,
    vendor_col: str | None = None,
    worker_col: str | None = None,
    grants: GrantsRef | None = None,
    inherit_from: InheritRef | None = None,
    self_org: bool = False,
) -> None:
    for stmt in enable_org_rls_sql(
        table,
        client_col=client_col,
        vendor_col=vendor_col,
        worker_col=worker_col,
        grants=grants,
        inherit_from=inherit_from,
        self_org=self_org,
    ):
        op.execute(stmt)


def disable_org_rls_sql(table: str) -> list[str]:
    t = _ident(table)
    return [
        f"DROP TRIGGER IF EXISTS tos_inherit_org ON {t}",
        f"DROP POLICY IF EXISTS {ORG_POLICY_NAME} ON {t}",
    ]


def disable_org_rls(table: str) -> None:
    for stmt in disable_org_rls_sql(table):
        op.execute(stmt)


# ---------------------------------------------------------------- monthly partitions (Epic 66, NFR-74)
def _month_start(year: int, month: int) -> tuple[int, int]:
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    return year, month


def monthly_partition_name(table: str, year: int, month: int) -> str:
    return f"{_ident(table)}_y{year:04d}m{month:02d}"


def monthly_partition_sql(table: str, year: int, month: int, *, org_rls: bool = True) -> list[str]:
    """``CREATE TABLE IF NOT EXISTS <table>_yYYYYmMM PARTITION OF <table>`` for one calendar month (UTC) plus
    tenant RLS (and internal-only org RLS) on the partition itself: a partition queried directly does not use the
    parent's policies, so every partition carries the same policies as its RANGE-partitioned parent."""
    t = _ident(table)
    y1, m1 = _month_start(year, month)
    y2, m2 = _month_start(year, month + 1)
    name = monthly_partition_name(t, y1, m1)
    stmts = [
        f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF {t} "
        f"FOR VALUES FROM ('{y1:04d}-{m1:02d}-01 00:00:00+00') TO ('{y2:04d}-{m2:02d}-01 00:00:00+00')",
        f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {POLICY_NAME} ON {name}",
        f"CREATE POLICY {POLICY_NAME} ON {name} USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})",
    ]
    if org_rls:
        stmts += enable_org_rls_sql(name)
    return stmts


def months_from(year: int, month: int, count: int) -> list[tuple[int, int]]:
    return [_month_start(year, month + i) for i in range(count)]


def create_monthly_partitions(
    table: str, months_ahead: int = 3, *, start: tuple[int, int] | None = None, org_rls: bool = True
) -> None:
    """Migration helper: the current month (or ``start``) and ``months_ahead`` further months. The maintenance task
    ``partitions.ensure_monthly`` keeps creating them (``monthly_partition_sql`` run by the owner role)."""
    import datetime as _dt

    if start is None:
        today = _dt.datetime.now(_dt.UTC)
        start = (today.year, today.month)
    for year, month in months_from(start[0], start[1], months_ahead + 1):
        for stmt in monthly_partition_sql(table, year, month, org_rls=org_rls):
            op.execute(stmt)
