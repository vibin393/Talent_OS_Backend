# ruff: file-ignore[line-too-long]
"""Facade of the ``transcripts`` domain (Epic 53, AD-66, spec 0.13). Other domains call these, never the package
internals.

Public API: ``create``, ``append_segments``, ``ingest_upload``, ``get``, ``for_model``, ``erase``.
"""

from app.services.transcripts.service import append_segments, create, erase, for_model, get, ingest_upload

__all__ = ["append_segments", "create", "erase", "for_model", "get", "ingest_upload"]
