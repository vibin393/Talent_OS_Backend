"""Model tiers (Epic 53, AD-57; coordinator decision 7). Agents name a tier, never a model id.

* ``fast`` -> Haiku (classification and extraction steps: injection pre-screen, intake / answer extraction);
* ``standard`` -> Sonnet (agent loops and the Copilot);
* ``deep`` -> Opus, only when the tenant opted in (``ai_tenant_settings.deep_tier_opt_in``); else Sonnet.
"""

from app.core.settings import get_settings
from app.schemas.agentic import ModelTier


def model_for(tier: ModelTier, *, deep_opt_in: bool) -> str:
    s = get_settings()
    if tier == "fast":
        return s.ai_model_fast
    if tier == "deep" and deep_opt_in:
        return s.ai_model_deep
    return s.ai_model_standard


__all__ = ["model_for"]
