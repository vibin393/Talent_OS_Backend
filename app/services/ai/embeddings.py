"""Local text embeddings (Epic 5, AD-7). The only backend module that imports ``sentence_transformers``.

* ``EMBEDDING_BACKEND=local``: a lazy, thread-safe singleton ``SentenceTransformer(EMBEDDING_MODEL,
  device="cpu")`` (default ``BAAI/bge-small-en-v1.5``, 384-dim), loaded on first use. Candidate text never
  leaves the platform.
* ``EMBEDDING_BACKEND=fake``: deterministic hash-seeded unit vectors (tests / CI; no model download). Each
  token contributes a vector seeded by its hash, so texts sharing words are similar -- enough to exercise
  semantic ranking in tests.

``embed(texts, kind)`` returns L2-normalised 384-dim vectors; queries get the bge retrieval prefix.
Embedding text is built from **structured fields only** (``candidate_text`` / ``demand_text``): no raw resume
text, no name or contact data, no protected attributes.
"""

import hashlib
import re
import threading
from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Literal

import numpy as np
from loguru import logger

from app.core.settings import get_settings
from app.schemas.matching_profiles import CandidateMatchProfile, DemandMatchProfile

QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
BATCH_SIZE = 32
FAKE_MODEL = "fake-hash-v1"

Kind = Literal["passage", "query"]

_lock = threading.Lock()
_model: Any = None
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def model_name() -> str:
    s = get_settings()
    return FAKE_MODEL if s.embedding_backend == "fake" else s.embedding_model


def dimension() -> int:
    return int(get_settings().embedding_dim)


def _load_model() -> Any:
    global _model  # process-wide lazy singleton (thread-safe, double-checked)
    if _model is None:
        with _lock:
            if _model is None:
                from sentence_transformers import SentenceTransformer

                name = get_settings().embedding_model
                logger.info("Loading embedding model {} on CPU", name)
                _model = SentenceTransformer(name, device="cpu")
    return _model


def _fake_one(text: str, dim: int) -> np.ndarray:
    tokens = _TOKEN_RE.findall(text.lower()) or [""]
    acc = np.zeros(dim, dtype=np.float64)
    for token in tokens:
        seed = int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "little")
        acc += np.random.default_rng(seed).standard_normal(dim)
    norm = float(np.linalg.norm(acc))
    return acc / norm if norm > 0 else acc


def embed(texts: Sequence[str], kind: Kind = "passage") -> list[list[float]]:
    """L2-normalised embeddings, one per text (batches of 32)."""
    if not texts:
        return []
    s = get_settings()
    dim = int(s.embedding_dim)
    inputs = [(QUERY_PREFIX + t) if kind == "query" else t for t in texts]
    if s.embedding_backend == "fake":
        return [_fake_one(t, dim).astype(np.float32).tolist() for t in inputs]
    model = _load_model()
    vectors = model.encode(
        list(inputs),
        batch_size=BATCH_SIZE,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return [np.asarray(v, dtype=np.float32).tolist() for v in vectors]


def content_hash(text: str) -> str:
    return hashlib.sha256(f"{model_name()}\0{text}".encode()).hexdigest()


# ================================================================ embedding text (structured fields only)
def _yrs(value: Decimal | None) -> str:
    if value is None:
        return ""
    return f" ({value.normalize():f} yrs)"


def candidate_text(profile: CandidateMatchProfile) -> str:
    parts: list[str] = []
    if profile.current_title:
        parts.append(f"Title: {profile.current_title}.")
    if profile.skills:
        parts.append("Skills: " + ", ".join(f"{s.name}{_yrs(s.years)}" for s in profile.skills[:40]) + ".")
    if profile.experience_years is not None:
        parts.append(f"Experience: {profile.experience_years.normalize():f} years.")
    if profile.role_titles:
        parts.append("Roles: " + ", ".join(profile.role_titles[:10]) + ".")
    if profile.domains:
        parts.append("Domains: " + ", ".join(profile.domains[:10]) + ".")
    if profile.certifications:
        parts.append("Certifications: " + ", ".join(profile.certifications[:10]) + ".")
    return " ".join(parts)


def demand_text(profile: DemandMatchProfile) -> str:
    parts = [f"Title: {profile.title}."]
    if profile.skills:
        rendered = []
        for s in profile.skills[:40]:
            extra = " (mandatory" if s.mandatory else " (nice to have"
            if s.min_years is not None:
                extra += f", {s.min_years.normalize():f}+ yrs"
            rendered.append(f"{s.label}{extra})")
        parts.append("Skills: " + ", ".join(rendered) + ".")
    if profile.experience_min is not None or profile.experience_max is not None:
        lo = f"{profile.experience_min.normalize():f}" if profile.experience_min is not None else "0"
        hi = f"{profile.experience_max.normalize():f}" if profile.experience_max is not None else "+"
        parts.append(f"Experience: {lo}-{hi} years.")
    if profile.domains:
        parts.append("Domain: " + ", ".join(profile.domains) + ".")
    if profile.summary:
        parts.append(f"Summary: {profile.summary}")
    return " ".join(parts)


def set_model_for_tests(model: Any) -> None:
    """Inject a stand-in model object (``encode``) -- tests only."""
    global _model  # test hook
    _model = model
