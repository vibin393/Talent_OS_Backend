"""Documents & eSignature (Epic 22, AD-28): templates, repository, signature requests, certificates, verify.

Modules: ``templates``, ``merge``, ``render`` (the one renderer), ``repository``, ``esign`` (requests and the
signing ceremony), ``certificate``, ``verify``, ``portal`` (Worker), ``service`` (public API), ``hooks``.
"""

from app.services.documents import service
from app.services.documents.merge import MERGE_CATALOGUE, register_merge_source
from app.services.documents.service import (
    add_version,
    create_document,
    create_signature_request,
    get_signature_request,
    list_documents,
    render_pdf,
    resolve_template,
)

__all__ = [
    "MERGE_CATALOGUE",
    "add_version",
    "create_document",
    "create_signature_request",
    "get_signature_request",
    "list_documents",
    "register_merge_source",
    "render_pdf",
    "resolve_template",
    "service",
]
