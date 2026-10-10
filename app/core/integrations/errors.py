"""Connector error classes (Epic 66, spec 0.10, AD-81). Adapters raise these; the connector job runner classifies them:

* ``ConnectorAuthError`` -- the provider refused the credentials: the Installation turns ``degraded``
  (``last_error_code='auth_failed'``), the job ends ``dead`` (no automatic retry; reconnect first).
* ``ConnectorRateLimited(retry_after)`` -- rescheduled after ``retry_after`` seconds; the attempt is not counted.
* ``ConnectorTransientError`` -- bounded exponential back-off (the ``connector_jobs`` rules).
* ``ConnectorPermanentError(code)`` -- fails the item (or the run when raised by the run), never retried.
* ``ManualActionRequired`` -- the operation needs a human (Manual adapters): the job succeeds with ``{"manual": true}``.

Messages must never carry a secret value (the Conformance Kit checks it).
"""

from collections.abc import Mapping
from typing import Any

__all__ = [
    "ConnectorAuthError",
    "ConnectorError",
    "ConnectorPermanentError",
    "ConnectorRateLimited",
    "ConnectorTransientError",
    "ManifestError",
    "ManualActionRequired",
]


class ConnectorError(Exception):
    """Base of the classified adapter errors."""

    code: str = "connector_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class ConnectorAuthError(ConnectorError):
    code = "auth_failed"


class ConnectorRateLimited(ConnectorError):  # noqa: N818 - public name fixed by the spec
    code = "rate_limited"

    def __init__(self, retry_after: float = 60.0, message: str | None = None) -> None:
        self.retry_after = max(float(retry_after), 1.0)
        super().__init__(message or f"rate limited; retry after {int(self.retry_after)} s")


class ConnectorTransientError(ConnectorError):
    code = "transient"


class ConnectorPermanentError(ConnectorError):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class ManualActionRequired(Exception):  # noqa: N818 - domain name fixed by the spec
    """The operation needs a human (Manual connector): the job ends ``succeeded`` with ``{"manual": true}``.

    ``app.services.connectors.base.ManualActionRequired`` is this class (one type, caught everywhere)."""

    def __init__(self, instructions: str, details: Mapping[str, Any] | None = None) -> None:
        self.instructions = instructions
        self.details = dict(details or {})
        super().__init__(instructions)


class ManifestError(ValueError):
    """An invalid Connector Manifest (raised by ``register_connector`` for structural errors)."""
