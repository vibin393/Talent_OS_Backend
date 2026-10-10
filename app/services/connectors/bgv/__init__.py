"""``bgv`` connector category (AD-29; owned by Epic 20 after the foundation). Importing the package
registers the ``manual`` adapter (and ``fake`` outside production)."""

from app.services.connectors.bgv import fake, manual, port

__all__ = ["fake", "manual", "port"]
