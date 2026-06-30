"""DemoEmbedder -- deterministic 1536-dim float vectors (PRP-0066, UDR-0041).

Replaces ``app.pipeline.rag.embedder.embed_texts`` and the query-side
``_embed_query`` in ``app.rag.tools`` when ``DEMO_MODE=true``.

Design points:

- 1536 dimensions matches Azure OpenAI ``text-embedding-3-small`` so
  the existing ChromaDB collection schema is unchanged. Mixing demo
  and live vectors in the same collection is forbidden, but operators
  using ``.chroma/`` from a previous LIVE run should clear the
  directory before running demo (or use a fresh path on the cloud
  host, which is the default).
- ``hashlib.shake_128`` produces a deterministic byte stream of any
  length from any input text, so the same text always embeds to the
  same vector (idempotent ingest, predictable top-K).
- Values are normalised to the unit sphere so cosine distances behave
  sensibly in ChromaDB's default L2 / cosine spaces.

The vectors do NOT carry semantic meaning -- different texts that
share short prefixes can hash to surprisingly close vectors. The demo
RAG corpus is small enough (a single bundled PDF) that this is good
enough to show non-empty top-K results with plausible citations.
"""

from __future__ import annotations

import hashlib
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

EMBEDDING_DIM = 1536


def _hash_to_floats(text: str) -> list[float]:
    """Return EMBEDDING_DIM floats in roughly [-1.0, 1.0] derived from ``text``."""
    # shake_128 lets us request EMBEDDING_DIM * 2 bytes deterministically.
    raw = hashlib.shake_128(text.encode("utf-8")).digest(EMBEDDING_DIM * 2)
    # Interpret consecutive byte pairs as little-endian unsigned 16-bit
    # values, then centre and scale to ~[-1.0, 1.0].
    floats: list[float] = []
    for i in range(0, len(raw), 2):
        word = raw[i] | (raw[i + 1] << 8)
        # 0..65535 -> -1.0..1.0
        floats.append((word - 32768) / 32768.0)
    return floats


def _l2_normalise(vec: list[float]) -> list[float]:
    """Project a vector onto the unit sphere (cosine-friendly)."""
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def embed_demo(text: str) -> list[float]:
    """Embed a single text to a deterministic 1536-dim unit vector."""
    return _l2_normalise(_hash_to_floats(text))


def embed_demo_batch(texts: Iterable[str]) -> list[list[float]]:
    """Embed a list of texts. Empty input returns an empty list."""
    return [embed_demo(t) for t in texts]


__all__ = ["EMBEDDING_DIM", "embed_demo", "embed_demo_batch"]
