"""``sms`` connector category (AD-29; owned by Epic 33 after the foundation). Importing the package registers
the ``manual`` and ``msg91`` adapters (and ``fake`` outside production)."""

from app.services.connectors.sms import channel, fake, manual, msg91, port

__all__ = ["channel", "fake", "manual", "msg91", "port"]
