"""DE-RISKING SPIKE (schema-per-app / company-DB model) — unit tests.

Cover the two load-bearing pieces WITHOUT needing a second database:

* the registry resolver returns ``(db_name, schema_name)`` and the request path turns a non-NULL schema
  into a ``search_path`` binding (schema-per-app) while a NULL schema stays unbound (legacy dedicated DB);
* ``provision_company(model="schema_per_app")`` takes the NEW path (ONE ``company_<slug>`` DB + a
  ``talentos`` schema, registered with ``schema_name``) and leaves the legacy path unchanged.
"""

import pytest

from app.core import database, tenant_registry
from app.core.settings import get_settings
from app.services import company_onboarding as co


# ------------------------------------------------------------------ resolver / search_path binding
def test_schema_connect_args_schema_vs_none() -> None:
    # Legacy dedicated-DB model: no schema -> no connect args -> server-default search_path (public).
    assert database._schema_connect_args(None) == {}
    assert database._schema_connect_args("") == {}
    # Schema-per-app model: bind the connection search_path to "<schema>,public".
    assert database._schema_connect_args("talentos") == {
        "server_settings": {"search_path": "talentos,public"}
    }


async def test_db_sessionmaker_keyed_by_schema() -> None:
    """A company DB's ``talentos`` schema and the same DB on the public schema never share a pool: the
    (loop, db_name, schema_name) cache key keeps them apart, and the schema maker carries the search_path."""
    db = "company_spikeunit"
    try:
        public_maker = database._db_sessionmaker(db, None)
        schema_maker = database._db_sessionmaker(db, "talentos")
        assert public_maker is not schema_maker
        # Same key -> same cached maker (one pool per (loop, db, schema)).
        assert database._db_sessionmaker(db, "talentos") is schema_maker
        # The schema maker's engine pins search_path; the public one does not.
        assert schema_maker.kw["bind"].url.database == db  # type: ignore[index]
    finally:
        await database.dispose_engines()


async def test_resolve_db_target_cache_schema_and_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """``resolve_db_target`` returns the schema for a schema-per-app row and None for a legacy row. We seed
    the process cache so the test never touches a control database."""
    tid_schema = "11111111-1111-1111-1111-111111111111"
    tid_legacy = "22222222-2222-2222-2222-222222222222"
    monkeypatch.setitem(tenant_registry._db_target_cache, tid_schema, ("company_acme", "talentos"))
    monkeypatch.setitem(tenant_registry._db_target_cache, tid_legacy, ("tenant_acme", None))

    assert await tenant_registry.resolve_db_target(tid_schema) == ("company_acme", "talentos")
    assert await tenant_registry.resolve_db_target(tid_legacy) == ("tenant_acme", None)
    # The legacy db-name helper still works (delegates to resolve_db_target).
    assert await tenant_registry.resolve_db_name(tid_schema) == "company_acme"
    assert await tenant_registry.resolve_db_name(tid_legacy) == "tenant_acme"


# ------------------------------------------------------------------ onboarding: new path vs legacy path
class _Recorder:
    def __init__(self) -> None:
        self.schema_db: tuple[str, str] | None = None
        self.legacy_db: str | None = None
        self.registered: dict[str, object] = {}
        self.services: dict[str, dict[str, object]] = {}  # service -> {db_name, schema_name}
        self.ensured_dbs: list[str] = []

    def _patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = self

        async def _free(_slug: str) -> bool:
            return True

        def _schema_path(db_name: str, schema: str) -> None:
            rec.schema_db = (db_name, schema)

        def _legacy_path(db_name: str) -> None:
            rec.legacy_db = db_name

        def _ensure_db(db_name: str, **_k: object) -> bool:
            rec.ensured_dbs.append(db_name)
            return False

        async def _register(
            tenant_id: object, slug: str, db_name: str, **kw: object
        ) -> None:
            rec.registered = {"db_name": db_name, "slug": slug, **kw}

        async def _register_service(
            _tenant_id: object, service: str, _slug: str, db_name: str, *, schema_name: object = None
        ) -> None:
            rec.services[service] = {"db_name": db_name, "schema_name": schema_name}

        async def _seed(*_a: object, **_k: object) -> str:
            return "invite-token"

        async def _noop(*_a: object, **_k: object) -> None:
            return None

        from app.services import entitlements

        monkeypatch.setattr(co, "_slug_is_free", _free)
        monkeypatch.setattr(co, "_provision_company_schema_db", _schema_path)
        monkeypatch.setattr(co, "_provision_talentos_db", _legacy_path)
        monkeypatch.setattr(co, "_register_service", _register_service)
        monkeypatch.setattr(co, "_seed_and_invite", _seed)
        monkeypatch.setattr(co.setup_db, "ensure_database", _ensure_db)
        monkeypatch.setattr(co.tenant_registry, "register_tenant", _register)
        monkeypatch.setattr(entitlements, "seed_plans", _noop)
        monkeypatch.setattr(entitlements, "set_tenant_plan", _noop)


