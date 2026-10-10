"""Facade of the ``external_auth`` domain (Epic 13)."""

from app.services.external_auth.service import me
from app.services.external_auth.sessions import create_session, resolve, revoke_all

__all__ = ["create_session", "me", "resolve", "revoke_all"]
