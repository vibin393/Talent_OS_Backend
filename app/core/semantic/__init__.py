"""Semantic query layer (Epic 36 story 36.2, AD-42/AD-46): the only dynamic-query compiler.

Segments, talent pools, reports, eligibility and audiences describe *what* they want as a ``Definition``
over a registered source; this package validates it, applies scope and field permissions and runs it
read-only with a statement timeout. Public API = contract 0.6.
"""

from app.core.semantic.catalogue import catalogue_for, source_catalogue
from app.core.semantic.compiler import CompiledQuery, SemanticContext, compile, validate
from app.core.semantic.errors import SemanticError, SemanticRegistryError, SemanticTimeout
from app.core.semantic.executor import SemanticResult, count, execute, ids
from app.core.semantic.operators import SkillExpander, register_skill_expander
from app.core.semantic.registry import extend_semantic_source, get_source, register_semantic_source, sources
from app.core.semantic.schema import AndNode, Condition, Definition, GroupBy, Measure, Node, OrNode, SortSpec
from app.core.semantic.types import CMP, Agg, FieldDef, FieldType, JoinDef, Op, PiiClass, ScopeSpec, SourceDef
from app.schemas.semantic import ColumnMeta, SourceCatalogOut

__all__ = [
    "CMP",
    "Agg",
    "AndNode",
    "ColumnMeta",
    "CompiledQuery",
    "Condition",
    "Definition",
    "FieldDef",
    "FieldType",
    "GroupBy",
    "JoinDef",
    "Measure",
    "Node",
    "Op",
    "OrNode",
    "PiiClass",
    "ScopeSpec",
    "SemanticContext",
    "SemanticError",
    "SemanticRegistryError",
    "SemanticResult",
    "SemanticTimeout",
    "SkillExpander",
    "SortSpec",
    "SourceCatalogOut",
    "SourceDef",
    "catalogue_for",
    "compile",
    "count",
    "execute",
    "extend_semantic_source",
    "get_source",
    "ids",
    "register_semantic_source",
    "register_skill_expander",
    "source_catalogue",
    "sources",
    "validate",
]
