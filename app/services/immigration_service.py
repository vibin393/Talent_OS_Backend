# ruff: file-ignore[line-too-long]
"""Facade of the ``immigration`` domain (Epic 46, AD-52). Other domains call these, never the package internals.

Public API: ``permits_for``, ``quick_search`` (Epic 52 global search, type ``immigration_case``) and
``import_immigration_case`` (Epic 52 go-live import, FR-263).
"""

from app.services.immigration.service import import_immigration_case, permits_for, quick_search

__all__ = ["import_immigration_case", "permits_for", "quick_search"]
