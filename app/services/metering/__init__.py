# ruff: file-ignore[line-too-long]
"""Usage metering and Rate Tiers (Epic 66, AD-78, FR-338, NFR-71/74). Informational only (D-12): no billing objects.
Facade: ``app.services.metering_service``.

* ``record`` -- hot-path counters (Redis hash per tenant and day, in-process buffer fallback).
* ``flush`` -- counters -> ``usage_counters`` (beat ``metering.flush``, 60 s).
* ``enforce`` -- Rate Tier limits and the daily quota (429 ``rate_limited`` / ``quota_exceeded``).
* ``request_log`` -- the after-response ``api_request_log`` writer that also meters ``api_request``.
* ``read`` -- tenant and platform usage reads.
* ``reconcile`` -- daily metered-vs-logged drift check (> 0.1 % alerts).
* ``partitions`` -- monthly partition registry, ``ensure_monthly`` and the retention purge.
"""
