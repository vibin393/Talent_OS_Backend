"""Interviews service package (Epic 7, stories 7.1-7.3). Facade: ``app.services.interviews_service``.

Modules: ``configuration`` (settings, kits, stage templates), ``plan`` (per-demand stage plans), ``timeutil``
(pure zone conversion), ``scheduling`` (interviews + participants), ``ics`` (iCalendar), ``invites`` (invite
sync, reminders, scorecard nudges -- Celery only), ``scorecards`` (blind rule, public links), ``outcomes``
(stage outcomes, selection, summary, board badges), ``handlers`` (events -> tasks), ``ports`` (other domains).
"""
