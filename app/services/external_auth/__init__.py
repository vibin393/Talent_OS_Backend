"""External principals (Epic 13, AD-10 / AD-26): Client User and Vendor User sessions (``sessions``) and
``me`` (``service``). Sign-in, invitations and MFA enrolment are Epic 14. Facade:
``external_auth_service``."""

from app.services.external_auth import service, sessions

__all__ = ["service", "sessions"]
