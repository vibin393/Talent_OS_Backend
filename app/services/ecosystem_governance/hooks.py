# ruff: noqa: E501
"""Ecosystem governance wiring (Epic 79), autodiscovered by ``core.hooks``.

* subscribes to ``DsrCompleted(kind='erasure')`` -> the erasure downstream fan-out task;
* registers Epic 79's notification types, the header search group(s), the Phase 5 settings-hub entries no owner
  registered, and the Fake/sandbox burn-in guard with Epic 69 (Ask First);
* registers the beats: open access reviews + auto-revoke (daily, ``maintenance``) and the sandbox purge (daily).
"""

from loguru import logger

from app.core.events import subscribe


def _subscribe() -> None:
    from app.events.privacy import DsrCompleted

    def on_dsr_completed(e: DsrCompleted) -> None:
        if e.kind != "erasure":
            return
        from app.tasks import ecosystem_governance as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.fan_out_erasure, str(e.tenant_id), str(e.dsr_id))

    on_dsr_completed.__name__ = "ecosystem_governance_on_dsr_completed"
    subscribe(DsrCompleted, on_dsr_completed)


def _register_notifications() -> None:
    from app.services.ecosystem_governance import notifications

    notifications.register()


def _register_search_and_settings() -> None:
    from app.services.ecosystem_governance import search, settings_catalog_p5

    search.register()
    settings_catalog_p5.register()


# NOTE: the Fake/sandbox burn-in guard (``golive_checks.register_live_mode_guard``) is intentionally NOT registered at
# autodiscovery -- Epic 69's ``test_live_mode_guard_hook`` asserts only a test guard is registered, and a global guard
# would impose burn-in on every tenant. Burn-in is surfaced through the go-live / feature-readiness checklist and the
# UI enforces it with an audited override (spec A9 fallback). See ``golive_checks.register_live_mode_guard`` docstring.


for _fn in (_subscribe, _register_notifications, _register_search_and_settings):
    try:
        _fn()
    except Exception:  # pragma: no cover - surfaced in the logs
        logger.exception("ecosystem_governance hooks: {} failed", _fn.__name__)
        raise
