"""Azure OpenAI Embedding wrapper for RAG pipeline (CTR-0076, PRP-0037, PRP-0047).

Embeds text chunks in batches using the Azure OpenAI Embedding API.

A single ``AzureOpenAI`` client is cached at module level so credential
resolution and TLS handshakes happen once per process, not once per batch.
The cache is thread/process safe because CPython module globals are
initialized at import time and subsequent access only reads the reference.
Tests inject a mock client via the ``client`` parameter of
:func:`embed_texts` to avoid touching Azure at all.
"""

from __future__ import annotations

import logging
import os

from openai import AzureOpenAI

from app.azure_credential import get_azure_openai_kwargs

logger = logging.getLogger(__name__)

# Azure OpenAI Embedding API accepts up to 2048 inputs per request,
# but practical batch size is kept at 100 for progress granularity.
EMBEDDING_BATCH_SIZE = 100

# Lazy-initialized Azure OpenAI client for RAG embedding (PRP-0047).
# Reused across batches so Azure CLI credential resolution and TLS
# handshake happen once per process, not once per 100-text batch.
_client: AzureOpenAI | None = None


def _create_client() -> AzureOpenAI:
    """Create a new Azure OpenAI client for RAG embedding.

    Credential resolution centralised in app.azure_credential (PRP-0058,
    UDR-0034). AZURE_OPENAI_API_KEY set -> api_key= shape; unset ->
    AzureCliCredential() via azure_ad_token_provider.

    Model Offering Catalog lane (PRP-0109, UDR-0087 D7): when an ``embeddings``
    offering is present, its endpoint / api_version / api_key override the
    dedicated env config (a base_url offering builds a plain OpenAI client
    instead of AzureOpenAI). None -> the existing AZURE_OPENAI_ENDPOINT path
    byte-for-byte.

    Callers should normally prefer :func:`_get_client` to reuse the
    module-level singleton; this function stays public for tests that
    need to assert construction behavior.
    """
    from app import models_catalog

    config = models_catalog.embedding_config()
    if config is not None and config.base_url:
        from openai import OpenAI

        return OpenAI(api_key=config.api_key or os.environ.get("OPENAI_API_KEY", ""), base_url=config.base_url)

    endpoint = (config.endpoint if config is not None and config.endpoint else os.environ.get("AZURE_OPENAI_ENDPOINT", "")) or ""
    if not endpoint:
        msg = "AZURE_OPENAI_ENDPOINT must be set for RAG embedding"
        raise ValueError(msg)

    api_version = (config.api_version if config is not None and config.api_version else "2024-10-21")
    cred_kwargs = {"api_key": config.api_key} if (config is not None and config.api_key) else get_azure_openai_kwargs()
    return AzureOpenAI(
        azure_endpoint=endpoint,
        api_version=api_version,
        **cred_kwargs,
    )


def _get_client() -> AzureOpenAI:
    """Return the module-level Azure OpenAI client, constructing on first use."""
    global _client
    if _client is None:
        _client = _create_client()
    return _client


def reset_client_for_tests() -> None:
    """Discard the cached client so the next call constructs a new one.

    Used by tests that switch the simulated Azure environment.
    """
    global _client
    _client = None


def _is_demo_mode_env() -> bool:
    """Return True when DEMO_MODE is enabled (env-only check).

    The batch MCP server runs in a child process so it cannot import
    ``app.demo`` reliably (it would need a fresh ``Settings``
    construction). Check the env var directly -- the value is the
    same string format pydantic parses for ``bool`` Settings fields.
    """
    raw = (os.environ.get("DEMO_MODE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def embed_texts(
    texts: list[str],
    model: str | None = None,
    client: AzureOpenAI | None = None,
) -> list[list[float]]:
    """Embed a list of texts using Azure OpenAI Embedding API.

    Args:
        texts: Texts to embed.
        model: Deployment name (default: EMBEDDING_DEPLOYMENT_NAME env var).
        client: Optional Azure OpenAI client. When None, reuses the
            module-level singleton created by :func:`_get_client`.

    Returns:
        List of embedding vectors (same order as input texts).

    PRP-0066 / UDR-0041: when DEMO_MODE=true the deterministic hash-based
    DemoEmbedder replaces the Azure call -- same dimensionality (1536),
    same return shape, zero outbound traffic.
    """
    if not texts:
        return []

    if _is_demo_mode_env():
        from app.demo.embedder import embed_demo_batch

        embeddings = embed_demo_batch(texts)
        logger.info("Embedded %d texts via DemoEmbedder (1536-dim hash)", len(embeddings))
        return embeddings

    if model is not None:
        deployment = model
    else:
        from app import models_catalog

        config = models_catalog.embedding_config()
        deployment = (
            config.deployment
            if config is not None
            else os.environ.get("EMBEDDING_DEPLOYMENT_NAME", "text-embedding-3-small")
        )
    azure_client = client if client is not None else _get_client()

    response = azure_client.embeddings.create(
        input=texts,
        model=deployment,
    )

    embeddings = [item.embedding for item in response.data]
    logger.info("Embedded %d texts via %s", len(embeddings), deployment)
    return embeddings


def embed_texts_batched(
    texts: list[str],
    batch_size: int = EMBEDDING_BATCH_SIZE,
    model: str | None = None,
    client: AzureOpenAI | None = None,
) -> list[list[float]]:
    """Embed texts in batches to handle large document sets.

    The Azure OpenAI client is created once on first call and reused
    across every batch to avoid repeated credential resolution and TLS
    handshakes (PRP-0047).

    Args:
        texts: All texts to embed.
        batch_size: Number of texts per API call.
        model: Deployment name.
        client: Optional Azure OpenAI client for test injection.

    Returns:
        List of all embedding vectors.
    """
    if not texts:
        return []

    azure_client = client if client is not None else _get_client()

    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        embeddings = embed_texts(batch, model, client=azure_client)
        all_embeddings.extend(embeddings)
    return all_embeddings
