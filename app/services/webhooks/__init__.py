# ruff: noqa: E501
"""Webhooks domain (Epic 68, AD-79/AD-80, FR-349..FR-353). Callers use ``app.services.webhooks_service`` only.

* ``subscriptions`` -- CRUD, scope / filter validation, limits; ``verification`` -- the signed challenge handshake;
* ``signing`` -- secret lifecycle and headers; ``sink`` -- the outbox sink; ``erasure_fanout`` -- D-11;
* ``delivery`` -- one attempt through ``SafeHttpClient``; ``scheduler`` -- pump, retries, fairness, concurrency;
* ``health`` -- failure streaks and auto-disable; ``replay``; ``test_events``; ``echo`` -- the sandbox receiver.
"""
