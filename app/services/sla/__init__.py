"""Generic SLA domain (Epic 13, AD-31): milestone registry and clocks. Facade: ``app.services.sla_service``."""

from app.services.sla import registry, service

__all__ = ["registry", "service"]
