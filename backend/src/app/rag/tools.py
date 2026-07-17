"""RAG Search Function Tool for the main agent (CTR-0077, PRP-0037).

Provides rag_search as a MAF Function Tool that queries ChromaDB
for similar document chunks using vector similarity search.

Uses the same plain-function + Annotated[type, Field(description=...)]
pattern as Weather tools (CTR-0027) for MAF AI Function registration.

Query embedding uses Azure OpenAI Embedding API (same model as ingest)
to ensure dimension consistency. ChromaDB's default embedding function
(all-MiniLM-L6-v2, 384d) is NOT used; we embed explicitly with the model
declared by the catalog embeddings offering (PRP-0114, UDR-0095 D1).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Annotated

import chromadb
from openai import AzureOpenAI
from pydantic import Field

from app.azure_credential import get_azure_openai_kwargs

logger = logging.getLogger(__name__)

# Module-level state (initialized by init_rag_search)
_chroma_client: chromadb.ClientAPI | None = None
_openai_client: AzureOpenAI | None = None
_embedding_model: str = "text-embedding-3-small"
_default_collection: str = "default"
_default_top_k: int = 5


def init_rag_search(chroma_dir: str, collection_name: str, top_k: int) -> None:
    """Initialize RAG search module state.

    Idempotent (PRP-0086): the agent factory's tool assembly is now re-run on every
    runtime agent rebuild (MCP tool gating, CTR-0121), so a second call with the
    ChromaDB PersistentClient already constructed re-uses it instead of opening a
    new SQLite-backed client to the same directory. The cheap config values
    (collection / top_k) are still refreshed.
    """
    from app import models_catalog
    from app.demo import is_demo_mode

    global _chroma_client, _openai_client, _embedding_model, _default_collection, _default_top_k
    _default_collection = collection_name
    _default_top_k = top_k
    if _chroma_client is not None:
        return
    _chroma_client = chromadb.PersistentClient(path=chroma_dir)

    # Model Offering Catalog (PRP-0114, UDR-0095 D1): the single ``embeddings``
    # offering is the SOLE source of the query embedding model (non-demo). The query
    # embedder MUST use the SAME model as ingest to keep vector dimensions
    # consistent -- both read this one offering. This function is only called when
    # an offering exists OR DEMO_MODE (the agent factory gates registration,
    # CTR-0077); the demo branch builds no Azure client (DemoEmbedder, UDR-0095 D4).
    config = models_catalog.embedding_config()
    _embedding_model = config.deployment if config is not None else "demo-embedder"

    if not is_demo_mode() and config is not None:
        if config.base_url:
            from openai import OpenAI

            _openai_client = OpenAI(
                api_key=config.api_key or os.environ.get("OPENAI_API_KEY", ""),
                base_url=config.base_url,
            )
        else:
            endpoint = config.endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT", "")
            if endpoint:
                api_version = config.api_version or models_catalog.DEFAULT_EMBEDDING_API_VERSION
                cred_kwargs = {"api_key": config.api_key} if config.api_key else get_azure_openai_kwargs()
                _openai_client = AzureOpenAI(
                    azure_endpoint=endpoint,
                    api_version=api_version,
                    **cred_kwargs,
                )

    logger.info(
        "RAG search initialized (chroma_dir=%s, collection=%s, top_k=%d, embedding=%s, demo=%s)",
        chroma_dir,
        collection_name,
        top_k,
        _embedding_model,
        is_demo_mode(),
    )


def _embed_query(text: str) -> list[float]:
    """Embed a query text.

    PRP-0066 / UDR-0041: when DEMO_MODE=true, DemoEmbedder replaces
    the Azure call so the same 1536-dim deterministic-hash vectors used
    by the demo ingest pipeline are used here too. Mixing demo and
    live vectors in the same collection is forbidden -- the deploy
    guide tells operators to use a fresh CHROMA_DIR on the demo host.
    """
    from app.demo import is_demo_mode

    if is_demo_mode():
        from app.demo.embedder import embed_demo

        return embed_demo(text)

    if _openai_client is None:
        msg = "Azure OpenAI client not initialized for query embedding"
        raise RuntimeError(msg)
    response = _openai_client.embeddings.create(input=[text], model=_embedding_model)
    return response.data[0].embedding


def rag_search(
    query: Annotated[str, Field(description="Search query text in natural language")],
    collection: Annotated[str, Field(description="Collection name (leave empty for default)")] = "",
    n_results: Annotated[int, Field(description="Number of results to return (0 for default)")] = 0,
) -> str:
    """Search ingested documents for relevant information using vector similarity. Use when the user asks about uploaded PDFs, ingested documents, or the knowledge base. Returns text chunks with source filename, page number, and relevance score."""
    if _chroma_client is None:
        return json.dumps({"error": "RAG search not initialized. CHROMA_DIR may not be configured."})

    col_name = collection or _default_collection
    top_k = n_results if n_results > 0 else _default_top_k

    try:
        col = _chroma_client.get_or_create_collection(name=col_name)

        if col.count() == 0:
            return json.dumps(
                {
                    "results": [],
                    "query": query,
                    "collection": col_name,
                    "message": "No documents ingested in this collection yet.",
                }
            )

        # Embed query using Azure OpenAI (same model as ingest) to match dimensions
        query_embedding = _embed_query(query)

        results = col.query(
            query_embeddings=[query_embedding],
            n_results=min(top_k, col.count()),
        )

        formatted = []
        if results["documents"] and results["documents"][0]:
            documents = results["documents"][0]
            metadatas = results["metadatas"][0] if results["metadatas"] else [{}] * len(documents)
            distances = results["distances"][0] if results["distances"] else [0.0] * len(documents)

            for doc, meta, dist in zip(documents, metadatas, distances, strict=False):
                formatted.append(
                    {
                        "text": doc,
                        "source": meta.get("source", ""),
                        "page": meta.get("page", 0),
                        "chunk_index": meta.get("chunk_index", 0),
                        "distance": round(dist, 4),
                    }
                )

        return json.dumps(
            {
                "results": formatted,
                "query": query,
                "collection": col_name,
            }
        )

    except Exception:
        logger.exception("RAG search failed for query: %s", query)
        return json.dumps(
            {
                "error": "RAG search failed. The collection may not exist or ChromaDB is unavailable.",
                "query": query,
                "collection": col_name,
            }
        )
