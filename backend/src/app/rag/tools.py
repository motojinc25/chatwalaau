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

    v0.117.2: only the ChromaDB client construction is skipped on re-entry. The
    embedding model name and the embedding client are ALWAYS re-resolved from the
    catalog. Previously the whole function returned early once the Chroma client
    existed, so changing the ``embeddings`` offering through the operator-facing
    catalog -- which hot-reloads in-process (CTR-0175, UDR-0090 / PRP-0115) --
    left the QUERY embedder pinned to the model resolved at first build while the
    rag-ingest job already wrote vectors with the NEW model. The dimension
    mismatch made every search fail until the process was restarted.
    """
    from app import models_catalog
    from app.demo import is_demo_mode

    global _chroma_client, _openai_client, _embedding_model, _default_collection, _default_top_k
    _default_collection = collection_name
    _default_top_k = top_k
    if _chroma_client is None:
        _chroma_client = chromadb.PersistentClient(path=chroma_dir)

    # Model Offering Catalog (PRP-0114, UDR-0095 D1): the single ``embeddings``
    # offering is the SOLE source of the query embedding model (non-demo). The query
    # embedder MUST use the SAME model as ingest to keep vector dimensions
    # consistent -- both read this one offering. This function is only called when
    # an offering exists OR DEMO_MODE (the agent factory gates registration,
    # CTR-0077); the demo branch builds no Azure client (DemoEmbedder, UDR-0095 D4).
    config = models_catalog.embedding_config()
    _embedding_model = config.deployment if config is not None else "demo-embedder"

    # Rebuilt on every call so an offering edit (endpoint / api_version / key /
    # deployment) takes effect on the next agent rebuild. Reset first so a
    # catalog change that REMOVES the offering cannot leave the previous client
    # live and silently embedding against the old deployment.
    _openai_client = None
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


#: Fixed answers (PRP-0256, UDR-0237 D3/D5). A refused and a missing collection give the
#: same text under a scope, so the model learns nothing about other collections.
COLLECTION_NOT_FOUND = "Collection not found."
COLLECTION_NOT_AVAILABLE = "Collection not available."
NO_DOCUMENTS_YET = "No documents ingested in this collection yet."

_RAG_SEARCH_DOC = (
    "Search ingested documents for relevant information using vector similarity. Use when the user asks "
    "about uploaded PDFs, ingested documents, or the knowledge base. Returns text chunks with source "
    "filename, page number, and relevance score."
)


def default_collection() -> str:
    return _default_collection


def default_top_k() -> int:
    return _default_top_k


def is_initialized() -> bool:
    return _chroma_client is not None


def collection_names() -> list[str] | None:
    """Names of the existing collections, or None when RAG is not initialized."""
    if _chroma_client is None:
        return None
    return [
        item if isinstance(item, str) else getattr(item, "name", str(item))
        for item in _chroma_client.list_collections()
    ]


def _get_collection(name: str):
    """The collection, or None when it does not exist. Never creates one (UDR-0237 D5)."""
    try:
        return _chroma_client.get_collection(name=name)
    except Exception:
        return None


def search_collection(query: str, collection: str, top_k: int) -> dict:
    """One similarity search; the JSON-ready result of ``rag_search`` (CTR-0077).

    Shared by the tool and the grounded answer gate (CTR-0287), so both read the same hits.
    """
    if _chroma_client is None:
        return {"error": "RAG search not initialized. CHROMA_DIR may not be configured."}
    try:
        col = _get_collection(collection)
        if col is None:
            if collection == _default_collection:
                # A fresh install has not ingested into the default collection yet.
                return {"results": [], "query": query, "collection": collection, "message": NO_DOCUMENTS_YET}
            return {"error": COLLECTION_NOT_FOUND, "results": [], "query": query, "collection": collection}

        count = col.count()
        if count == 0:
            return {"results": [], "query": query, "collection": collection, "message": NO_DOCUMENTS_YET}

        # Embed query using Azure OpenAI (same model as ingest) to match dimensions
        query_embedding = _embed_query(query)

        results = col.query(query_embeddings=[query_embedding], n_results=min(top_k, count))

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

        return {"results": formatted, "query": query, "collection": collection}

    except Exception:
        logger.exception("RAG search failed for query: %s", query)
        return {
            "error": "RAG search failed. The collection may not exist or ChromaDB is unavailable.",
            "query": query,
            "collection": collection,
        }


def rag_search(
    query: Annotated[str, Field(description="Search query text in natural language")],
    collection: Annotated[str, Field(description="Collection name (leave empty for default)")] = "",
    n_results: Annotated[int, Field(description="Number of results to return (0 for default)")] = 0,
) -> str:
    """Search ingested documents for relevant information using vector similarity. Use when the user asks about uploaded PDFs, ingested documents, or the knowledge base. Returns text chunks with source filename, page number, and relevance score."""
    top_k = n_results if n_results > 0 else _default_top_k
    return json.dumps(search_collection(query, collection or _default_collection, top_k))


def make_scoped_rag_search(collections: tuple[str, ...]):
    """``rag_search`` limited to ``collections`` (PRP-0256, UDR-0237 D3).

    Installed only by the tool-assembly chokepoint for a spec with a knowledge scope. The
    first collection is the default; any other name answers ``COLLECTION_NOT_AVAILABLE``.

    A routed /v1/responses run narrows the list further for that run only (PRP-0258,
    UDR-0239 D5): the route's first allowed collection becomes the default, and a
    collection outside the route answers the same refusal. Unset, nothing changes.
    """
    from app.agent.knowledge_narrowing import narrow_collections

    allowed = tuple(collections)

    def rag_search(query: str, collection: str = "", n_results: int = 0) -> str:
        run_allowed = narrow_collections(allowed)
        name = collection or (run_allowed[0] if run_allowed else "")
        if not name or name not in run_allowed:
            return json.dumps({"error": COLLECTION_NOT_AVAILABLE, "results": [], "query": query})
        top_k = n_results if n_results > 0 else _default_top_k
        return json.dumps(search_collection(query, name, top_k))

    # Real objects, not strings: this module uses postponed annotations, and the allowed
    # names are local to this call.
    rag_search.__annotations__ = {
        "query": Annotated[str, Field(description="Search query text in natural language")],
        "collection": Annotated[
            str, Field(description=f"Collection name, one of: {', '.join(allowed)} (leave empty for {allowed[0]})")
        ],
        "n_results": Annotated[int, Field(description="Number of results to return (0 for default)")],
        "return": str,
    }
    rag_search.__doc__ = _RAG_SEARCH_DOC
    rag_search.scoped_collections = allowed  # type: ignore[attr-defined]
    return rag_search
