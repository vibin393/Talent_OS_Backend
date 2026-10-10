"""SSO test fixtures (Epic 14): a fake OIDC provider (local JWKS behind ``httpx.MockTransport``), signed SAML
responses (self-signed IdP certificate) and helpers to configure an IdP through the admin API."""

import base64
import dataclasses
import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from joserfc import jwt
from joserfc.jwk import RSAKey

from app.services.sso import oidc
from tests.modules.identity.conftest import Member, Org, csrf, make_tenant, other_org  # noqa: F401
from tests.modules.identity.conftest import join as join_tenant

API = "/api/v1"
ISSUER = "https://idp.example.test"
CLIENT_ID = "talent-os-client"
DOMAIN = "initech.example.com"


# ================================================================ fake OIDC provider
@dataclasses.dataclass
class FakeIdp:
    key: RSAKey
    claims: dict[str, Any] = dataclasses.field(default_factory=dict)
    last_token_form: dict[str, list[str]] = dataclasses.field(default_factory=dict)
    nonce_override: str | None = None

    def id_token(self, nonce: str) -> str:
        now = int(time.time())
        payload = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": self.nonce_override or nonce,
            **self.claims,
        }
        return jwt.encode({"alg": "RS256", "kid": "k1"}, payload, self.key)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if url == f"{ISSUER}/jwks":
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if url == f"{ISSUER}/token":
            form = parse_qs(request.content.decode())
            self.last_token_form = form
            nonce = form.get("code", [""])[0]  # tests pass the nonce as the "code"
            return httpx.Response(
                200, json={"access_token": "at", "token_type": "Bearer", "id_token": self.id_token(nonce)}
            )
        return httpx.Response(404)


@pytest.fixture
def fake_idp() -> Iterator[FakeIdp]:
    idp = FakeIdp(key=RSAKey.generate_key(2048, parameters={"kid": "k1"}))
    oidc.set_http_client_factory(lambda: httpx.AsyncClient(transport=httpx.MockTransport(idp.handler)))
    yield idp
    oidc.set_http_client_factory(None)


# ================================================================ tenants
@pytest.fixture
async def sso_org(app: FastAPI) -> AsyncIterator[Org]:
    tenant = await make_tenant(seat_limit=20)
    admin = await join_tenant(app, tenant, name="Ada Admin")
    yield Org(tenant=tenant, admin=admin)
    await admin.client.aclose()


async def create_oidc_idp(admin: Member, **extra: Any) -> dict[str, Any]:
    body = {
        "name": f"Okta {uuid.uuid4().hex[:4]}",
        "kind": "oidc",
        "email_domains": [DOMAIN],
        "oidc": {"issuer": ISSUER, "client_id": CLIENT_ID},
        "client_secret": "s3cret",
        **extra,
    }
    resp = await admin.client.post(f"{API}/sso/identity-providers", json=body, headers=csrf(admin.client))
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def activate(admin: Member, idp_id: str, **extra: Any) -> httpx.Response:
    return await admin.client.patch(
        f"{API}/sso/identity-providers/{idp_id}",
        json={"status": "active", **extra},
        headers=csrf(admin.client),
    )


