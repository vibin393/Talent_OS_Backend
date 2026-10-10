"""``stt`` connector category (Epic 66 contract; Epic 76 fills real adapters). Importing the package registers the
``fake_stt`` manifest (Fake + Manual adapters; also bridged as the ``fake`` / ``manual`` provider keys)."""

from app.services.connectors.stt import fake, hooks, manual, port

__all__ = ["fake", "hooks", "manual", "port"]
