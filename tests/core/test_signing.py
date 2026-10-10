"""Story 66.7: webhook signing vectors and connector ingress verification (pure, no DB)."""

import base64
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from app.core.signing import sign_v1, signature_header, verify_hmac, verify_v1

VECTORS = json.loads((Path(__file__).parent / "signing_vectors.json").read_text(encoding="utf-8"))
NOW = 1_700_000_000


def test_vectors_file_has_enough_cases() -> None:
    assert len(VECTORS) >= 5


@pytest.mark.parametrize("vec", VECTORS, ids=lambda v: str(v["timestamp"]))
def test_vectors_round_trip(vec: dict) -> None:
    secret = bytes.fromhex(vec["secret_hex"])
    secondary = bytes.fromhex(vec["secondary_secret_hex"])
    body = vec["body"].encode("utf-8")
    ts = vec["timestamp"]
    independent = hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert sign_v1(secret, ts, body) == vec["expected_v1"] == independent
    assert signature_header([secret, secondary], ts, body) == vec["header"]
    assert verify_v1(vec["header"], [secret], body, now=ts)
    assert verify_v1(vec["header"], [secondary], body, now=ts)
    assert not verify_v1(vec["header"], [b"other"], body, now=ts)


def test_dual_secret_rotation() -> None:
    old, new, body = b"old-secret", b"new-secret", b'{"a":1}'
    header = signature_header([new, old], NOW, body)
    assert header.count("v1=") == 2
    # receivers holding either secret verify; a receiver holding both also verifies
    assert verify_v1(header, [old], body, now=NOW)
    assert verify_v1(header, [new], body, now=NOW)
    assert verify_v1(header, [b"x", new], body, now=NOW)
    single = signature_header([new], NOW, body)
    assert not verify_v1(single, [old], body, now=NOW)


def test_signature_header_requires_secret() -> None:
    with pytest.raises(ValueError):
        signature_header([], NOW, b"")


def test_tolerance_window() -> None:
    body = b"payload"
    header = signature_header([b"s"], NOW, body)
    assert verify_v1(header, [b"s"], body, now=NOW + 300)
    assert verify_v1(header, [b"s"], body, now=NOW - 300)
    assert not verify_v1(header, [b"s"], body, now=NOW + 301)
    assert not verify_v1(header, [b"s"], body, now=NOW - 301)
    assert verify_v1(header, [b"s"], body, tolerance=1000, now=NOW + 900)


def test_tamper_detected() -> None:
    body = b'{"amount":100}'
    header = signature_header([b"s"], NOW, body)
    assert not verify_v1(header, [b"s"], b'{"amount":900}', now=NOW)
    shifted = header.replace(f"t={NOW}", f"t={NOW + 1}")
    assert not verify_v1(shifted, [b"s"], body, now=NOW)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "garbage",
        "t=abc,v1=00",
        f"t={NOW}",
        "v1=" + "0" * 64,
        f"t={NOW},v1=",
        f"t={NOW},t={NOW},v1=" + "0" * 64,
        f"t=-{NOW},v1=" + "0" * 64,
        f"t={NOW},v1=zz,,",
    ],
)
def test_malformed_headers(header: str) -> None:
    assert not verify_v1(header, [b"s"], b"", now=NOW)


def test_no_secrets_never_verifies() -> None:
    header = signature_header([b"s"], NOW, b"")
    assert not verify_v1(header, [], b"", now=NOW)


def test_header_whitespace_and_uppercase_hex_accepted() -> None:
    body = b"x"
    sig = sign_v1(b"s", NOW, body)
    assert verify_v1(f"t={NOW}, v1={sig.upper()}", [b"s"], body, now=NOW)


# ------------------------------------------------------------------------------------------- verify_hmac


def test_hmac_sha256_hex_base64_prefix_and_case_insensitive_header() -> None:
    secret, body = b"whsec", b'{"id":1}'
    digest = hmac.new(secret, body, hashlib.sha256).digest()
    for value in (digest.hex(), "sha256=" + digest.hex(), base64.b64encode(digest).decode()):
        assert verify_hmac(
            "hmac_sha256",
            secret,
            {"x-hub-signature-256": value},
            body,
            signature_header="X-Hub-Signature-256",
        )
    assert verify_hmac("hmac_sha256", secret, {"X-SIGNATURE": digest.hex()}, body)
    assert not verify_hmac("hmac_sha256", secret, {"X-Signature": digest.hex()}, body + b" ")
    assert not verify_hmac("hmac_sha256", b"other", {"X-Signature": digest.hex()}, body)
    assert not verify_hmac("hmac_sha256", secret, {}, body)


