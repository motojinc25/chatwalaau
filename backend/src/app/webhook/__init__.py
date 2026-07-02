"""Inbound Webhook Gateway (CAP-010, FEAT-0052/0053, PRP-0097, UDR-0075/0076).

A new external-boundary capability that lets ChatWalaʻau be DRIVEN by external
events delivered as HTTP webhooks. It is a sibling of the Teams channel adapter
(CAP-009): a separate capability with its OWN trust model, mounted into the single
ChatWalaʻau FastAPI app, depending one-way on the CAP-002 core.

Design (UDR-0075):

- Generic source registry (CTR-0151) is the single extension point; the first
  registered source is Microsoft Graph (``msgraph``). A later source is one
  registration (UDR-0075 D2).
- The public ingress (CTR-0149) handles the Graph validation handshake and ACKs
  notifications with 202; the heavy work runs asynchronously as a pipeline job
  (UDR-0075 D3).
- The Microsoft Graph adapter (CTR-0150) verifies ``clientState`` (HMAC-safe),
  optional CIDR / resource allowlists, and de-duplicates in a process-local store.
- The subscription lifecycle (CTR-0152) subscribes / renews / deletes against Graph
  and runs an internal maintenance scheduler that auto-renews ONLY while
  ``CRON_ENABLED`` (UDR-0075 D8).
- Graph access uses dedicated ``GRAPH_*`` app-only credentials (CTR-0153, UDR-0075 D6)
  and PLAIN notifications + fetch (UDR-0075 D5).
- The first consumer is the Teams Meeting Pipeline (FEAT-0053, CTR-0156), a pipeline
  job type contributed into the CTR-0073 engine (UDR-0076).

The whole capability is INERT unless ``WEBHOOK_ENABLED`` (UDR-0075 D11):
``register_webhook`` is a no-op when disabled, so the routers are not mounted and the
Graph SDK/credentials are never touched.
"""

from __future__ import annotations

from app.webhook.router import (
    initialize_webhook,
    register_webhook,
    shutdown_webhook,
)

__all__ = ["initialize_webhook", "register_webhook", "shutdown_webhook"]
