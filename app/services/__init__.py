"""Service layer (AD-1): ``app/services/<domain>_service.py`` or ``app/services/<domain>/``.

``load_erasure_handlers()`` imports every ``app/services/<domain>_erasure.py`` module so each domain's
erasure handlers are registered with ``app.services.privacy.registry`` (Epic 12, FR-68). It runs lazily
(first use of the registry) rather than at package import, so importing any service stays cheap and free of
import cycles.
"""

import importlib
import pkgutil

_erasure_loaded = False


def load_erasure_handlers() -> None:
    global _erasure_loaded  # process-wide, idempotent
    if _erasure_loaded:
        return
    for module in pkgutil.iter_modules(__path__):
        if module.name.endswith("_erasure"):
            importlib.import_module(f"{__name__}.{module.name}")
    _erasure_loaded = True
