"""``esign`` connector category (AD-29; owned by Epic 22). Importing the package registers the ``builtin``
(default) and ``manual`` adapters, and ``fake`` outside production."""

from app.services.connectors.esign import builtin, fake, manual, port

__all__ = ["builtin", "fake", "manual", "port"]
