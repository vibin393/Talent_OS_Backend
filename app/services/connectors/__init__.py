"""Provider connector framework (Epic 13, AD-29, NFR-31): generic port (``base``), adapter / webhook
registries (``registry``), outbound jobs (``jobs``), configurations and webhooks (``service``) and one
sub-package per category (``esign``, ``bgv``, ``einvoice``, ``payroll``, ``accounting``, ``sms``,
``whatsapp``; Epic 36 adds ``assessment``, ``dsc``, ``tds_statement``, ``fx``; Epic 53 adds ``voice``; Epic 66 adds the manifest categories ``job_board``, ``hrms``, ``stt``) with
``port`` / ``manual`` / ``fake``. Facade: ``app.services.connectors_service``."""

from app.services.connectors import (
    accounting,
    assessment,
    base,
    bgv,
    dsc,
    einvoice,
    esign,
    fx,
    hrms,
    job_board,
    jobs,
    payroll,
    registry,
    service,
    sms,
    stt,
    tds_statement,
    voice,
    whatsapp,
)

__all__ = [
    "accounting",
    "assessment",
    "base",
    "bgv",
    "dsc",
    "einvoice",
    "esign",
    "fx",
    "hrms",
    "job_board",
    "jobs",
    "payroll",
    "registry",
    "service",
    "sms",
    "stt",
    "tds_statement",
    "voice",
    "whatsapp",
]
