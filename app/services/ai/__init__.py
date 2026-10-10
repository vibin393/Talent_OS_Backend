"""AI gateway package (AD-6): ``app.services.ai.gateway`` is the only importer of ``anthropic``.

Callers use the public functions re-exported here (``run``, ``record_decision``, ``list_actions``, ...).
"""

from app.services.ai.gateway import (
    AIResult,
    ConsentDecision,
    get_settings_out,
    is_feature_enabled,
    list_actions,
    load_prompt,
    reassign_subject,
    record_decision,
    render_prompt,
    run,
    set_client_factory,
    set_consent_checker,
    set_subject,
    update_settings,
)

__all__ = [
    "AIResult",
    "ConsentDecision",
    "get_settings_out",
    "is_feature_enabled",
    "list_actions",
    "load_prompt",
    "reassign_subject",
    "record_decision",
    "render_prompt",
    "run",
    "set_client_factory",
    "set_consent_checker",
    "set_subject",
    "update_settings",
]

# Epic 12: processing basis, toggles, budget, AI action log administration.
from app.services.ai import admin, budget
from app.services.ai.gateway import (
    allow_all_consent_checker,
    bulk_blocked,
    features_map,
    monthly_budget,
    processing_basis,
    set_features,
    set_monthly_budget,
    set_processing_basis,
)

__all__ += [
    "admin",
    "allow_all_consent_checker",
    "budget",
    "bulk_blocked",
    "features_map",
    "monthly_budget",
    "processing_basis",
    "set_features",
    "set_monthly_budget",
    "set_processing_basis",
]

# Epic 5: local embeddings (AD-7) and the vector index port.
from app.services.ai import embedding_store, embeddings, vector_index  # noqa: E402
from app.services.ai.embeddings import embed  # noqa: E402
from app.services.ai.vector_index import VectorIndex, get_vector_index  # noqa: E402

__all__ += ["VectorIndex", "embed", "embedding_store", "embeddings", "get_vector_index", "vector_index"]

# Epic 53 (AD-57): per-step agent calls, the scripted Fake LLM, model tiers and pricing.
from app.services.ai import fake_llm, loop, model_tiers, pricing  # noqa: E402
from app.services.ai.fake_llm import FakeLLM, FakeTurn, install_fake  # noqa: E402
from app.services.ai.fake_llm import fake_llm as fake_llm_installed  # noqa: E402
from app.services.ai.loop import (  # noqa: E402
    run_agent_step,
    stream_agent_step,
    tool_result_block,
    wrap_tool_result,
)
from app.services.ai.model_tiers import model_for  # noqa: E402
from app.services.ai.pricing import cost_micros  # noqa: E402

__all__ += [
    "FakeLLM",
    "FakeTurn",
    "cost_micros",
    "fake_llm",
    "fake_llm_installed",
    "install_fake",
    "loop",
    "model_for",
    "model_tiers",
    "pricing",
    "run_agent_step",
    "stream_agent_step",
    "tool_result_block",
    "wrap_tool_result",
]
