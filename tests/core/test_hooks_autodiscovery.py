"""AD-31: ``hooks.py`` autodiscovery imports every hooks module exactly once (Epic 13)."""

import importlib
import sys

import pytest

from app.core import hooks
from app.core.registry import RegistryError


def test_every_hooks_module_is_imported_once() -> None:
    first = hooks.autodiscover("app.services")
    assert "app.services.legal_entities.hooks" in first
    assert "app.services.external_auth.hooks" in first
    assert "app.services.placements.hooks" in first
    assert "app.services.candidates.hooks" in first
    for name in first:
        assert name in sys.modules
    modules_before = {name: id(sys.modules[name]) for name in first}
    second = hooks.autodiscover("app.services")
    assert second == first
    assert {name: id(sys.modules[name]) for name in second} == modules_before
    assert hooks.discovered("app.services") == first


def test_app_creation_runs_autodiscovery() -> None:
    from app.main import create_app

    create_app()
    assert "app.services.connectors.hooks" in hooks.discovered("app.services")


def test_duplicate_registration_raises() -> None:
    from app.services.notifications_service import register_recipient_directory

    hooks.autodiscover("app.services")  # registers client_user / vendor_user / candidate directories

    async def directory(db: object, rid: object) -> None:
        return None

    with pytest.raises(RegistryError):
        register_recipient_directory("client_user", directory)  # type: ignore[arg-type]
    # Re-importing a hooks module through importlib.reload would re-register: duplicates raise.
    module = sys.modules["app.services.external_auth.hooks"]
    with pytest.raises(RegistryError):
        importlib.reload(module)


def test_celery_signals_wired() -> None:
    from celery.signals import beat_init, worker_init

    from app.core import celery as core_celery

    assert any(r[1]() is core_celery._autodiscover_hooks or r[1] is core_celery._autodiscover_hooks for r in worker_init.receivers)  # type: ignore[union-attr]
    assert any(r[1]() is core_celery._autodiscover_hooks or r[1] is core_celery._autodiscover_hooks for r in beat_init.receivers)  # type: ignore[union-attr]
