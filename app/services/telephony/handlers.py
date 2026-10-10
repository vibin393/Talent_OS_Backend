# ruff: noqa: E501
"""After-commit event handlers (Epic 76): queue the telephony tasks on ``telephony`` and react to withdrawals.

* ``TelephonyRecordingReady`` -> ``telephony.fetch_recording``; ``TelephonyProviderDeleteQueued`` ->
  ``telephony.delete_provider_recording``; ``SttJobQueued`` -> ``telephony.transcribe``.
* ``ConsentWithdrawn(call_recording | voice_screening)`` -> ``telephony.withdraw`` (provider copies deleted; the P4
  purge removes the stored file within its 24 h window).
"""

from typing import Any

from app.core.events import subscribe


def _enqueue(task_name: str, *args: str) -> None:
    from app.tasks import telephony as tasks
    from app.tasks.runtime import enqueue

    enqueue(getattr(tasks, task_name), *args)


def on_recording_ready(e: Any) -> None:
    _enqueue("fetch_recording", str(e.tenant_id), str(e.call_id))


def on_delete_queued(e: Any) -> None:
    _enqueue("delete_provider_recording", str(e.tenant_id), str(e.call_id))


def on_stt_queued(e: Any) -> None:
    _enqueue("transcribe", str(e.tenant_id), str(e.job_id))


def on_consent_withdrawn(e: Any) -> None:
    if getattr(e, "purpose", None) in ("call_recording", "voice_screening") and getattr(e, "candidate_id", None):
        _enqueue("withdraw", str(e.tenant_id), str(e.candidate_id))


def register() -> None:
    from app.events.privacy import ConsentWithdrawn
    from app.events.telephony import SttJobQueued, TelephonyProviderDeleteQueued, TelephonyRecordingReady

    on_recording_ready.__name__ = "telephony_on_recording_ready"
    on_delete_queued.__name__ = "telephony_on_delete_queued"
    on_stt_queued.__name__ = "telephony_on_stt_queued"
    on_consent_withdrawn.__name__ = "telephony_on_consent_withdrawn"
    subscribe(TelephonyRecordingReady, on_recording_ready)
    subscribe(TelephonyProviderDeleteQueued, on_delete_queued)
    subscribe(SttJobQueued, on_stt_queued)
    subscribe(ConsentWithdrawn, on_consent_withdrawn)
