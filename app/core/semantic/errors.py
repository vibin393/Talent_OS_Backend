"""Semantic layer errors (Epic 36 contract 0.6).

* ``SemanticRegistryError`` -- a bad registration; raised at import / startup, never at request time.
* ``SemanticError`` -- 422 with one of the ``semantic_*`` codes and ``details.path`` (JSON path into the
  definition, e.g. ``filters.and[0].field``).
* ``SemanticTimeout`` -- 503 ``query_timeout`` when the statement timeout cancels a semantic query.
"""

from typing import Any

from app.core.errors import DomainError, ValidationFailed

__all__ = [
    "FIELD_FORBIDDEN",
    "OPERATOR_NOT_ALLOWED",
    "TOO_COMPLEX",
    "UNKNOWN_FIELD",
    "UNKNOWN_JOIN",
    "UNKNOWN_SOURCE",
    "VALUE_TYPE",
    "SemanticError",
    "SemanticRegistryError",
    "SemanticTimeout",
]

UNKNOWN_SOURCE = "semantic_unknown_source"
UNKNOWN_FIELD = "semantic_unknown_field"
UNKNOWN_JOIN = "semantic_unknown_join"
OPERATOR_NOT_ALLOWED = "semantic_operator_not_allowed"
VALUE_TYPE = "semantic_value_type"
TOO_COMPLEX = "semantic_too_complex"
FIELD_FORBIDDEN = "semantic_field_forbidden"


class SemanticRegistryError(Exception):
    """A semantic source / field registration is invalid (raised at import / startup)."""


class SemanticError(ValidationFailed):
    """422 ``semantic_*`` with ``details.path``."""

    def __init__(self, code: str, message: str, path: str, **details: Any) -> None:
        super().__init__(message, code=code, details={"path": path, **details})
        self.path = path


class SemanticTimeout(DomainError):  # ruff: ignore[error-suffix-on-exception-name] - public name fixed by the contract (0.6)
    status_code = 503
    code = "query_timeout"
    default_message = "The query took too long and was cancelled"
