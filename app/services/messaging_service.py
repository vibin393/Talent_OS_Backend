# ruff: file-ignore[line-too-long]
"""Facade of the messaging domain (Epic 33): SMS / WhatsApp templates, sends, opt-outs, manual queue, portal."""

from app.services.messaging import connectors_admin, inbound, notices, opt_outs, portal, sending, templates

__all__ = ["connectors_admin", "inbound", "notices", "opt_outs", "portal", "sending", "templates"]
