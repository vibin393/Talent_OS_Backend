"""Privacy service facade (Epic 12; ``from app.services import privacy as privacy_service``).

Modules: ``consent`` (consents, notices, AI-processing basis, gateway checker), ``dsr`` (data subject
requests, legal holds), ``registry`` (erasure-handler registry), ``erasure`` (runs), ``export`` (access
exports), ``retention`` (policy, preview, execute).
"""

from app.services.privacy import (
    class_expiry,
    consent,
    dsr,
    erasure,
    export,
    registry,
    retention,
    retention_bindings,
    retention_classes,
    retention_overrides,
)
from app.services.privacy.consent import (
    NOTICE_BASED_SOURCES,
    ai_consent_checker,
    basis_out,
    grant_consent,
    has_active_consent,
    initial_ai_consent_state,
    list_consents,
    recompute_ai_consent_state,
    request_consents,
    set_basis,
    withdraw_consent,
)
from app.services.privacy.dsr import DSR_TRANSITIONS, create_dsr
from app.services.privacy.registry import ErasureContext, ErasureHandler, register_erasure_handler


def install() -> None:
    """Startup wiring (API process and workers): the gateway consent checker and the event handlers."""
    from app.services.privacy import handlers

    consent.install()
    handlers.install()


__all__ = [
    "DSR_TRANSITIONS",
    "NOTICE_BASED_SOURCES",
    "ErasureContext",
    "ErasureHandler",
    "ai_consent_checker",
    "basis_out",
    "class_expiry",
    "consent",
    "create_dsr",
    "dsr",
    "erasure",
    "export",
    "grant_consent",
    "has_active_consent",
    "initial_ai_consent_state",
    "install",
    "list_consents",
    "recompute_ai_consent_state",
    "register_erasure_handler",
    "registry",
    "request_consents",
    "retention",
    "retention_bindings",
    "retention_classes",
    "retention_overrides",
    "set_basis",
    "withdraw_consent",
]
