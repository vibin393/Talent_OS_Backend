"""Facade of the ``documents`` domain (Epic 13 contract layer; filled by Epic 22)."""

from app.services.documents import esign, render, repository, templates, verify
from app.services.documents.merge import register_merge_source
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
    "add_version",
    "create_document",
    "create_signature_request",
    "esign",
    "get_signature_request",
    "list_documents",
    "register_merge_source",
    "render",
    "render_pdf",
    "repository",
    "resolve_template",
    "templates",
    "verify",
]
