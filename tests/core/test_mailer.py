import json
import uuid
from pathlib import Path

import pytest

from app.core.mailer import EmailMessage, OutboxMailer


def test_outbox_mailer_writes_json_and_keeps_sent(tmp_path: Path) -> None:
    mailer = OutboxMailer(tmp_path / "outbox", "Talent OS <no-reply@example.com>")
    tenant_id = uuid.uuid4()
    msg = EmailMessage(
        to="person@example.com", subject="Hi", text="secret link", tenant_id=tenant_id, kind="test"
    )
    mailer.send(msg)

    assert list(mailer.sent) == [msg]
    path = mailer.outbox_path(msg)
    assert path is not None and path.parent == tmp_path / "outbox"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["to"] == "person@example.com"
    assert data["from"] == "Talent OS <no-reply@example.com>"
    assert data["text"] == "secret link"
    assert data["tenant_id"] == str(tenant_id)
    assert data["created_at"].endswith("Z")


def test_outbox_mailer_refused_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import mailer as mailer_mod
    from app.core.settings import get_settings

    monkeypatch.setattr(get_settings(), "app_env", "production")
    mailer_mod.get_mailer.cache_clear()
    try:
        with pytest.raises(mailer_mod.MailerConfigError):
            mailer_mod.get_mailer()
    finally:
        monkeypatch.undo()
        mailer_mod.get_mailer.cache_clear()


def test_outbox_sent_is_bounded(tmp_path: Path) -> None:
    mailer = OutboxMailer(tmp_path, "x@example.com")
    assert mailer.sent.maxlen == OutboxMailer.SENT_MAXLEN


def test_smtp_mailer_builds_and_sends_via_starttls(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.core import mailer as mailer_mod

    sent: dict[str, object] = {}

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            sent["conn"] = (host, port)

        def __enter__(self) -> "FakeSMTP":
            return self

        def __exit__(self, *exc: object) -> None:
            sent["closed"] = True

        def starttls(self, context: object) -> None:
            sent["tls"] = True

        def login(self, user: str, password: str) -> None:
            sent["login"] = user

        def send_message(self, msg: object) -> None:
            sent["msg"] = msg

    monkeypatch.setattr(mailer_mod.smtplib, "SMTP", FakeSMTP)
    copy = mailer_mod.OutboxMailer(tmp_path, "Support <s@example.com>")
    m = mailer_mod.SmtpMailer(
        "smtp.example.com", 587, "s@example.com", "pw", "Support <s@example.com>", copy_to=copy
    )
    msg = mailer_mod.EmailMessage(
        to="a@example.com",
        subject="Hi",
        text="plain",
        html="<p>html</p>",
        cc=("c@example.com",),
        reply_to="r@example.com",
        from_name="Ravi via Appxcess",
        attachments=(mailer_mod.EmailAttachment("invite.ics", "text/calendar", b"BEGIN:VCALENDAR"),),
    )
    m.send(msg)

    assert sent["conn"] == ("smtp.example.com", 587) and sent["tls"] and sent["login"] == "s@example.com"
    mime = sent["msg"]
    assert mime["To"] == "a@example.com" and mime["Cc"] == "c@example.com"  # type: ignore[index]
    assert mime["Reply-To"] == "r@example.com"  # type: ignore[index]
    assert mime["From"] == "Ravi via Appxcess <s@example.com>"  # type: ignore[index]
    assert [p.get_filename() for p in mime.iter_attachments()] == ["invite.ics"]  # type: ignore[attr-defined]
    assert len(copy.sent) == 1  # dev outbox copy kept
