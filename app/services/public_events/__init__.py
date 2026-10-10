# ruff: noqa: E501
"""Public events domain (Epic 66, AD-79, FR-334/350): the transactional outbox writer wiring, the Public Event catalogue
v1, the relay (``dispatch``) and the sink registry. Public API: the facade ``app.services.public_events_service``.
Layer-4 package of ``p5-dag``: never imports another Phase 5 package or facade.
"""
