"""Semantic layer catalogue and validation (Epic 36 story 36.2): ``/semantic/*``.

``GET /semantic/sources``, ``GET /semantic/sources/{key}`` (fields the caller may use) and
``POST /semantic/validate`` (no execution). Internal principals with ``candidate:read`` or ``report:read``
only; execution endpoints belong to the segment / report owners (Epics 38, 47).
"""

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Path

from app.core.permissions import Principal, require_any
from app.core.semantic import SemanticError, catalogue_for, source_catalogue, sources, validate
from app.schemas.semantic import SemanticErrorOut, SourceCatalogOut, ValidateOut

router = APIRouter(prefix="/semantic", tags=["semantic"])
SemanticReader = Annotated[Principal, Depends(require_any("candidate:read", "report:read"))]


@router.get("/sources", response_model=list[SourceCatalogOut])
async def list_sources(principal: SemanticReader) -> list[SourceCatalogOut]:
    return catalogue_for(principal)


@router.get("/sources/{key}", response_model=SourceCatalogOut)
async def get_source_catalogue(
    principal: SemanticReader, key: Annotated[str, Path(max_length=100)]
) -> SourceCatalogOut:
    return source_catalogue(principal, key)


@router.post("/validate", response_model=ValidateOut)
async def validate_definition(
    principal: SemanticReader, body: Annotated[dict[str, Any], Body()]
) -> ValidateOut:
    src = sources().get(body.get("source")) if isinstance(body.get("source"), str) else None  # type: ignore[arg-type]
    if src is not None and not principal.has(src.scope.permission):
        error = SemanticErrorOut(code="semantic_unknown_source", message="Unknown source", path="source")
        return ValidateOut(valid=False, errors=[error])
    try:
        validate(body, principal=principal)
    except SemanticError as exc:
        details = {k: v for k, v in exc.details.items() if k != "path"}
        error = SemanticErrorOut(code=exc.code, message=exc.message, path=exc.path, details=details)
        return ValidateOut(valid=False, errors=[error])
    return ValidateOut(valid=True, errors=[])
