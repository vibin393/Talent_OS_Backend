# ruff: noqa: E501
"""Background verification and reference checks (Epic 20, FR-110..FR-114).

Modules: ``packages`` (configuration), ``cases`` (creation, start, cancel, reads, document review), ``status``
(case state machine + automation), ``checks`` (assign / start / record / waive / packet / overdue),
``discrepancies``, ``adjudication`` (★ human-only, start-gate resolver, client status), ``references``,
``portal`` (Worker "My Work"), ``provider_links`` (Manual connector secure links), ``retention`` (evidence purge),
``consent_events``, ``events`` (local notification events), ``hooks`` (registrations).

The BGV provider is reached only through ``connectors/bgv`` (AD-29). ``bgv`` never imports ``compliance``,
``onboarding`` or ``ai``.
"""

from app.services.bgv import adjudication, cases, checks, discrepancies, packages, references, status

__all__ = ["adjudication", "cases", "checks", "discrepancies", "packages", "references", "status"]
