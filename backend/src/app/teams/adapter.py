"""Teams channel adapter -- SDK host + reply orchestration (CTR-0140, PRP-0092).

Hosts the ``microsoft-teams-apps`` SDK (Teams AI Library v2) IN-PROCESS and ties
the SDK-independent pipeline (``app.teams.pipeline``) to the agent core
(``app.teams.agent_run``) and the proactive reply path (UDR-0070 D2/D4/D6/D8).

How the SDK is wired (validated against microsoft-teams-apps 2.0.13.x):

- A ``FastAPIAdapter(app=<our FastAPI app>)`` is passed to the Teams ``App`` as its
  ``http_server_adapter``, so the SDK registers its messaging route onto OUR
  FastAPI app -- ChatWalaʻau owns the single HTTP server (UDR-0070 D2).
- ``await app.initialize()`` registers ``POST {messaging_endpoint}`` (set to
  ``TEAMS_MESSAGING_ENDPOINT_PATH``) with Bot Framework JWT validation, WITHOUT
  running the SDK's own uvicorn (``app.start()`` is never called).
- ``@app.on_message`` dispatches inbound activities to ``_handle_ctx`` -> the
  SDK-independent pipeline.
- Replies are proactive: the inbound handler returns fast (ACK) after dispatching
  the agent turn to the Background Task Runner (CTR-0108); the turn replies with
  ``app.send(conversation_id, ...)`` (UDR-0070 D6).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.config import settings
from app.teams import authz, reply
from app.teams.approval import build_approval_card, parse_submit
from app.teams.message import TeamsMessage
from app.teams.pipeline import InboundView, build_teams_message
from app.teams.store import conversation_refs

logger = logging.getLogger(__name__)

_BACKGROUND_TASK_TYPE = "teams-turn"

# A module-level handle to the live adapter so the background-task runner (which
# receives only a ctx dict) and the lifespan initializer can reach it.
_active_adapter: TeamsAdapter | None = None


class TeamsAdapter:
    """Hosts the Teams SDK and orchestrates inbound -> core -> proactive reply."""

    def __init__(self, *, agent_registry: Any) -> None:
        self._agent_registry = agent_registry
        self._teams_app: Any = None  # microsoft_teams.apps.App
        global _active_adapter
        _active_adapter = self
        # Register the proactive-reply background task once (CTR-0108).
        from app.background import register_task

        register_task(_BACKGROUND_TASK_TYPE, _run_teams_turn_task)

    # ---- SDK wiring (microsoft-teams-apps) ----

    def build(self, fastapi_app: Any) -> None:
        """Build the Teams App over OUR FastAPI app (no SDK-owned server; UDR-0070 D2)."""
        from microsoft_teams.api import MessageActivity  # type: ignore[import-not-found]  # noqa: TC002
        from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter  # type: ignore[import-not-found]

        adapter = FastAPIAdapter(app=fastapi_app)
        self._teams_app = App(
            client_id=settings.teams_app_id,
            client_secret=settings.teams_app_password,
            tenant_id=settings.teams_tenant_id,
            http_server_adapter=adapter,
            messaging_endpoint=settings.teams_messaging_endpoint_path or "/api/teams/messages",
        )

        @self._teams_app.on_message
        async def _on_message(ctx: ActivityContext[MessageActivity]) -> None:  # pragma: no cover -- SDK-driven
            await self._handle_ctx(ctx)

        @self._teams_app.on_install_add
        async def _on_install(ctx: Any) -> None:  # pragma: no cover -- SDK-driven
            # Greet when added (esp. to a group chat / channel) so the operator can
            # confirm the bot installed correctly. Best-effort; never fails the add.
            try:
                await ctx.send(
                    "ChatWalaʻau is ready. In a group chat or channel, @mention me to ask; "
                    "in a 1:1 chat, just send a message."
                )
            except Exception:
                logger.debug("Teams install welcome failed", exc_info=True)

    async def initialize(self) -> None:
        """Register the messaging route + JWT validation WITHOUT starting a server."""
        if self._teams_app is None:
            return
        await self._teams_app.initialize()
        logger.info(
            "Microsoft Teams integration initialized: POST %s (PRP-0092)",
            settings.teams_messaging_endpoint_path or "/api/teams/messages",
        )

    # ---- inbound projection (SDK -> SDK-neutral InboundView) ----

    def _handle_ctx(self, ctx: Any) -> Any:  # pragma: no cover -- SDK-driven
        return self.on_message(self._extract_view(ctx))

    def _extract_view(self, ctx: Any) -> InboundView:  # pragma: no cover -- SDK-driven
        from microsoft_teams.apps import to_threaded_conversation_id  # type: ignore[import-not-found]

        activity = ctx.activity
        conversation = getattr(activity, "conversation", None)
        sender = getattr(activity, "from_", None)
        recipient = getattr(activity, "recipient", None)
        bot_id = getattr(recipient, "id", "") or ""
        conversation_id = getattr(conversation, "id", "") or ""
        conv_type_raw = getattr(conversation, "conversation_type", None)

        # Bot echo: the message's sender is the bot itself.
        is_echo = bool(sender and bot_id and getattr(sender, "id", "") == bot_id)

        # The bot's OWN recipient mention(s) from the activity entities (UDR-0070 D7).
        mention_texts: list[str] = []
        for entity in getattr(activity, "entities", None) or []:
            if getattr(entity, "type", "") == "mention":
                mentioned = getattr(entity, "mentioned", None)
                if mentioned is not None and getattr(mentioned, "id", "") == bot_id:
                    mention_texts.append(getattr(entity, "text", "") or "")

        # Proactive-reply target: thread the reply in a channel (UDR-0070 D6).
        reply_target = conversation_id
        if (conv_type_raw or "").lower() == "channel" and ";messageid=" not in conversation_id:
            reply_target = to_threaded_conversation_id(conversation_id, getattr(activity, "id", "") or "")

        return InboundView(
            activity_id=getattr(activity, "id", "") or "",
            conversation_id=conversation_id,
            conversation_type_raw=conv_type_raw,
            text=getattr(activity, "text", "") or "",
            sender_object_id=getattr(sender, "aad_object_id", "") or "",
            sender_id=getattr(sender, "id", "") or "",
            sender_name=getattr(sender, "name", "") or "",
            is_bot_echo=is_echo,
            bot_mention_texts=mention_texts,
            image_uris=[],  # attachment caching is a follow-up (UDR-0070 D9 best-effort)
            conversation_ref=reply_target,
            submit_value=getattr(activity, "value", None),
        )

    # ---- inbound orchestration (SDK-independent) ----

    async def on_message(self, view: InboundView) -> None:
        """Pipeline -> authz -> dispatch; reply proactively (UDR-0070 D4/D5/D6)."""
        # An Adaptive Card Action.Submit (tool approval decision) arrives as a
        # message activity with `value` set -- resolve it and stop (CTR-0141).
        if view.submit_value and await self.handle_submit(view.submit_value):
            return
        msg = build_teams_message(view)
        if msg is None:
            return  # bot echo or duplicate -> no run
        conversation_refs.save(msg.thread_id, view.conversation_ref)
        if not authz.is_sender_allowed(msg.sender_object_id):
            logger.info("Teams sender %s not allowed; refusing", msg.sender_object_id or "<unknown>")
            await self.send_text(view.conversation_ref, "Sorry, you are not authorized to use this assistant.")
            return
        try:
            await self.send_typing(view.conversation_ref)
        except Exception:  # typing is cosmetic; never fail the turn
            logger.debug("Teams typing indicator failed", exc_info=True)
        # Dispatch the agent turn (deduped by message id); reply proactively.
        from app.background import dispatch

        dispatch(_BACKGROUND_TASK_TYPE, dedup_key=msg.message_id, ctx={"message": msg})

    async def run_and_reply(self, msg: TeamsMessage) -> None:
        """Run the agent turn and send the chunked reply (background-task body)."""
        from app.teams.agent_run import run_turn

        async def _renderer(record_id: str, thread_id: str, tool_name: str, preview: dict[str, Any]) -> bool:
            return await self._render_approval(msg.conversation_ref, record_id, thread_id, tool_name, preview)

        try:
            text = await run_turn(msg, agent_registry=self._agent_registry, approval_renderer=_renderer)
        except Exception:
            logger.warning("Teams agent turn failed for %s", msg.thread_id, exc_info=True)
            await self.send_text(msg.conversation_ref, "Sorry, something went wrong while answering.")
            return
        # Generated images are sent as DATA attachments, not dead /api/uploads links
        # (UDR-0070 D9); the image markdown is stripped from the text body.
        from app.teams.outbound import extract_upload_images, strip_upload_image_markdown

        images = extract_upload_images(text, msg.thread_id)
        body = strip_upload_image_markdown(text)
        chunks = reply.chunk_reply(body)
        if not chunks and not images:
            chunks = ["(no response)"]
        # Group Chat / Channel: @mention the user who addressed the bot, on the FIRST
        # message only. Personal Chat: no mention (UDR-0070 D6).
        mention = msg.conversation_type in ("group", "channel")
        for index, chunk in enumerate(chunks):
            await self.send_reply(msg, chunk, mention=mention and index == 0)
        for image in images:
            await self.send_image(msg, image)

    async def _render_approval(
        self,
        conversation_id: Any,
        record_id: str,
        thread_id: str,
        tool_name: str,
        preview: dict[str, Any],
    ) -> bool:
        """Render an Adaptive Card and park until the user decides (UDR-0070 D8)."""
        from app.agent.approval import approval_store

        card = build_approval_card(
            record_id=record_id, thread_id=thread_id, tool_name=tool_name, arguments_preview=preview
        )
        await self.send_card(conversation_id, card)
        record = await approval_store.get(record_id)
        if record is None:
            return False
        try:
            await asyncio.wait_for(record.event.wait(), timeout=float(settings.tool_approval_timeout_sec))
        except TimeoutError:
            return False
        return bool(record.resolution and record.resolution.approved)

    async def handle_submit(self, value: dict[str, Any]) -> bool:
        """Resolve a tool-approval Adaptive Card Action.Submit (CTR-0141)."""
        from app.teams.approval import apply_decision

        submit = parse_submit(value)
        if submit is None:
            return False
        await apply_decision(submit)
        return True

    # ---- proactive send (SDK app.send chokepoint) ----

    async def send_reply(self, msg: TeamsMessage, text: str, *, mention: bool) -> None:  # pragma: no cover -- SDK
        """Send a text reply, optionally @mentioning the sender (UDR-0070 D6).

        Group Chat / Channel mention the user who addressed the bot; Personal Chat
        does not. The mention uses the sender's Bot Framework channel account id.
        """
        if self._teams_app is None or not msg.conversation_ref:
            return
        from microsoft_teams.api import Account, MessageActivityInput  # type: ignore[import-not-found]

        activity = MessageActivityInput()
        if mention and msg.sender_id:
            activity = activity.add_mention(Account(id=msg.sender_id, name=msg.sender_name or "user"))
            activity = activity.add_text(f" {text}")
        else:
            activity = activity.add_text(text)
        await self._teams_app.send(msg.conversation_ref, activity)

    async def send_image(self, msg: TeamsMessage, image: Any) -> None:  # pragma: no cover -- SDK-driven
        """Send a generated image as INLINE DATA (a data: URI), not a link (UDR-0070 D9).

        The attachment carries NO ``name``: a Bot Framework image attachment renders
        inline only when it has a data-URI ``content_url`` and no file name (a name
        makes Teams show a download/file card with just the filename instead).
        """
        if self._teams_app is None or not msg.conversation_ref:
            return
        from microsoft_teams.api import Attachment, MessageActivityInput  # type: ignore[import-not-found]

        attachment = Attachment(content_type=image.content_type, content_url=image.data_uri)
        await self._teams_app.send(msg.conversation_ref, MessageActivityInput().add_attachments(attachment))

    async def send_text(self, conversation_id: Any, text: str) -> None:  # pragma: no cover -- SDK-driven
        if self._teams_app is None or not conversation_id:
            return
        await self._teams_app.send(conversation_id, text)

    async def send_typing(self, conversation_id: Any) -> None:  # pragma: no cover -- SDK-driven
        if self._teams_app is None or not conversation_id:
            return
        from microsoft_teams.api import TypingActivityInput  # type: ignore[import-not-found]

        await self._teams_app.send(conversation_id, TypingActivityInput())

    async def send_card(self, conversation_id: Any, card: dict[str, Any]) -> None:  # pragma: no cover -- SDK-driven
        if self._teams_app is None or not conversation_id:
            return
        try:
            from microsoft_teams.cards import AdaptiveCard  # type: ignore[import-not-found]

            await self._teams_app.send(conversation_id, AdaptiveCard.model_validate(card))
        except Exception:
            # Best-effort: fall back to a text prompt if the card cannot be built.
            logger.debug("Teams Adaptive Card send failed; falling back to text", exc_info=True)
            tool = card.get("body", [{}])[1].get("text", "a tool") if card.get("body") else "a tool"
            await self._teams_app.send(
                conversation_id,
                f"Approval required to run {tool}. Reply 'allow' or 'deny'.",
            )


async def _run_teams_turn_task(ctx: dict[str, Any]) -> None:
    """Background-task body (CTR-0108): run the agent turn and reply proactively."""
    msg = ctx.get("message")
    if not isinstance(msg, TeamsMessage):
        return
    adapter = _active_adapter
    if adapter is None:
        logger.warning("teams-turn dispatched with no active adapter")
        return
    await adapter.run_and_reply(msg)


async def initialize_active_adapter() -> None:
    """Initialize the live adapter's Teams App at FastAPI startup (lifespan)."""
    if _active_adapter is not None:
        await _active_adapter.initialize()
