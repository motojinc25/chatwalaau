"""RAG Ingest job for Batch MCP Server (CTR-0076, PRP-0037).

Pipeline: PDF parse -> chunk -> embed -> ChromaDB store.
Supports cooperative cancellation and progress tracking.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

import chromadb

from app.mcp_batch.models import Job, JobStatus
from app.mcp_batch.rag.chunker import chunk_pages
from app.mcp_batch.rag.embedder import EMBEDDING_BATCH_SIZE, embed_texts
from app.mcp_batch.rag.pdf_parser import extract_pages

if TYPE_CHECKING:
    from app.mcp_batch.storage import JobStorage

logger = logging.getLogger(__name__)


async def run_rag_ingest_job(
    job: Job,
    storage: JobStorage,
    cancel_event: asyncio.Event,
) -> None:
    """Ingest a PDF document into ChromaDB collection.

    Pipeline stages:
      1. PDF parsing (0-10%)
      2. Chunking (10-20%)
      3. Embedding + ChromaDB storage (20-95%)
      4. Finalization (95-100%)

    Args:
        job: The job record to update.
        storage: Persistence layer for saving progress.
        cancel_event: Set by the queue to request cancellation.
    """
    file_path = Path(job.params.get("file_path", ""))
    collection_name = job.params.get("collection", os.environ.get("RAG_COLLECTION_NAME", "default"))
    chunk_size = int(job.params.get("chunk_size", os.environ.get("RAG_CHUNK_SIZE", "800")))
    chunk_overlap = int(job.params.get("chunk_overlap", os.environ.get("RAG_CHUNK_OVERLAP", "200")))
    # PRP-0047: trailing-chunk merge threshold. None -> chunker derives default.
    chunk_min_raw = job.params.get("chunk_min_size", os.environ.get("RAG_CHUNK_MIN_SIZE"))
    chunk_min_size = int(chunk_min_raw) if chunk_min_raw not in (None, "") else None
    chroma_dir = os.environ.get("CHROMA_DIR", ".chroma")

    # Validate file exists
    if not file_path.is_file():
        job.status = JobStatus.failed
        job.error = f"PDF file not found: {file_path}"
        job.completed_at = datetime.now(UTC).isoformat()
        storage.save(job)
        return

    # --- Stage 1: PDF Parsing (0-10%) ---
    job.progress = 0
    job.progress_message = "Parsing PDF..."
    storage.save(job)

    try:
        pages = await asyncio.to_thread(extract_pages, file_path)
    except Exception as e:
        job.status = JobStatus.failed
        job.error = f"Failed to parse PDF: {e}"
        job.completed_at = datetime.now(UTC).isoformat()
        storage.save(job)
        return

    if not pages:
        job.status = JobStatus.completed
        job.progress = 100
        job.progress_message = "No extractable text in PDF"
        job.result = {"chunks_total": 0, "pages_total": 0, "collection": collection_name, "source": file_path.name}
        job.completed_at = datetime.now(UTC).isoformat()
        storage.save(job)
        return

    job.progress = 10
    job.progress_message = f"Parsed {len(pages)} pages"
    storage.save(job)

    if cancel_event.is_set():
        _set_cancelled(job, storage)
        return

    # --- Stage 2: Chunking (10-20%) ---
    job.progress_message = "Chunking text..."
    storage.save(job)

    records = await asyncio.to_thread(chunk_pages, pages, chunk_size, chunk_overlap, chunk_min_size)

    if not records:
        job.status = JobStatus.completed
        job.progress = 100
        job.progress_message = "No chunks produced"
        job.result = {
            "chunks_total": 0,
            "pages_total": len(pages),
            "collection": collection_name,
            "source": file_path.name,
        }
        job.completed_at = datetime.now(UTC).isoformat()
        storage.save(job)
        return

    job.progress = 20
    job.progress_message = f"Created {len(records)} chunks"
    storage.save(job)

    if cancel_event.is_set():
        _set_cancelled(job, storage)
        return

    # --- Stage 3: Embedding + ChromaDB Storage (20-95%) ---
    job.progress_message = "Embedding and storing chunks..."
    storage.save(job)

    client = chromadb.PersistentClient(path=chroma_dir)
    collection = client.get_or_create_collection(name=collection_name)

    total_chunks = len(records)
    processed = 0
    progress_range = 75  # 20% to 95% = 75 percentage points

    for batch_start in range(0, total_chunks, EMBEDDING_BATCH_SIZE):
        if cancel_event.is_set():
            _set_cancelled(job, storage)
            return

        batch_end = min(batch_start + EMBEDDING_BATCH_SIZE, total_chunks)
        batch = records[batch_start:batch_end]

        batch_texts = [r["text"] for r in batch]
        batch_ids = [r["chunk_id"] for r in batch]
        batch_metadatas = [{"source": r["source"], "page": r["page"], "chunk_index": r["chunk_index"]} for r in batch]

        try:
            embeddings = await asyncio.to_thread(embed_texts, batch_texts)
        except Exception as e:
            job.status = JobStatus.failed
            job.error = f"Embedding failed at chunk {batch_start}: {e}"
            job.completed_at = datetime.now(UTC).isoformat()
            storage.save(job)
            return

        try:
            await asyncio.to_thread(
                collection.upsert,
                ids=batch_ids,
                documents=batch_texts,
                embeddings=embeddings,
                metadatas=batch_metadatas,
            )
        except Exception as e:
            job.status = JobStatus.failed
            job.error = f"ChromaDB storage failed at chunk {batch_start}: {e}"
            job.completed_at = datetime.now(UTC).isoformat()
            storage.save(job)
            return

        processed += len(batch)
        batch_progress = int(20 + (processed / total_chunks) * progress_range)
        job.progress = min(batch_progress, 95)
        job.progress_message = f"Embedded {processed}/{total_chunks} chunks"
        storage.save(job)

    # --- Stage 4: Finalization (95-100%) ---
    job.status = JobStatus.completed
    job.progress = 100
    job.progress_message = f"Completed: {total_chunks} chunks from {len(pages)} pages"
    job.result = {
        "chunks_total": total_chunks,
        "pages_total": len(pages),
        "collection": collection_name,
        "source": file_path.name,
    }
    job.completed_at = datetime.now(UTC).isoformat()
    storage.save(job)

    logger.info(
        "RAG ingest completed: %s -> %d chunks in collection '%s'",
        file_path.name,
        total_chunks,
        collection_name,
    )


def _set_cancelled(job: Job, storage: JobStorage) -> None:
    """Mark job as cancelled."""
    job.status = JobStatus.cancelled
    job.completed_at = datetime.now(UTC).isoformat()
    storage.save(job)
