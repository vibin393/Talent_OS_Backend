"""``einvoice`` connector category (AD-29; owned by Epic 29 after the foundation). Importing the package
registers the ``manual`` adapter (and ``fake`` outside production)."""

from app.services.connectors.einvoice import fake, manual, port

__all__ = ["fake", "manual", "port"]
