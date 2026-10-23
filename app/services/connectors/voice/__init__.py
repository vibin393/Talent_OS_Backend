# ruff: file-ignore[line-too-long]
"""``voice`` connector category (AD-29 / AD-65; Epic 53 contract, spec 0.13; owned by Epic 58). Importing the package
registers the ``manual`` adapter, and ``fake`` outside production. There is no real STT adapter in Phase 4. Port
signatures are frozen."""

from app.services.connectors.voice import fake, manual, port

__all__ = ["fake", "manual", "port"]
