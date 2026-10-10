"""SAML 2.0 service provider (Epic 14, AD-30) on python3-saml: SP-initiated AuthnRequest (HTTP-Redirect),
assertions over HTTP-POST. Signed assertions are mandatory (``wantAssertionsSigned`` forced), the audience is
our SP entity id, the recipient / destination our ACS URL, ``InResponseTo`` the stored AuthnRequest id.

Only this package imports ``onelogin.saml2``. Assertions are never logged.
"""

import dataclasses
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from app.core.errors import Unauthorized

BINDING_POST = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
BINDING_REDIRECT = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
NAMEID_EMAIL = "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress"


def invalid(reason: str) -> Unauthorized:
    return Unauthorized("Single sign-on failed", code="sso_assertion_invalid", details={"reason": reason})


@dataclasses.dataclass(frozen=True, slots=True)
class SamlIdentity:
    subject: str
    email: str | None
    name: str | None
    groups: list[str]


def _clean_cert(cert: str) -> str:
    body = cert.replace("-----BEGIN CERTIFICATE-----", "").replace("-----END CERTIFICATE-----", "")
    return "".join(body.split())


def settings(config: Mapping[str, Any], *, sp_entity_id: str, acs_url: str) -> dict[str, Any]:
    certs = [_clean_cert(c) for c in config.get("x509_certs") or []]
    return {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": sp_entity_id,
            "assertionConsumerService": {"url": acs_url, "binding": BINDING_POST},
            "NameIDFormat": NAMEID_EMAIL,
            "x509cert": "",
            "privateKey": "",
        },
        "idp": {
            "entityId": config["idp_entity_id"],
            "singleSignOnService": {"url": config["sso_url"], "binding": BINDING_REDIRECT},
            "x509certMulti": {"signing": certs},
        },
        "security": {
            "authnRequestsSigned": False,
            "wantAssertionsSigned": True,  # forced (AD-30)
            "wantMessagesSigned": False,
            "wantNameId": True,
            "requestedAuthnContext": False,
            "rejectDeprecatedAlgorithm": True,
        },
    }


def _request_data(
    url: str, *, get: Mapping[str, str] | None = None, post: Mapping[str, str] | None = None
) -> dict[str, Any]:
    parts = urlsplit(url)
    https = parts.scheme == "https"
    port = parts.port or (443 if https else 80)
    return {
        "https": "on" if https else "off",
        "http_host": parts.hostname or "",
        "server_port": str(port),
        "script_name": parts.path,
        "get_data": dict(get or {}),
        "post_data": dict(post or {}),
    }


def _auth(cfg: dict[str, Any], request_data: dict[str, Any]) -> Any:
    from onelogin.saml2.auth import OneLogin_Saml2_Auth

    return OneLogin_Saml2_Auth(request_data, old_settings=cfg)


def login_url(
    config: Mapping[str, Any], *, sp_entity_id: str, acs_url: str, relay_state: str
) -> tuple[str, str]:
    """``(redirect URL, AuthnRequest ID)``."""
    auth = _auth(settings(config, sp_entity_id=sp_entity_id, acs_url=acs_url), _request_data(acs_url))
    url = auth.login(return_to=relay_state)
    return str(url), str(auth.get_last_request_id())


def metadata_xml(config: Mapping[str, Any], *, sp_entity_id: str, acs_url: str) -> str:
    from onelogin.saml2.settings import OneLogin_Saml2_Settings

    cfg = settings(config, sp_entity_id=sp_entity_id, acs_url=acs_url)
    sp_settings = OneLogin_Saml2_Settings(cfg, sp_validation_only=True)
    meta = sp_settings.get_sp_metadata()
    return meta.decode() if isinstance(meta, bytes) else str(meta)


def process_response(
    config: Mapping[str, Any],
    *,
    sp_entity_id: str,
    acs_url: str,
    saml_response: str,
    relay_state: str,
    request_id: str,
    group_attribute: str | None,
) -> SamlIdentity:
    cfg = settings(config, sp_entity_id=sp_entity_id, acs_url=acs_url)
    auth = _auth(cfg, _request_data(acs_url, post={"SAMLResponse": saml_response, "RelayState": relay_state}))
    try:
        auth.process_response(request_id=request_id)
    except Exception as exc:  # malformed XML, bad base64, ...
        raise invalid("malformed") from exc
    if auth.get_errors() or not auth.is_authenticated():
        raise invalid(str(auth.get_last_error_reason() or "rejected")[:200])
    subject = auth.get_nameid()
    if not subject:
        raise invalid("subject")
    attributes: dict[str, list[str]] = auth.get_attributes() or {}

    def first(name: str | None) -> str | None:
        if not name:
            return None
        values = attributes.get(name) or []
        return str(values[0]) if values else None

    email = first(config.get("email_attribute")) or (subject if "@" in subject else None)
    groups = [str(g) for g in attributes.get(group_attribute or "", [])] if group_attribute else []
    return SamlIdentity(
        subject=str(subject), email=email, name=first(config.get("name_attribute")), groups=groups
    )


# ============================================================ Epic 50: metadata import, certificate checks
def certificate_not_after(cert: str) -> datetime:
    """Expiry of a PEM / bare base64 X.509 certificate; ``ValueError`` when it does not parse."""
    from cryptography import x509

    body = _clean_cert(cert)
    pem = "-----BEGIN CERTIFICATE-----\n" + "\n".join(body[i : i + 64] for i in range(0, len(body), 64))
    pem += "\n-----END CERTIFICATE-----\n"
    try:
        parsed = x509.load_pem_x509_certificate(pem.encode())
    except Exception as exc:
        raise ValueError("invalid certificate") from exc
    return parsed.not_valid_after_utc


def parse_metadata(xml: str) -> dict[str, Any]:
    """IdP metadata (``IdPMetadataParser``) -> our SAML config fields; ``ValueError`` when unusable."""
    from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser

    try:
        parsed = OneLogin_Saml2_IdPMetadataParser.parse(xml, required_sso_binding=BINDING_REDIRECT)
    except Exception as exc:  # malformed XML, XXE refused, ...
        raise ValueError("malformed metadata") from exc
    idp = parsed.get("idp") or {}
    certs = list((idp.get("x509certMulti") or {}).get("signing") or [])
    if not certs and idp.get("x509cert"):
        certs = [idp["x509cert"]]
    sso_url = (idp.get("singleSignOnService") or {}).get("url")
    if not idp.get("entityId") or not sso_url or not certs:
        raise ValueError("incomplete metadata")
    return {
        "idp_entity_id": str(idp["entityId"]),
        "sso_url": str(sso_url),
        "x509_certs": [str(c) for c in certs],
    }


async def fetch_metadata(url: str) -> str:
    """Download IdP metadata (https only) through the package's HTTP client factory (tests swap it)."""
    from app.services.sso import oidc

    if urlsplit(url).scheme != "https":
        raise ValueError("https required")
    async with oidc.http_client() as client:
        resp = await client.get(url)
    if resp.status_code != 200 or len(resp.content) > 500_000:
        raise ValueError("metadata unreachable")
    return resp.text
