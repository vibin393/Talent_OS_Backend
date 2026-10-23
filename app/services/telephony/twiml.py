# ruff: file-ignore[line-too-long]
"""Answer documents (Epic 76, spec A3): TwiML for Twilio, Plivo XML for Plivo. The recording notice is spoken and
recording requested **only** when ``recording_requested`` (the dial-time gate result); otherwise the candidate is
dialled without any record attribute. Exotel is flow-based (no answer document)."""

from xml.sax.saxutils import escape, quoteattr

__all__ = ["render"]


def _twilio(candidate: str, caller_id: str, notice: str | None, record: bool, callback: str, max_s: int) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Response>"]
    if record and notice:
        parts.append(f'<Say language="en-IN">{escape(notice)}</Say>')
    attrs = f"callerId={quoteattr(caller_id)} timeLimit={quoteattr(str(max_s))} answerOnBridge=\"true\""
    if record:
        attrs += f' record="record-from-answer-dual" recordingStatusCallback={quoteattr(callback)}'
        attrs += ' recordingStatusCallbackEvent="completed"'
    number_attrs = f"statusCallback={quoteattr(callback)} statusCallbackEvent=\"answered completed\""
    parts.append(f"<Dial {attrs}><Number {number_attrs}>{escape(candidate)}</Number></Dial>")
    parts.append("</Response>")
    return "".join(parts)


def _plivo(candidate: str, caller_id: str, notice: str | None, record: bool, callback: str, max_s: int) -> str:
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<Response>"]
    if record:
        if notice:
            parts.append(f'<Speak language="en-IN">{escape(notice)}</Speak>')
        parts.append(
            f'<Record action={quoteattr(callback)} callbackUrl={quoteattr(callback)} recordSession="true" '
            f'redirect="false" fileFormat="wav" maxLength={quoteattr(str(max_s))}/>'
        )
    parts.append(
        f"<Dial callerId={quoteattr(caller_id.lstrip('+'))} timeLimit={quoteattr(str(max_s))} "
        f"callbackUrl={quoteattr(callback)}><Number>{escape(candidate.lstrip('+'))}</Number></Dial>"
    )
    parts.append("</Response>")
    return "".join(parts)


def render(
    provider_key: str,
    *,
    candidate_e164: str,
    caller_id_e164: str,
    notice: str | None,
    record: bool,
    callback_url: str,
    max_duration_s: int,
) -> tuple[str, str] | None:
    """``(media_type, document)`` for the provider, or None (Exotel / unknown)."""
    if provider_key in ("twilio", "fake"):
        return "application/xml", _twilio(candidate_e164, caller_id_e164, notice, record, callback_url, max_duration_s)
    if provider_key == "plivo":
        return "application/xml", _plivo(candidate_e164, caller_id_e164, notice, record, callback_url, max_duration_s)
    return None
