"""Facade of the ``segments`` domain (Epic 36 contract layer, AD-39; filled by Epic 38)."""

from app.services.segments.service import evaluate

__all__ = ["evaluate"]
