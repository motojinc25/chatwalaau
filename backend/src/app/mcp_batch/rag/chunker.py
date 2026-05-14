"""Overlap-based text chunking for RAG pipeline (CTR-0076, PRP-0037, PRP-0047).

Splits text into overlapping character-based chunks for embedding. A final
trailing chunk shorter than ``chunk_min_size`` is merged into the previous
chunk so that tiny fragments (e.g. "B." on a page boundary) do not reach
the embedding API and dilute top-k retrieval quality.

Cross-page merging is intentionally NOT performed: source/page metadata is
part of the citation surface, and mixing pages inside a single chunk would
corrupt the chunk_id derivation (CTR-0076).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Absolute floor for the resolved minimum chunk size regardless of the
# ``chunk_size`` requested by the caller. Keeps the minimum useful even
# when an operator sets an unusually small chunk_size.
_ABSOLUTE_MIN_CHUNK_SIZE = 32


def _resolve_min_chunk_size(chunk_size: int, chunk_min_size: int | None) -> int:
    """Resolve the effective minimum chunk size.

    - ``None`` (not configured): default to ``chunk_size // 4`` floored
      at ``_ABSOLUTE_MIN_CHUNK_SIZE``.
    - ``0``: explicitly disable trailing merge (backward compat opt-out).
    - Any positive integer: clamp to ``chunk_size - 1`` so a single chunk
      always satisfies the invariant.
    """
    if chunk_min_size is None:
        return max(_ABSOLUTE_MIN_CHUNK_SIZE, chunk_size // 4)
    if chunk_min_size <= 0:
        return 0
    return min(chunk_min_size, max(1, chunk_size - 1))


def chunk_text(
    text: str,
    chunk_size: int = 800,
    chunk_overlap: int = 200,
    chunk_min_size: int | None = None,
) -> list[str]:
    """Split text into overlapping chunks by character count.

    Args:
        text: Input text to chunk.
        chunk_size: Maximum characters per chunk.
        chunk_overlap: Overlap characters between consecutive chunks.
        chunk_min_size: Minimum length for a trailing chunk. If the final
            chunk is strictly shorter than this and a previous chunk
            exists in the same call, the final chunk is appended to the
            previous chunk (after stripping the overlap region) rather
            than returned as its own record. ``None`` resolves to
            ``chunk_size // 4`` (floor 32). ``0`` disables the merge.

    Returns:
        List of non-empty text chunks.
    """
    if not text or not text.strip():
        return []

    if chunk_size <= 0:
        chunk_size = 800
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        chunk_overlap = min(200, chunk_size // 4)

    min_size = _resolve_min_chunk_size(chunk_size, chunk_min_size)

    chunks: list[str] = []
    start = 0
    text_len = len(text)

    while start < text_len:
        end = start + chunk_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        # Advance by (chunk_size - overlap) to create overlapping windows
        start += chunk_size - chunk_overlap

    # Trailing-chunk merge (PRP-0047). If the last chunk is below the
    # minimum size, fold it into the previous chunk. Two cases:
    #
    # 1. The tail is shorter than ``chunk_overlap`` characters. Because
    #    consecutive windows share the last ``chunk_overlap`` chars of
    #    the previous window, the tail's content is already fully
    #    contained in ``chunks[-2]``; the tail is redundant and is
    #    dropped.
    # 2. The tail is longer than ``chunk_overlap`` but still below
    #    ``min_size``. Only ``tail[chunk_overlap:]`` is new content and
    #    is appended to the previous chunk with a single-space
    #    separator so token boundaries remain sane for embeddings.
    if min_size > 0 and len(chunks) >= 2 and len(chunks[-1]) < min_size:
        tail = chunks.pop()
        if len(tail) > chunk_overlap:
            appended = tail[chunk_overlap:]
            if appended:
                chunks[-1] = chunks[-1] + " " + appended
        # else: tail fully within overlap -> already present in chunks[-1]

    return chunks


def chunk_pages(
    pages: list[dict],
    chunk_size: int = 800,
    chunk_overlap: int = 200,
    chunk_min_size: int | None = None,
) -> list[dict]:
    """Chunk all pages and produce records with metadata.

    Args:
        pages: List of page dicts from pdf_parser.extract_pages().
        chunk_size: Characters per chunk.
        chunk_overlap: Overlap between chunks.
        chunk_min_size: Minimum trailing chunk length; see ``chunk_text``.

    Returns:
        List of dicts with keys: text, source, page, chunk_index, chunk_id.
    """
    records: list[dict] = []
    for page_info in pages:
        text = page_info["text"]
        source = page_info["source"]
        page = page_info["page"]

        chunks = chunk_text(text, chunk_size, chunk_overlap, chunk_min_size)
        for idx, chunk in enumerate(chunks):
            chunk_id = f"{source}__p{page}__c{idx}"
            records.append(
                {
                    "text": chunk,
                    "source": source,
                    "page": page,
                    "chunk_index": idx,
                    "chunk_id": chunk_id,
                }
            )

    logger.info(
        "Chunked %d pages into %d chunks (size=%d, overlap=%d, min=%s)",
        len(pages),
        len(records),
        chunk_size,
        chunk_overlap,
        chunk_min_size if chunk_min_size is not None else "auto",
    )
    return records
