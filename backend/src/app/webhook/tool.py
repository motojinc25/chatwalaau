"""manage_webhook MAF Function Tool (CTR-0155, PRP-0097, UDR-0075).

A single agent tool exposing the webhook gateway verb surface to the LLM: list / show /
fetch (run the meeting pipeline for a meeting) / subscriptions / subscribe /
renew-subscription / delete-subscription / maintain-subscriptions / token-health /
validate. It writes through the SAME store + Graph client + pipeline engine as the REST
API (CTR-0154), so the agent and the portal never diverge.

Registered on the shared agent only when WEBHOOK_ENABLED (agent_factory). It performs no
privileged action the management API would not; subscription mutations call Graph.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from pydantic import Field

logger = logging.getLogger(__name__)

WEBHOOK_TOOL_INSTRUCTION = (
    "\n\n## Webhooks\n"
    "You can manage the inbound Webhook Gateway via the manage_webhook tool. The first "
    "source is Microsoft Graph ('msgraph'), used to turn Teams meeting transcripts into "
    "summaries. Use action='fetch' with organizer_id (the meeting organizer's user id or "
    "UPN -- required, app-only access is organizer-scoped) plus meeting_id or join_web_url "
    "to run the Teams meeting summary pipeline on demand. Use 'subscriptions' to list Graph subscriptions, "
    "'subscribe' to create one, 'renew-subscription' / 'delete-subscription' / "
    "'maintain-subscriptions' to manage them, 'token-health' to check Graph credentials, "
    "and 'validate' to self-test the notification handshake. Confirm before creating or "
    "deleting subscriptions."
)


async def manage_webhook(
    action: Annotated[
        str,
        Field(
            description=(
                "One of: list, show, fetch, subscriptions, subscribe, renew-subscription, "
                "delete-subscription, maintain-subscriptions, token-health, validate."
            )
        ),
    ],
    source: Annotated[str, Field(description="Webhook source name (default 'msgraph').")] = "msgraph",
    organizer_id: Annotated[
        str,
        Field(
            description="For fetch: the meeting organizer's AAD object id or UPN (required; app-only is organizer-scoped)."
        ),
    ] = "",
    meeting_id: Annotated[str, Field(description="For fetch: a Graph onlineMeeting id.")] = "",
    join_web_url: Annotated[str, Field(description="For fetch: a meeting joinWebUrl.")] = "",
    resource: Annotated[str, Field(description="For subscribe: Graph resource (empty = configured default).")] = "",
    subscription_id: Annotated[str, Field(description="For renew/delete-subscription: the subscription id.")] = "",
) -> str:
    """Manage the inbound Webhook Gateway (Microsoft Graph): subscriptions + meeting fetch."""
    from app.webhook import store
    from app.webhook.registry import get_source, list_sources

    act = (action or "").strip().lower()

    if act == "list":
        sources = [
            {
                "name": s.name,
                "label": s.label,
                "enabled": store.is_source_enabled(s.name),
                "receipts": store.count_receipts(s.name),
            }
            for s in list_sources()
        ]
        return json.dumps({"sources": sources}, ensure_ascii=False)

    if act == "show":
        src = get_source(source)
        if src is None:
            return f"Error: unknown source '{source}'."
        return json.dumps(
            {
                "source": src.name,
                "enabled": store.is_source_enabled(source),
                "recent_receipts": store.list_receipts(source, limit=10),
            },
            ensure_ascii=False,
        )

    if act == "fetch":
        if not organizer_id:
            return "Error: 'organizer_id' is required for fetch (app-only access is organizer-scoped)."
        if not (meeting_id or join_web_url):
            return "Error: 'meeting_id' or 'join_web_url' is required for fetch."
        from app.webhook.msgraph import meeting_pipeline

        try:
            result = await meeting_pipeline.fetch(
                organizer_id=organizer_id, meeting_id=meeting_id, join_web_url=join_web_url
            )
        except ValueError as exc:
            return f"Error: {exc}"
        return json.dumps({"fetched": result}, ensure_ascii=False)

    # The remaining actions are Microsoft Graph subscription operations.
    from app.webhook.msgraph import subscriptions

    try:
        if act == "subscriptions":
            return json.dumps(await subscriptions.list_all(), ensure_ascii=False)
        if act == "subscribe":
            return json.dumps({"subscribed": await subscriptions.subscribe(resource=resource)}, ensure_ascii=False)
        if act == "renew-subscription":
            if not subscription_id:
                return "Error: 'subscription_id' is required for renew-subscription."
            return json.dumps({"renewed": await subscriptions.renew(subscription_id)}, ensure_ascii=False)
        if act == "delete-subscription":
            if not subscription_id:
                return "Error: 'subscription_id' is required for delete-subscription."
            return json.dumps({"deleted": await subscriptions.delete(subscription_id)}, ensure_ascii=False)
        if act == "maintain-subscriptions":
            return json.dumps(await subscriptions.maintain(), ensure_ascii=False)
        if act == "token-health":
            return json.dumps(await subscriptions.token_health(), ensure_ascii=False)
        if act == "validate":
            return json.dumps(await subscriptions.validate(), ensure_ascii=False)
    except ValueError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: Graph operation failed: {exc}"

    return (
        "Error: 'action' must be one of list, show, fetch, subscriptions, subscribe, "
        "renew-subscription, delete-subscription, maintain-subscriptions, token-health, validate."
    )


__all__ = ["WEBHOOK_TOOL_INSTRUCTION", "manage_webhook"]