def test_hmac_sha256_with_timestamp_header() -> None:
    secret, body = b"whsec", b"payload"
    digest = hmac.new(secret, f"{NOW}.".encode() + body, hashlib.sha256).hexdigest()
    headers = {"X-Sig": digest, "X-Ts": str(NOW)}
    kw = {"signature_header": "X-Sig", "timestamp_header": "X-Ts"}
    assert verify_hmac("hmac_sha256", secret, headers, body, now=NOW, **kw)
    assert not verify_hmac("hmac_sha256", secret, headers, body, now=NOW + 301, **kw)
    assert not verify_hmac("hmac_sha256", secret, {"X-Sig": digest}, body, now=NOW, **kw)


def test_twilio_hmac_sha1_url() -> None:
    secret = b"twilio-token"
    url = "https://api.example.com/hooks/twilio/sms"
    body = b"To=%2B919800000000&From=%2B14155550100&Body=Hello"
    data = url + "BodyHello" + "From+14155550100" + "To+919800000000"
    sig = base64.b64encode(hmac.new(secret, data.encode(), hashlib.sha1).digest()).decode()
    headers = {"x-twilio-signature": sig, "Content-Type": "application/x-www-form-urlencoded"}
    assert verify_hmac("hmac_sha1_url", secret, headers, body, url)
    assert not verify_hmac("hmac_sha1_url", secret, headers, body, url + "?x=1")
    assert not verify_hmac("hmac_sha1_url", secret, headers, body.replace(b"Hello", b"Bye"), url)
    assert not verify_hmac("hmac_sha1_url", secret, headers, body, None)


def test_plivo_v3() -> None:
    secret, url, nonce = b"plivo-token", "https://api.example.com/hooks/plivo", "12345678"
    sig = base64.b64encode(hmac.new(secret, (url + nonce).encode(), hashlib.sha256).digest()).decode()
    headers = {"X-Plivo-Signature-V3": f"bogus,{sig}", "X-Plivo-Signature-V3-Nonce": nonce}
    assert verify_hmac("plivo_v3", secret, headers, b"", url)
    assert not verify_hmac("plivo_v3", secret, {**headers, "X-Plivo-Signature-V3-Nonce": "1"}, b"", url)
    assert not verify_hmac("plivo_v3", secret, {"X-Plivo-Signature-V3": sig}, b"", url)


def test_basic() -> None:
    secret = b"user:pa:ss"
    token = base64.b64encode(secret).decode()
    assert verify_hmac("basic", secret, {"authorization": f"Basic {token}"}, b"")
    assert not verify_hmac("basic", secret, {"Authorization": f"Bearer {token}"}, b"")
    assert not verify_hmac("basic", secret, {"Authorization": "Basic !!!"}, b"")
    assert not verify_hmac("basic", b"user:other", {"Authorization": f"Basic {token}"}, b"")
    assert not verify_hmac("basic", secret, {}, b"")


def test_tokens() -> None:
    assert verify_hmac("path_token", b"tok", {"X-Channel-Token": "tok"}, b"")
    assert verify_hmac("path_token", b"tok", {"X-Custom": "tok"}, b"", signature_header="x-custom")
    assert not verify_hmac("path_token", b"tok", {"X-Channel-Token": "nope"}, b"")
    assert not verify_hmac("path_token", b"tok", {}, b"")
    assert verify_hmac("channel_token", b"tok", {"x-goog-channel-token": "tok"}, b"")
    assert verify_hmac("channel_token", b"tok", {"X-Channel-Token": "tok"}, b"")
    assert not verify_hmac("channel_token", b"tok", {"X-Goog-Channel-Token": "bad"}, b"")


def test_custom_and_unknown() -> None:
    with pytest.raises(NotImplementedError):
        verify_hmac("custom", b"s", {}, b"")
    with pytest.raises(ValueError):
        verify_hmac("rot13", b"s", {}, b"")
