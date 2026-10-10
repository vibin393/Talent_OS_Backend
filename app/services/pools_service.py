"""Facade of the ``talent_pools`` domain (Epic 36 contract layer, AD-39; filled by Epic 38)."""

from app.services.talent_pools.service import member_ids, pool_summary, quick_search

__all__ = ["member_ids", "pool_summary", "quick_search"]
