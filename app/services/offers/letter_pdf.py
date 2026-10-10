"""Offer letter PDF (story 8.1). Since Epic 22 the renderer lives in ``app/services/documents/render.py`` (the
one renderer, AD-28); this module keeps its public names as thin re-exports so P1 callers and tests are
unchanged.

``render(markdown, ...)`` renders a letter with the tenant name as page header and the footer
``Offer {OFR-...} · version {n} · page x of y``. ``acceptance_certificate(...)`` delegates to
``documents.certificate`` (offers accepted before Epic 22; newer acceptances get the signature certificate).
"""

from app.services.documents.certificate import acceptance_certificate
from app.services.documents.render import (
    _build,
    _cells,
    _numbered_canvas,
    _styles,
    append_pages,
    inline,
    markdown_flowables,
    pdf_text,
)


def render(markdown: str, *, tenant_name: str, offer_ref: str, version_no: int) -> bytes:
    """Render the letter; returns PDF bytes."""
    footer = f"Offer {offer_ref} · version {version_no}"
    return _build(
        markdown_flowables(markdown),
        header=tenant_name,
        footer_prefix=footer,
        title=f"Offer {offer_ref} v{version_no}",
    )


__all__ = [
    "_build",
    "_cells",
    "_numbered_canvas",
    "_styles",
    "acceptance_certificate",
    "append_pages",
    "inline",
    "markdown_flowables",
    "pdf_text",
    "render",
]
