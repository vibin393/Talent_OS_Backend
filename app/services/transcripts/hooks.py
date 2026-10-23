# ruff: file-ignore[line-too-long]
"""Registrations of the ``transcripts`` domain (autodiscovered by ``core.hooks``), Epic 53 (spec 0.15).

Retention bindings: ``conversation_records`` <- ``transcript_segments``; ``recordings`` <- ``voice_sessions`` and
``interview_captures`` (expiry deletes the recording file and nulls the FK). The classes are seeded by
``privacy.retention_classes.seed_p4_classes``. The erasure handlers live in ``services/transcripts_erasure.py``.
"""

from loguru import logger


def _register_retention() -> None:
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings
    from app.services.transcripts import retention

    bindings = retention_bindings()
    if "transcript_segments" not in bindings:
        register_retention_binding(
            "transcript_segments",
            "conversation_records",
            subject_resolver=retention.segment_subject,
            anchor_resolver=retention.segment_anchor,
            expire_fn=retention.expire_segments,
        )
    if "voice_sessions" not in bindings:
        register_retention_binding(
            "voice_sessions",
            "recordings",
            subject_resolver=retention.voice_subject,
            anchor_resolver=retention.voice_anchor,
            expire_fn=retention.expire_voice_recordings,
        )
    if "interview_captures" not in bindings:
        register_retention_binding(
            "interview_captures",
            "recordings",
            subject_resolver=retention.capture_subject,
            anchor_resolver=retention.capture_anchor,
            expire_fn=retention.expire_capture_recordings,
        )


for _fn in (_register_retention,):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block start-up on an optional registry
        logger.exception("transcripts: {} failed", _fn.__name__)
