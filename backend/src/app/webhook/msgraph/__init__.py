"""Microsoft Graph webhook source (CTR-0150/0152/0153/0156, PRP-0097, UDR-0075/0076).

The first registered webhook source. Modules:

- ``graph_client``     -- app-only token + Graph REST calls (CTR-0153)
- ``adapter``          -- ingress handler: clientState/allowlist/dedupe + 202 (CTR-0150)
- ``subscriptions``    -- subscribe / renew / delete / maintain / token-health (CTR-0152)
- ``meeting_pipeline`` -- the teams-meeting pipeline job type + handoff (CTR-0156)

Everything here is reached only when WEBHOOK_ENABLED and the source is registered by
``app.webhook.router.register_webhook`` (UDR-0075 D11).
"""

from __future__ import annotations

SOURCE_NAME = "msgraph"

__all__ = ["SOURCE_NAME"]
