# ruff: file-ignore[line-too-long]
"""Pure item draw and per-attempt shuffling (Epic 40 A4, NFR-35). No I/O.

Seeded by a SHA-256 of the invitation id (and attempt number), so a draw is deterministic per invitation and
differs across invitations. ``draw`` picks ``count`` items from a frozen pool, stratified by difficulty across the
rule's ``[min, max]`` range (round-robin over the difficulty levels present), so sets are equivalent but differ per
Candidate. Option / line ids delivered to the Candidate are opaque per attempt.
"""

import hashlib
import random
import uuid
from collections.abc import Sequence


def seed_for(invitation_id: uuid.UUID, salt: str = "") -> int:
    digest = hashlib.sha256(f"{invitation_id}:{salt}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def rng_for(invitation_id: uuid.UUID, salt: str = "") -> random.Random:
    return random.Random(seed_for(invitation_id, salt))  # nosec B311 - non-cryptographic sampling/jitter


def draw(
    pool: Sequence[tuple[str, int]], count: int, difficulty: tuple[int, int], rng: random.Random
) -> list[str]:
    """``pool`` = ``(item_version_id, difficulty)`` pairs (frozen at publish). Returns ``count`` ids (fewer only
    when the pool is smaller, which publish refuses)."""
    lo, hi = min(difficulty), max(difficulty)
    levels: dict[int, list[str]] = {}
    for vid, diff in sorted(pool):
        if lo <= diff <= hi:
            levels.setdefault(diff, []).append(vid)
    for ids in levels.values():
        rng.shuffle(ids)
    order = sorted(levels)
    picked: list[str] = []
    while len(picked) < count and any(levels[d] for d in order):
        for d in order:
            if levels[d] and len(picked) < count:
                picked.append(levels[d].pop())
    return picked


def shuffled[T](items: Sequence[T], rng: random.Random) -> list[T]:
    out = list(items)
    rng.shuffle(out)
    return out


def opaque_ids(n: int, rng: random.Random, prefix: str = "o") -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    while len(out) < n:
        candidate = f"{prefix}{rng.getrandbits(40):010x}"
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


__all__ = ["draw", "opaque_ids", "rng_for", "seed_for", "shuffled"]
