# ruff: file-ignore[line-too-long]
"""Facade of the ``exposure`` domain (Epic 66, D-11, D-25(d)): ``record`` (buffered), ``flush`` (beat ``exposure.flush``),
and the erasure fan-out readers ``installations_exposed`` / ``api_keys_exposed``."""

from app.services.exposure.record import Channel, api_keys_exposed, flush, installations_exposed, record

__all__ = ["Channel", "api_keys_exposed", "flush", "installations_exposed", "record"]
