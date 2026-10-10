"""Search service package (Epic 5, stories 5.1/5.3/5.6): candidate search (Boolean, faceted, NL), the
``candidate_search_docs`` projection and the header global search.

Public API: ``search_candidates``, ``global_search``, ``projection`` (``refresh_docs``, ``reindex``),
``query_parser``.
"""

from app.services.search import candidate_search, global_search, handlers, projection, query_parser
from app.services.search.candidate_search import search_candidates
from app.services.search.global_search import global_search as run_global_search

handlers.register()

__all__ = [
    "candidate_search",
    "global_search",
    "handlers",
    "projection",
    "query_parser",
    "run_global_search",
    "search_candidates",
]
