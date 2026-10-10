import pytest
from pydantic import ValidationError

from app.core.migration_helpers import create_app_role_sql
from app.core.settings import Settings


def _settings(**kw: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "development",
        "db_host": "db.local",
        "db_port": 5432,
        "db_name": "main",
        "db_user": "owner",
        "db_password": "p@ss:/",
        "db_app_user": "talent_app",
        "db_app_password": "p@ss:/",
        "redis_host": "localhost",
        "redis_port": 6379,
        "redis_password": "p@ss:/",
    }
    base.update(kw)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def test_db_urls_percent_encode_credentials() -> None:
    s = _settings()
    assert s.app_db_url() == "postgresql+asyncpg://talent_app:p%40ss%3A%2F@db.local:5432/main"
    assert s.owner_db_url("other") == "postgresql+asyncpg://owner:p%40ss%3A%2F@db.local:5432/other"


def test_db_url_supports_ipv6_host() -> None:
    url = _settings(db_host="::1").app_db_url()
    assert url == "postgresql+asyncpg://talent_app:p%40ss%3A%2F@[::1]:5432/main"


def test_empty_db_password_is_omitted() -> None:
    assert _settings(db_password="").owner_db_url() == "postgresql+asyncpg://owner@db.local:5432/main"


def test_redis_url_encoding_and_empty_password() -> None:
    assert _settings().redis_url(1) == "redis://:p%40ss%3A%2F@localhost:6379/1"
    assert _settings(redis_password="").redis_url() == "redis://localhost:6379/0"


def test_production_rejects_default_app_password() -> None:
    with pytest.raises(ValidationError, match="DB_APP_PASSWORD"):
        _settings(app_env="production", db_app_password="talent_app_dev")
    keys = {"audit_master_key": "k1", "mfa_encryption_key": "k2"}
    p5 = {"vault_master_keys": {"1": "a2V5"}, "public_cursor_secret": "c"}  # Epic 66: required in production
    assert _settings(app_env="production", db_app_password="strong", **keys, **p5).app_env == "production"
    with pytest.raises(ValidationError, match="VAULT_MASTER_KEYS"):
        _settings(app_env="production", db_app_password="strong", **keys)


def test_production_requires_encryption_keys() -> None:
    with pytest.raises(ValidationError, match="AUDIT_MASTER_KEY"):
        _settings(app_env="production", db_app_password="strong", mfa_encryption_key="k")
    with pytest.raises(ValidationError, match="MFA_ENCRYPTION_KEY"):
        _settings(app_env="production", db_app_password="strong", audit_master_key="k")


def test_unknown_app_env_rejected() -> None:
    with pytest.raises(ValidationError):
        _settings(app_env="prod")


def test_create_app_role_rejects_empty_password() -> None:
    with pytest.raises(ValueError, match="empty"):
        create_app_role_sql("talent_app", "")
