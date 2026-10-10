# ruff: noqa: E501
"""Pure DSC verifier (Epic 51, FR-253): proves an externally signed PDF is OUR stored render plus a valid PAdES
signature, without trusting the uploader. Verification only -- Talent OS never holds DSC private keys and there is
no signing code here (PRD 8.1). With ``connectors/dsc/*`` this is the only module importing pyHanko.

Checks, in order (each failure raises ``DscVerificationError`` with the API error code):

a. ``signed[:len(render)] == render`` (an incremental update over the stored render; sha256 compared) ->
   ``dsc_content_mismatch``;
b. at least one signature whose ``/ByteRange`` starts at 0 and covers the file at least up to the end of the
   render, in a revision after the render's last revision -> else ``dsc_signature_missing`` (no signature) /
   ``dsc_signature_invalid`` (signature not covering the original revision);
c. pyHanko reports the signature ``intact`` and ``valid`` (cryptographically). The trust chain is recorded as
   ``trusted: "unknown"`` -- there is no trust store in Phase 3 -> else ``dsc_signature_invalid``;
d. SubFilter ``ETSI.CAdES.detached`` (PAdES) or ``adbe.pkcs7.detached`` (accepted with a warning) -> else
   ``dsc_signature_invalid``.
"""

import dataclasses
import hashlib
import io
import logging
from datetime import datetime
from typing import Any

PADES = "/ETSI.CAdES.detached"
PKCS7 = "/adbe.pkcs7.detached"

# pyHanko logs the (expected) missing trust path at WARNING with a traceback; we record trust as "unknown".
for _name in ("pyhanko", "pyhanko_certvalidator"):
    logging.getLogger(_name).setLevel(logging.CRITICAL)


class DscVerificationError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclasses.dataclass(frozen=True, slots=True)
class DscVerification:
    intact: bool
    valid: bool
    trusted: str  # always "unknown" in Phase 3
    sub_filter: str | None
    signer_subject: str | None
    signer_serial: str | None
    signing_time: datetime | None
    render_sha256: str
    signed_sha256: str
    warnings: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "intact": self.intact,
            "valid": self.valid,
            "trusted": self.trusted,
            "sub_filter": self.sub_filter,
            "signer_subject": self.signer_subject,
            "signer_serial": self.signer_serial,
            "signing_time": self.signing_time.isoformat() if self.signing_time else None,
            "render_sha256": self.render_sha256,
            "signed_sha256": self.signed_sha256,
            "warnings": list(self.warnings),
        }


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_incremental_over(render: bytes, signed: bytes) -> bool:
    """Check (a): the signed file starts with exactly the stored render bytes."""
    return len(signed) > len(render) and sha256(signed[: len(render)]) == sha256(render)


def _revisions(data: bytes) -> int:
    from pyhanko.pdf_utils.reader import PdfFileReader

    return int(PdfFileReader(io.BytesIO(data), strict=False).xrefs.total_revisions)


def verify(render: bytes, signed: bytes) -> DscVerification:
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    if not is_incremental_over(render, signed):
        raise DscVerificationError(
            "dsc_content_mismatch", "The uploaded PDF is not the stored invoice PDF with a signature appended"
        )
    try:
        render_last_rev = _revisions(render) - 1
        reader = PdfFileReader(io.BytesIO(signed), strict=False)
        signatures = list(reader.embedded_signatures)
    except Exception as exc:  # unreadable PDF structure after the render bytes
        if b"/ByteRange" not in signed[len(render) :]:
            raise DscVerificationError(
                "dsc_signature_missing", "The uploaded PDF carries no digital signature"
            ) from exc
        raise DscVerificationError("dsc_signature_invalid", "The signed PDF could not be read") from exc
    if not signatures:
        raise DscVerificationError("dsc_signature_missing", "The uploaded PDF carries no digital signature")

    warnings: list[str] = []
    best: tuple[Any, Any, str] | None = None
    reasons: list[str] = []
    for sig in signatures:
        try:
            byte_range = [int(x) for x in sig.sig_object["/ByteRange"]]
            covered_end = byte_range[2] + byte_range[3]
            if byte_range[0] != 0 or covered_end < len(render) or int(sig.signed_revision) <= render_last_rev:
                reasons.append("signature does not cover the original revision")
                continue
            sub_filter = str(sig.sig_object.get("/SubFilter") or "")
            if sub_filter not in (PADES, PKCS7):
                reasons.append(f"unsupported SubFilter {sub_filter or '-'}")
                continue
            status = validate_pdf_signature(sig, signer_validation_context=ValidationContext(trust_roots=[]))
            if not (status.intact and status.valid):
                reasons.append("signature is broken or the signed bytes changed")
                continue
            best = (sig, status, sub_filter)
            break
        except Exception as exc:  # malformed signature dictionary / CMS blob
            reasons.append(f"signature could not be validated ({type(exc).__name__})")
    if best is None:
        raise DscVerificationError(
            "dsc_signature_invalid", "; ".join(dict.fromkeys(reasons)) or "The signature is not valid"
        )
    _sig, status, sub_filter = best
    if sub_filter == PKCS7:
        warnings.append("adbe_pkcs7_detached")
    cert = status.signing_cert
    subject = cert.subject.human_friendly if cert is not None else None
    serial = str(cert.serial_number) if cert is not None else None
    signing_time = getattr(status, "signer_reported_dt", None)
    return DscVerification(
        intact=bool(status.intact),
        valid=bool(status.valid),
        trusted="unknown",
        sub_filter=sub_filter.lstrip("/"),
        signer_subject=subject[:500] if subject else None,
        signer_serial=serial[:100] if serial else None,
        signing_time=signing_time,
        render_sha256=sha256(render),
        signed_sha256=sha256(signed),
        warnings=tuple(warnings),
    )
