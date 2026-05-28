"""Demo Mode bootstrap (PRP-0066, UDR-0041 D6).

Auto-seeds the bundled demo RAG corpus into ChromaDB at FastAPI
lifespan startup so a reviewer's first ``rag_search`` click returns
non-trivial citations -- without requiring them to first upload a PDF
and wait for a batch ingest job. Idempotent: skipped when the
collection is non-empty.

The seeding pipeline reuses the same chunker that the live RAG ingest
job uses (``app.mcp_batch.rag.chunker.chunk_pages``) and the
``DemoEmbedder`` so the demo and live code paths share their parsing
/ chunking surface.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_ASSET_PDF = Path(__file__).resolve().parent / "assets" / "demo_rag_corpus.pdf"


def _seed_collection_sync() -> dict[str, Any]:
    """Synchronous seed implementation -- called inside ``asyncio.to_thread``."""
    import chromadb

    from app.core.config import settings
    from app.demo.embedder import embed_demo_batch
    from app.mcp_batch.rag.chunker import chunk_pages
    from app.mcp_batch.rag.pdf_parser import extract_pages

    if not _ASSET_PDF.is_file():
        logger.warning("Demo RAG corpus missing at %s; seeding skipped.", _ASSET_PDF)
        return {"skipped": True, "reason": "asset_missing"}

    client = chromadb.PersistentClient(path=settings.chroma_dir)
    collection = client.get_or_create_collection(name=settings.rag_collection_name)

    if collection.count() > 0:
        logger.info(
            "Demo RAG corpus seeding skipped: collection '%s' already has %d chunks.",
            settings.rag_collection_name,
            collection.count(),
        )
        return {"skipped": True, "reason": "non_empty", "count": collection.count()}

    pages = extract_pages(_ASSET_PDF)
    if not pages:
        logger.warning("Demo RAG corpus PDF has no extractable text; seeding skipped.")
        return {"skipped": True, "reason": "no_text"}

    records = chunk_pages(pages, chunk_size=600, chunk_overlap=120, chunk_min_size=120)
    if not records:
        logger.warning("Demo RAG corpus produced 0 chunks; seeding skipped.")
        return {"skipped": True, "reason": "no_chunks"}

    texts = [r["text"] for r in records]
    ids = [r["chunk_id"] for r in records]
    metadatas = [
        {
            "source": r["source"],
            "page": r["page"],
            "chunk_index": r["chunk_index"],
            "demo": True,
        }
        for r in records
    ]
    embeddings = embed_demo_batch(texts)

    collection.upsert(
        ids=ids,
        documents=texts,
        embeddings=embeddings,
        metadatas=metadatas,
    )

    logger.info(
        "Demo RAG corpus seeded into '%s': %d chunks from %d pages.",
        settings.rag_collection_name,
        len(records),
        len(pages),
    )
    return {"skipped": False, "chunks": len(records), "pages": len(pages)}


async def seed_rag_corpus_if_needed() -> dict[str, Any]:
    """Public async entry point called from FastAPI lifespan."""
    import asyncio

    try:
        return await asyncio.to_thread(_seed_collection_sync)
    except Exception:
        # Demo seeding must never block startup (UDR-0041 D6 spirit).
        logger.exception("Demo RAG corpus seeding failed; demo will run with an empty collection.")
        return {"skipped": True, "reason": "exception"}


__all__ = ["seed_rag_corpus_if_needed"]