async def oidc_sign_in(
    app: FastAPI,
    idp: FakeIdp,
    slug: str,
    idp_id: str,
    claims: dict[str, Any],
    *,
    next_path: str = "/candidates",
) -> tuple[httpx.AsyncClient, httpx.Response]:
    """Run start -> (fake IdP) -> callback in a fresh browser; returns (client, callback response)."""
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    start = await client.get(f"{API}/auth/sso/{idp_id}/start", params={"tenant": slug, "next": next_path})
    assert start.status_code == 302, start.text
    query = parse_qs(urlsplit(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    idp.claims = claims
    callback = await client.get(
        f"{API}/auth/sso/oidc/callback",
        params={"code": query["nonce"][0], "state": query["state"][0]},
        headers={"Accept": "application/json"},
    )
    return client, callback


# ================================================================ SAML
@dataclasses.dataclass
class SamlIdp:
    key_pem: str
    cert_pem: str
    entity_id: str = "https://saml-idp.example.test/entity"
    sso_url: str = "https://saml-idp.example.test/sso"


@pytest.fixture(scope="session")
def saml_idp() -> SamlIdp:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "saml-idp.example.test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return SamlIdp(
        key_pem=key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
        cert_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
    )


def _ts(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def saml_response(
    idp: SamlIdp,
    *,
    acs: str,
    audience: str,
    request_id: str,
    email: str,
    groups: tuple[str, ...] = (),
    sign: bool = True,
    expired: bool = False,
) -> str:
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    now = datetime.now(UTC)
    later = now - timedelta(minutes=5) if expired else now + timedelta(minutes=5)
    attrs = "".join(
        f'<saml:AttributeValue xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="xs:string">{g}</saml:AttributeValue>'  # noqa: E501
        for g in groups
    )
    group_attribute = f'<saml:Attribute Name="groups">{attrs}</saml:Attribute>' if groups else ""
    attribute_statement = (
        '<saml:AttributeStatement><saml:Attribute Name="email">'
        f'<saml:AttributeValue xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:type="xs:string">{email}</saml:AttributeValue>'  # noqa: E501
        f"</saml:Attribute>{group_attribute}</saml:AttributeStatement>"
    )
    assertion_id = f"_a{uuid.uuid4().hex}"
    assertion = (
        f'<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'xmlns:xs="http://www.w3.org/2001/XMLSchema" ID="{assertion_id}" Version="2.0" IssueInstant="{_ts(now)}">'  # noqa: E501
        f"<saml:Issuer>{idp.entity_id}</saml:Issuer>"
        "<saml:Subject>"
        f'<saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{email}</saml:NameID>'
        '<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">'
        f'<saml:SubjectConfirmationData NotOnOrAfter="{_ts(later)}" Recipient="{acs}" InResponseTo="{request_id}"/>'  # noqa: E501
        "</saml:SubjectConfirmation></saml:Subject>"
        f'<saml:Conditions NotBefore="{_ts(now - timedelta(minutes=5))}" NotOnOrAfter="{_ts(later)}">'
        f"<saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction>"
        "</saml:Conditions>"
        f'<saml:AuthnStatement AuthnInstant="{_ts(now)}" SessionIndex="_s1"><saml:AuthnContext>'
        "<saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:Password</saml:AuthnContextClassRef>"
        "</saml:AuthnContext></saml:AuthnStatement>"
        f"{attribute_statement}"
        "</saml:Assertion>"
    )
    if sign:
        signed = OneLogin_Saml2_Utils.add_sign(assertion, idp.key_pem, idp.cert_pem)
        assertion = signed.decode() if isinstance(signed, bytes) else str(signed)
        if assertion.startswith("<?xml"):
            assertion = assertion.split("?>", 1)[1]
    response = (
        '<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        f'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_r{uuid.uuid4().hex}" Version="2.0" '
        f'IssueInstant="{_ts(now)}" Destination="{acs}" InResponseTo="{request_id}">'
        f"<saml:Issuer>{idp.entity_id}</saml:Issuer>"
        '<samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
        f"{assertion}</samlp:Response>"
    )
    return base64.b64encode(response.encode()).decode()


def saml_request_id(location: str) -> str:
    """Extract the AuthnRequest ID from the HTTP-Redirect URL (deflated, base64)."""
    import zlib

    raw = parse_qs(urlsplit(location).query)["SAMLRequest"][0]
    xml = zlib.decompress(base64.b64decode(raw), -15).decode()
    marker = ' ID="'
    start = xml.index(marker) + len(marker)
    return xml[start : xml.index('"', start)]


def relay_state(location: str) -> str:
    return parse_qs(urlsplit(location).query)["RelayState"][0]


def dump(obj: Any) -> str:  # pragma: no cover - debugging helper
    return json.dumps(obj, indent=2, default=str)
