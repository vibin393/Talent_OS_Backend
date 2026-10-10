"""Shared helpers for manifest Fake adapters (Epic 66, spec 0.9): deterministic, no network, scripted through
``settings.connector_fake_script_dir/<key>.yaml`` or in-test helpers (``FakeClass.script`` / ``fail_ops`` /
``reset()``).

Scripted behaviours common to every fake: the secret ``api_key`` equal to ``"invalid"`` (or ``script["auth_fail"]``)
raises ``ConnectorAuthError``; ops in ``script["rate_limit_ops"]`` raise ``ConnectorRateLimited(30)``; ops in
``fail_ops`` raise ``ConnectorTransientError``. Webhooks: HMAC-SHA256 of the body in ``X-Fake-Signature`` with the
``webhook_secret`` secret (``FAKE_WEBHOOK``), event id at ``/event_id``.
"""

import functools
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from app.core.integrations.context import AdapterContext
from app.core.integrations.errors import ConnectorAuthError, ConnectorRateLimited, ConnectorTransientError
from app.core.integrations.manifest import WebhookSpec
from app.core.signing import verify_hmac

__all__ = ["FAKE_WEBHOOK", "FakeAdapterBase", "load_script", "sign_fake_webhook", "tiny_pdf"]

FAKE_WEBHOOK = WebhookSpec(scheme="hmac_sha256", signature_header="X-Fake-Signature", event_id_path="/event_id")


def tiny_pdf(text: str) -> bytes:
    """A minimal, valid one-page PDF showing ``text`` (deterministic bytes)."""
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")[:200]
    stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


@functools.cache
def _file_script(key: str) -> Mapping[str, Any]:
    from app.core.settings import get_settings

    path = Path(get_settings().connector_fake_script_dir) / f"{key}.yaml"
    if not path.is_file():
        return {}
    import yaml  # type: ignore[import-untyped,unused-ignore]

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def load_script(key: str) -> Mapping[str, Any]:
    """The YAML script of a fake connector (``{}`` when there is none); cached per process."""
    return _file_script(key)


def sign_fake_webhook(secret: str, body: bytes) -> dict[str, str]:
    """Headers for a Fake webhook body (tests and the Conformance Kit)."""
    import hmac

    return {"X-Fake-Signature": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}


class FakeAdapterBase:
    """Base of the manifest Fakes: call recording, scripted failures, generic port, HMAC webhooks."""

    category: ClassVar[str] = ""
    provider_key: ClassVar[str] = ""
    calls: ClassVar[list[tuple[str, Any]]]
    fail_ops: ClassVar[set[str]]
    script: ClassVar[dict[str, Any]]

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        cls.calls = []
        cls.fail_ops = set()
        cls.script = {}

    def __init__(self, ctx: AdapterContext) -> None:
        self.ctx = ctx
        self.key = type(self).provider_key

    def __repr__(self) -> str:
        return f"{type(self).__name__}(mode={self.ctx.mode})"

    @classmethod
    def reset(cls) -> None:
        cls.calls.clear()
        cls.fail_ops.clear()
        cls.script.clear()
        _file_script.cache_clear()

    def scripted(self) -> dict[str, Any]:
        merged = dict(load_script(type(self).provider_key))
        merged.update(type(self).script)
        return merged

    def _check(self, op: str, args: Any = None) -> None:
        cls = type(self)
        cls.calls.append((op, args))
        script = self.scripted()
        if script.get("auth_fail") or self.ctx.secrets.get("api_key") == "invalid":
            msg = "The provider rejected the credentials"
            raise ConnectorAuthError(msg)
        if op in set(script.get("rate_limit_ops") or ()):
            raise ConnectorRateLimited(30)
        if op in cls.fail_ops:
            msg = f"fake {op} failure"
            raise ConnectorTransientError(msg)

    # ------------------------------------------------------------------------------------------- ConnectorPort
    async def handle(self, op: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self._check(op, dict(payload))
        if op == "health":
            return {"ok": True}
        digest = hashlib.sha256(json.dumps(dict(payload), sort_keys=True, default=str).encode()).hexdigest()
        return {"ok": True, "op": op, "ref": f"fake-{digest[:12]}"}

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        secret = self.ctx.secrets.get("webhook_secret")
        if not secret:
            return False
        return verify_hmac(
            FAKE_WEBHOOK.scheme, secret.encode(), headers, body, signature_header=FAKE_WEBHOOK.signature_header
        )

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:  # noqa: ARG002
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {"data": data}