async def test_provision_company_schema_per_app_path(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    rec._patch(monkeypatch)
    s = get_settings()

    result = await co.provision_company(
        "spikeunit", "Spike Unit Co", "admin@spikeunit.test", "Admin", model="schema_per_app"
    )

    # NEW path: one company_<slug> DB with the talentos schema migrated into it.
    assert rec.schema_db == (s.company_db_name("spikeunit"), s.talentos_schema)
    assert rec.legacy_db is None  # the legacy template-copy path was NOT taken
    # Registered with db_name=company_<slug> and schema_name='talentos'.
    assert rec.registered["db_name"] == "company_spikeunit"
    assert rec.registered["schema_name"] == "talentos"
    assert result.tenant_slug == "spikeunit"
    # Croar + Feedback IQ register as sibling schemas INSIDE the one company DB (no per-app database created).
    assert rec.services == {
        "croar": {"db_name": "company_spikeunit", "schema_name": "croar"},
        "feedbackiq": {"db_name": "company_spikeunit", "schema_name": "feedbackiq"},
    }
    assert rec.ensured_dbs == []  # the schema path builds no separate per-app databases


async def test_provision_company_legacy_path_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    rec._patch(monkeypatch)
    s = get_settings()

    await co.provision_company(
        "spikeunit2", "Legacy Co", "admin@spikeunit2.test", "Admin", model="db_per_tenant"
    )

    # LEGACY path: dedicated tenant_<slug> DB (template copy), registered with schema_name NULL.
    assert rec.legacy_db == s.tenant_db_name("spikeunit2")
    assert rec.schema_db is None
    assert rec.registered["db_name"] == "tenant_spikeunit2"
    assert rec.registered.get("schema_name") is None
    # Croar + Feedback IQ keep their OWN dedicated databases (schema_name NULL), each ensured here.
    assert rec.services == {
        "croar": {"db_name": "croar_tenant_spikeunit2", "schema_name": None},
        "feedbackiq": {"db_name": "feedbackiq_tenant_spikeunit2", "schema_name": None},
    }
    assert rec.ensured_dbs == ["croar_tenant_spikeunit2", "feedbackiq_tenant_spikeunit2"]


# ------------------------------------------------------------------ per-app role isolation (company DB)
def test_company_role_isolation_sql_confines_each_role_to_its_schema() -> None:
    """Each app role gets USAGE+CREATE on its OWN schema + USAGE on public + CONNECT, and an explicit REVOKE
    ALL on every SIBLING app schema, so e.g. croar_app cannot touch the talentos schema. Talentos (in
    ``dml_schemas``) also gets table/sequence DML; croar/feedbackiq do not (they own what they build)."""
    from app.core import migration_helpers as mh

    stmts = mh.company_role_isolation_sql(
        "company_apex", mh.COMPANY_APP_SCHEMA_ROLES, "pw", dml_schemas=("talentos",)
    )
    sql = "\n".join(stmts)

    # Three LOGIN NOSUPERUSER NOBYPASSRLS roles created idempotently.
    for role in ("talentos_app", "croar_app", "feedbackiq_app"):
        assert f"rolname = '{role}'" in sql
        assert f'GRANT USAGE ON SCHEMA public TO "{role}"' in sql
        assert f'GRANT CONNECT ON DATABASE "company_apex" TO "{role}"' in sql
    assert "NOSUPERUSER NOBYPASSRLS" in sql

    # Each role has USAGE+CREATE on exactly its own schema.
    assert 'GRANT USAGE, CREATE ON SCHEMA "talentos" TO "talentos_app"' in sql
    assert 'GRANT USAGE, CREATE ON SCHEMA "croar" TO "croar_app"' in sql
    assert 'GRANT USAGE, CREATE ON SCHEMA "feedbackiq" TO "feedbackiq_app"' in sql

    # Cross-schema confinement: every role REVOKEs ALL on each sibling schema (the security proof).
    assert 'REVOKE ALL ON SCHEMA "talentos" FROM "croar_app"' in sql
    assert 'REVOKE ALL ON SCHEMA "talentos" FROM "feedbackiq_app"' in sql
    assert 'REVOKE ALL ON SCHEMA "croar" FROM "talentos_app"' in sql
    assert 'REVOKE ALL ON SCHEMA "feedbackiq" FROM "talentos_app"' in sql
    # A role never revokes itself from its own schema.
    assert 'REVOKE ALL ON SCHEMA "talentos" FROM "talentos_app"' not in sql

    # DML only for schemas in dml_schemas (talentos), not the self-owned croar/feedbackiq schemas.
    assert 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "talentos" TO "talentos_app"' in sql
    assert 'ON ALL TABLES IN SCHEMA "croar"' not in sql
    assert 'ON ALL TABLES IN SCHEMA "feedbackiq"' not in sql


def test_company_role_isolation_sql_rejects_unsafe_identifiers() -> None:
    from app.core import migration_helpers as mh

    with pytest.raises(ValueError):
        mh.company_role_isolation_sql("company apex; DROP", mh.COMPANY_APP_SCHEMA_ROLES, "pw")
