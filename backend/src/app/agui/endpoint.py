"""AG-UI endpoint with reasoning event support (CTR-0009, PRP-0009).

Custom AG-UI streaming endpoint that replaces add_agent_framework_fastapi_endpoint()
to emit REASONING_* events for text_reasoning content. The upstream library's
_emit_content() skips text_reasoning; this endpoint handles it directly.

Session management is enabled via FileHistoryProvider (CTR-0014).
Agent selection is done via AgentRegistry using state.model (CTR-0070, PRP-0035).
Background Responses support via CTR-0045 (PRP-0025).
"""

from collections.abc import AsyncGenerator
import json
import logging
from pathlib import Path
from typing import Any
import uuid

from ag_ui.core import (
    CustomEvent,
    EventType,
    ReasoningMessageContentEvent,
    ReasoningMessageEndEvent,
    ReasoningMessageStartEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from ag_ui.encoder import EventEncoder
from agent_framework import AgentSession, Content
from agent_framework.exceptions import ChatClientException
from agent_framework_ag_ui._agent_run import _normalize_response_stream
from agent_framework_ag_ui._message_adapters import normalize_agui_input_messages
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from openai import NotFoundError as OpenAINotFoundError
from pydantic import AliasChoices, BaseModel, Field

from app.agui.agent_registry import AgentRegistry
from app.auth import verify_api_key
from app.core.config import settings
from app.image_gen.tools import current_thread_id as _image_gen_thread_id

logger = logging.getLogger(__name__)


class AGUIRequest(BaseModel):
    """AG-UI protocol request (mirrors agent_framework_ag_ui._types.AGUIRequest)."""

    messages: list[dict[str, Any]] = []
    run_id: str | None = Field(default=None, validation_alias=AliasChoices("run_id", "runId"))
    thread_id: str | None = Field(default=None, validation_alias=AliasChoices("thread_id", "threadId"))
    state: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    context: list[dict[str, Any]] | None = None
    forwarded_props: dict[str, Any] | None = None


def _generate_id() -> str:
    return str(uuid.uuid4())


def _strip_pdf_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove PDF image_url entries from message content arrays.

    When session history includes messages with PDF attachments stored as
    image_url content entries, normalize_agui_input_messages would convert
    them to MAF Content objects. Azure OpenAI Responses API rejects non-image
    content types, causing 400 errors. This function strips PDF entries before
    normalization. PDF references are injected as text by _inject_image_content.
    """
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            # Filter out PDF image_url entries from content array
            filtered = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "image_url":
                    url_info = item.get("image_url", {})
                    url = url_info.get("url", "") if isinstance(url_info, dict) else ""
                    if url.endswith(".pdf") or "application/pdf" in str(url_info):
                        continue  # Skip PDF entries
                filtered.append(item)
            if filtered != content:
                msg = {**msg, "content": filtered or msg.get("content", "")}
                # If content is now empty list or has only empty items, convert to empty string
                if isinstance(msg["content"], list) and not msg["content"]:
                    msg["content"] = ""
        cleaned.append(msg)
    return cleaned


def _inject_image_content(
    maf_messages: list[Any],
    raw_messages: list[dict[str, Any]],
) -> None:
    """Inject image Content into MAF Messages from AG-UI request.

    The frontend sends images as ``{"images": [{"uri": "...", "media_type": "..."}]}``
    alongside the text content. ``normalize_agui_input_messages`` only handles text,
    so we resolve image references here and append to the MAF Message's contents.

    Local uploads (``/api/uploads/...``) are read from disk via ``Content.from_data()``.
    External URLs are passed via ``Content.from_uri()``.
    """
    upload_dir = Path(settings.upload_dir)
    for raw_msg in raw_messages:
        images = raw_msg.get("images")
        if not images or raw_msg.get("role") != "user":
            continue
        # Find the matching MAF user message (last user message)
        target = None
        for maf_msg in reversed(maf_messages):
            if getattr(maf_msg, "role", None) == "user":
                target = maf_msg
                break
        if target is None:
            continue
        pdf_refs: list[str] = []
        for img in images:
            uri = img.get("uri", "")
            media_type = img.get("media_type", "")
            # PDF files: collect references for text injection (not sent as image content)
            if media_type == "application/pdf" or uri.endswith(".pdf"):
                if uri.startswith("/api/uploads/"):
                    parts = uri.split("/")  # ["", "api", "uploads", thread_id, filename]
                    if len(parts) >= 5:
                        # Convert API URI to disk path for submit_job file_path param
                        disk_path = f"{settings.upload_dir}/{parts[3]}/{parts[4]}"
                        pdf_refs.append(f"[Attached PDF: {parts[4]}, file_path={disk_path}]")
                continue
            if uri.startswith("/api/uploads/"):
                parts = uri.split("/")  # ["", "api", "uploads", thread_id, filename]
                if len(parts) >= 5:
                    file_path = upload_dir / parts[3] / parts[4]
                    if file_path.is_file():
                        target.contents.append(Content.from_data(data=file_path.read_bytes(), media_type=media_type))
            elif uri.startswith(("http://", "https://")):
                target.contents.append(Content.from_uri(uri=uri, media_type=media_type))

        # Append PDF references as text so the agent knows the file paths
        if pdf_refs and target is not None:
            existing_text = ""
            for c in target.contents:
                if getattr(c, "type", None) == "text":
                    existing_text = getattr(c, "text", "")
                    break
            pdf_info = "\n".join(pdf_refs)
            if existing_text:
                # Replace the text content to include PDF references
                for i, c in enumerate(target.contents):
                    if getattr(c, "type", None) == "text":
                        target.contents[i] = Content.from_text(text=f"{existing_text}\n\n{pdf_info}")
                        break
            else:
                target.contents.append(Content.from_text(text=pdf_info))


async def _stream_with_reasoning(
    agent_registry: AgentRegistry,
    request_body: AGUIRequest,
) -> AsyncGenerator[str, None]:
    """Stream AG-UI events including REASONING_* for text_reasoning content.

    Iterates the MAF agent response stream directly to emit reasoning events
    that agent-framework-ag-ui's _emit_content() would otherwise skip.
    Selects the Agent from the registry based on state.model (CTR-0070).
    """
    encoder = EventEncoder()
    thread_id = request_body.thread_id or _generate_id()
    run_id = request_body.run_id or _generate_id()

    yield encoder.encode(
        RunStartedEvent(
            type=EventType.RUN_STARTED,
            run_id=run_id,
            thread_id=thread_id,
        )
    )

    msg_id: str | None = None
    reasoning_msg_id: str | None = None
    tool_call_id: str | None = None
    tc_name_current: str | None = None
    run_error = False
    error_message = ""

    try:
        # Pre-process: strip PDF image_url entries from messages before normalization.
        # normalize_agui_input_messages converts image_url content to MAF Content,
        # but Azure OpenAI Responses API rejects non-image content types (e.g., PDF).
        # PDF references are injected as text by _inject_image_content instead.
        sanitized_messages = _strip_pdf_content(request_body.messages)

        # Convert AG-UI messages to MAF Message objects
        messages, _ = normalize_agui_input_messages(sanitized_messages)

        # Inject image content from request into MAF messages (CTR-0022)
        _inject_image_content(messages, request_body.messages)

        # Create session with metadata (same as _agent_run.py:685-691)
        session = AgentSession()
        session.metadata = {
            "ag_ui_thread_id": thread_id,
            "ag_ui_run_id": run_id,
        }

        # Read model and background options from AG-UI state (CTR-0070, CTR-0045)
        selected_model = None
        background = False
        continuation_token = None
        if request_body.state:
            selected_model = request_body.state.get("model")
            background = request_body.state.get("background", False)
            continuation_token = request_body.state.get("continuation_token")

        # Select agent from registry based on model (CTR-0070, PRP-0035)
        agent = agent_registry.get(selected_model)

        # Validate continuation_token format: MAF expects dict with "response_id"
        if continuation_token and isinstance(continuation_token, str):
            continuation_token = {"response_id": continuation_token}

        run_options: dict[str, Any] = {}
        if background:
            run_options["background"] = True
        if continuation_token:
            run_options["continuation_token"] = continuation_token

        # Set thread_id for image generation tools (CTR-0050, PRP-0027)
        _image_gen_thread_id.set(thread_id)

        # Run agent with streaming
        response_stream = agent.run(
            messages,
            stream=True,
            session=session,
            options=run_options or None,
        )
        stream = await _normalize_response_stream(response_stream)

        async for update in stream:
            # Emit continuation_token if present (CTR-0045, PRP-0025)
            if background:
                update_ct = getattr(update, "continuation_token", None)
                if update_ct is not None:
                    yield encoder.encode(
                        CustomEvent(
                            type=EventType.CUSTOM,
                            name="continuation_token",
                            value=dict(update_ct) if hasattr(update_ct, "__iter__") else {"token": update_ct},
                        )
                    )

            contents = getattr(update, "contents", None) or []
            for content in contents:
                content_type = getattr(content, "type", None)

                if content_type == "text_reasoning":
                    text = getattr(content, "text", None)
                    if not text:
                        continue
                    if reasoning_msg_id is None:
                        reasoning_msg_id = _generate_id()
                        yield encoder.encode(
                            ReasoningMessageStartEvent(
                                type=EventType.REASONING_MESSAGE_START,
                                message_id=reasoning_msg_id,
                                role="assistant",
                            )
                        )
                    yield encoder.encode(
                        ReasoningMessageContentEvent(
                            type=EventType.REASONING_MESSAGE_CONTENT,
                            message_id=reasoning_msg_id,
                            delta=text,
                        )
                    )

                elif content_type == "text":
                    text = getattr(content, "text", None)
                    if not text:
                        continue
                    # Close any open reasoning block before text
                    if reasoning_msg_id is not None:
                        yield encoder.encode(
                            ReasoningMessageEndEvent(
                                type=EventType.REASONING_MESSAGE_END,
                                message_id=reasoning_msg_id,
                            )
                        )
                        reasoning_msg_id = None
                    if msg_id is None:
                        msg_id = _generate_id()
                        yield encoder.encode(
                            TextMessageStartEvent(
                                type=EventType.TEXT_MESSAGE_START,
                                message_id=msg_id,
                                role="assistant",
                            )
                        )
                    yield encoder.encode(
                        TextMessageContentEvent(
                            type=EventType.TEXT_MESSAGE_CONTENT,
                            message_id=msg_id,
                            delta=text,
                        )
                    )

                elif content_type == "function_call":
                    # Close reasoning block before tool call
                    if reasoning_msg_id is not None:
                        yield encoder.encode(
                            ReasoningMessageEndEvent(
                                type=EventType.REASONING_MESSAGE_END,
                                message_id=reasoning_msg_id,
                            )
                        )
                        reasoning_msg_id = None

                    tc_id = getattr(content, "call_id", None) or _generate_id()
                    tc_name = getattr(content, "name", None)
                    if tc_name and tc_id != tool_call_id:
                        tool_call_id = tc_id
                        tc_name_current = tc_name
                        yield encoder.encode(
                            ToolCallStartEvent(
                                type=EventType.TOOL_CALL_START,
                                tool_call_id=tc_id,
                                tool_call_name=tc_name,
                                parent_message_id=msg_id,
                            )
                        )
                    tc_args = getattr(content, "arguments", None)
                    if tc_args:
                        delta = tc_args if isinstance(tc_args, str) else json.dumps(tc_args)
                        yield encoder.encode(
                            ToolCallArgsEvent(
                                type=EventType.TOOL_CALL_ARGS,
                                tool_call_id=tc_id,
                                delta=delta,
                            )
                        )

                elif content_type == "function_result":
                    tc_id = getattr(content, "call_id", None)
                    if tc_id:
                        yield encoder.encode(
                            ToolCallEndEvent(
                                type=EventType.TOOL_CALL_END,
                                tool_call_id=tc_id,
                            )
                        )
                        raw_result = getattr(content, "result", "") or ""
                        result_str = raw_result if isinstance(raw_result, str) else json.dumps(raw_result)
                        yield encoder.encode(
                            ToolCallResultEvent(
                                type=EventType.TOOL_CALL_RESULT,
                                message_id=_generate_id(),
                                tool_call_id=tc_id,
                                content=result_str,
                                role="tool",
                            )
                        )
                        # MCP Apps: check if this tool has UI resource (CTR-0067, PRP-0034)
                        from app.mcp_apps.manager import fetch_ui_resource, get_ui_tool_metadata, store_app_html

                        ui_meta = get_ui_tool_metadata(tc_name_current)
                        if ui_meta:
                            try:
                                # Find the MCP tool instance
                                from app.mcp.lifecycle import _mcp_server_status, _mcp_tools

                                mcp_tool = None
                                for idx, status in enumerate(_mcp_server_status):
                                    if status["name"] == ui_meta.server_name and idx < len(_mcp_tools):
                                        mcp_tool = _mcp_tools[idx]
                                        break

                                if mcp_tool:
                                    ui_resource = await fetch_ui_resource(mcp_tool, ui_meta.resource_uri)
                                    if ui_resource:
                                        ref_id = tc_id or _generate_id()
                                        html_filename = store_app_html(thread_id, ref_id, ui_resource.html)
                                        yield encoder.encode(
                                            CustomEvent(
                                                type=EventType.CUSTOM,
                                                name="mcp_app",
                                                value={
                                                    "server_name": ui_meta.server_name,
                                                    "tool_name": ui_meta.tool_name,
                                                    "resource_uri": ui_meta.resource_uri,
                                                    "html_ref": f"/api/mcp-apps/html/{thread_id}/{html_filename}",
                                                    "csp": ui_resource.csp,
                                                    "permissions": ui_resource.permissions,
                                                    "call_id": ref_id,
                                                },
                                            )
                                        )
                            except Exception:
                                logger.warning("Failed to fetch MCP App UI for %s", tc_name_current, exc_info=True)

                        tool_call_id = None
                        # Reset text message after tool result (allows new text block)
                        if msg_id is not None:
                            yield encoder.encode(
                                TextMessageEndEvent(
                                    type=EventType.TEXT_MESSAGE_END,
                                    message_id=msg_id,
                                )
                            )
                            msg_id = None

                elif content_type == "usage":
                    usage_details = getattr(content, "usage_details", None) or {}
                    usage_value = dict(usage_details)
                    model_name = selected_model or agent_registry.default_model
                    usage_value["max_context_tokens"] = settings.get_max_context_tokens(model_name)
                    usage_value["model"] = model_name
                    yield encoder.encode(
                        CustomEvent(
                            type=EventType.CUSTOM,
                            name="usage",
                            value=usage_value,
                        )
                    )

    except (OpenAINotFoundError, ChatClientException, TypeError) as exc:
        run_error = True
        if continuation_token:
            error_message = "Background response token not found or expired. Please resend your message."
            logger.warning("Continuation token error for thread %s: %s", thread_id, exc)
        else:
            error_message = "An internal error occurred during agent execution."
            logger.exception("AG-UI stream error")

    except Exception:
        run_error = True
        if continuation_token:
            error_message = "Background response token not found or expired. Please resend your message."
            logger.warning("Continuation token error for thread %s", thread_id, exc_info=True)
        else:
            error_message = "An internal error occurred during agent execution."
            logger.exception("AG-UI stream error")

    # Always finalize open blocks -- even after exceptions, so the frontend
    # can stop thinking indicators and display any error.
    if reasoning_msg_id is not None:
        yield encoder.encode(
            ReasoningMessageEndEvent(
                type=EventType.REASONING_MESSAGE_END,
                message_id=reasoning_msg_id,
            )
        )
    if msg_id is not None:
        yield encoder.encode(
            TextMessageEndEvent(
                type=EventType.TEXT_MESSAGE_END,
                message_id=msg_id,
            )
        )

    if run_error:
        yield encoder.encode(
            RunErrorEvent(
                type=EventType.RUN_ERROR,
                message=error_message,
            )
        )
    yield encoder.encode(
        RunFinishedEvent(
            type=EventType.RUN_FINISHED,
            run_id=run_id,
            thread_id=thread_id,
        )
    )


def register_agui_endpoints(app: FastAPI, *, agent_registry: AgentRegistry) -> None:
    """Register custom AG-UI endpoint with reasoning support (CTR-0009).

    Replaces add_agent_framework_fastapi_endpoint() to handle text_reasoning
    content that the upstream library skips. CORS is handled at app level.
    AgentRegistry is created by agent_factory (CTR-0070).
    """

    @app.post("/ag-ui/", tags=["AG-UI"], dependencies=[Depends(verify_api_key)])
    async def agui_endpoint(request_body: AGUIRequest):
        return StreamingResponse(
            _stream_with_reasoning(agent_registry, request_body),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    logger.info("AG-UI endpoint registered at /ag-ui/ (reasoning + session management)")
