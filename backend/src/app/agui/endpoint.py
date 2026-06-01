"""AG-UI endpoint with reasoning event support (CTR-0009, PRP-0009).

Custom AG-UI streaming endpoint that replaces add_agent_framework_fastapi_endpoint()
to emit REASONING_* events for text_reasoning content. The upstream library's
_emit_content() skips text_reasoning; this endpoint handles it directly.

Session management is enabled via FileHistoryProvider (CTR-0014).
Agent selection is done via AgentRegistry using state.model (CTR-0070, PRP-0035).
Background Responses support via CTR-0045 (PRP-0025).

PRP-0067 / UDR-0043 (CTR-0009 v12): the endpoint also translates MAF's
``function_approval_request`` content into a CUSTOM
``tool_approval_request`` event, parks the stream on an
``asyncio.Event`` from ``app.agent.approval.approval_store``, and after
the operator POSTs to ``/api/tool-approval`` (or the timeout fires)
re-runs the agent with an appended ``function_approval_response``
message so MAF resumes execution / short-circuits with the rejection
text. The full handshake is ephemeral -- nothing about the approval
request/response is persisted to session JSON.
"""

import asyncio
from collections.abc import AsyncGenerator
import json
import logging
from pathlib import Path
from typing import Any, get_args
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

from app import providers
from app.agent.approval import (
    ApprovalRecord,
    approval_store,
    truncate_arguments_preview,
)
from app.agent.approval_iteration import IterationContentAccumulator
from app.agui.agent_registry import AgentRegistry
from app.auth import verify_api_key
from app.core.config import settings
from app.demo import is_demo_mode
from app.image_gen.tools import current_thread_id as _image_gen_thread_id

logger = logging.getLogger(__name__)

# ag-ui-protocol changed ReasoningMessageStartEvent.role from
# Literal["assistant"] (<= 0.1.13) to Literal["reasoning"] (newer releases).
# Reasoning models such as gpt-5.5 stream text_reasoning content, which hits
# this event; hardcoding either value breaks on the other version. Derive the
# accepted literal from the installed schema so the event always validates.
_REASONING_MESSAGE_ROLE = get_args(ReasoningMessageStartEvent.model_fields["role"].annotation)[0]


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


def _is_previous_response_not_found(exc: BaseException) -> bool:
    """Detect an Azure/OpenAI 400 ``previous_response_not_found`` in the chain.

    The Responses API raises this when a ``previous_response_id`` points at a
    response the service can no longer find -- e.g. an approval-interrupted
    response that was never committed server-side, server-side eviction, or a
    cross-resource reference. agent-framework wraps it in ChatClientException,
    so inspect the whole ``__cause__`` / ``__context__`` chain by error code
    and message text.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if getattr(cur, "code", None) == "previous_response_not_found":
            return True
        text = str(cur).lower()
        if "previous_response_not_found" in text or "previous response with id" in text:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _is_rate_limit_error(exc: BaseException) -> bool:
    """Detect an Azure/OpenAI 429 (rate limit) anywhere in the exception chain.

    A 429 may arrive at request start (typed openai.RateLimitError) or
    mid-stream (a plain openai.APIError carrying "Too Many Requests"), and
    agent-framework wraps it in ChatClientException, so inspect the whole
    __cause__ / __context__ chain by type name, status code, and message.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if type(cur).__name__ == "RateLimitError" or getattr(cur, "status_code", None) == 429:
            return True
        text = str(cur).lower()
        if "too many requests" in text or "rate limit" in text or "error code: 429" in text:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


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


def _arguments_to_dict(raw: Any) -> dict[str, Any]:
    """Best-effort decode of FunctionCallContent.arguments to a dict.

    MAF emits arguments either as a parsed dict or as a streaming JSON
    string; the approval preview wants a dict so the SPA can render
    structured fields.
    """
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {"_raw": raw}
        return decoded if isinstance(decoded, dict) else {"_value": decoded}
    return {}


async def _stream_with_reasoning(
    agent_registry: AgentRegistry,
    request_body: AGUIRequest,
) -> AsyncGenerator[str, None]:
    """Stream AG-UI events including REASONING_* for text_reasoning content.

    Iterates the MAF agent response stream directly to emit reasoning events
    that agent-framework-ag-ui's _emit_content() would otherwise skip.
    Selects the Agent from the registry based on state.model (CTR-0070).

    PRP-0067 (CTR-0009 v12): when MAF emits ``function_approval_request``
    content the loop translates it to a CUSTOM
    ``tool_approval_request`` event, parks on the approval store, and
    re-runs the agent with an appended ``function_approval_response``
    message once the approval is resolved (UDR-0043 D1+D6+D7).
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

        # Read model, reasoning, and background options from AG-UI state
        # (CTR-0070, CTR-0045, CTR-0009 reasoning PRP-0071)
        selected_model = None
        requested_reasoning = None
        background = False
        continuation_token = None
        if request_body.state:
            selected_model = request_body.state.get("model")
            requested_reasoning = request_body.state.get("reasoning")
            background = request_body.state.get("background", False)
            continuation_token = request_body.state.get("continuation_token")

        # Select agent from registry based on model (CTR-0070, PRP-0035)
        agent = agent_registry.get(selected_model)

        # Resolve the per-message reasoning effort against the owning provider's
        # catalog (PRP-0071, UDR-0047 D4). The resolved value is emitted in the
        # usage event regardless of provider; the per-request options are merged
        # only for live (non-demo) agents -- DemoChatClient ignores them.
        effective_model = selected_model or agent_registry.default_model
        resolved_reasoning = providers.resolve_effort(effective_model, requested_reasoning)

        # Validate continuation_token format: MAF expects dict with "response_id"
        if continuation_token and isinstance(continuation_token, str):
            continuation_token = {"response_id": continuation_token}

        run_options: dict[str, Any] = {}
        if background:
            run_options["background"] = True
        if continuation_token:
            run_options["continuation_token"] = continuation_token
        # Per-request reasoning options override the Agent's startup default_options
        # via MAF _merge_options (per key). Only when the client explicitly chose an
        # effort; otherwise the Agent's catalog-default options already apply.
        if requested_reasoning and not is_demo_mode():
            run_options.update(providers.build_model_options(effective_model, resolved_reasoning))

        # Set thread_id for image generation tools (CTR-0050, PRP-0027)
        _image_gen_thread_id.set(thread_id)

        # PRP-0067 approval loop. The first iteration runs the original
        # messages. If MAF emits function_approval_request contents, the
        # inner loop collects them, parks on the asyncio.Event for each,
        # then appends a fresh "user" message bundling the resolved
        # responses and re-runs the agent. The loop terminates when an
        # iteration finishes without producing any approval request.
        iteration_messages: list[Any] = list(messages)
        max_approval_iterations = 16
        logger.info(
            "AG-UI run start: thread=%s model=%s input_msgs=%d",
            thread_id,
            selected_model or "<default>",
            len(iteration_messages),
        )
        for _iteration in range(max_approval_iterations):
            pending_approvals: list[tuple[ApprovalRecord, Content]] = []
            # PRP-0069 follow-up: capture this iteration's stream content so
            # the next iteration's agent.run input pairs the originating
            # function_call (assistant role) with the approval response (user
            # role). Without it, MAF's Anthropic connector emits an orphan
            # tool_result -> HTTP 400. See app.agent.approval_iteration.
            iter_accumulator = IterationContentAccumulator()

            response_stream = agent.run(
                iteration_messages,
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
                        # PRP-0069 follow-up: capture each delta (and the
                        # Anthropic thinking signature) for iter N+1 reconstruction.
                        iter_accumulator.observe_text_reasoning(content)
                        text = getattr(content, "text", None)
                        if not text:
                            continue
                        if reasoning_msg_id is None:
                            reasoning_msg_id = _generate_id()
                            yield encoder.encode(
                                ReasoningMessageStartEvent(
                                    type=EventType.REASONING_MESSAGE_START,
                                    message_id=reasoning_msg_id,
                                    role=_REASONING_MESSAGE_ROLE,
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
                        # PRP-0069 follow-up: capture each delta for iter N+1.
                        iter_accumulator.observe_text(content)
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
                        # PRP-0069 follow-up: capture every non-gated function_call
                        # for iter N+1 so the model does not re-execute it (avoids
                        # an agent loop that exhausts Anthropic's rate limit).
                        iter_accumulator.observe_function_call(content)
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

                    elif content_type == "function_approval_request":
                        # PRP-0067 / UDR-0043 D1: MAF paused before executing
                        # this tool. Register the approval, emit a CUSTOM
                        # event with the parsed arguments preview, and
                        # accumulate the record so the post-stream phase
                        # parks on its asyncio.Event.
                        fn_call = getattr(content, "function_call", None)
                        if fn_call is None:
                            logger.warning("function_approval_request without function_call payload; skipping")
                            continue
                        # PRP-0069 follow-up: keep the originating function_call so
                        # iter N+1's input has the matching tool_use (Anthropic
                        # rejects orphan tool_results from approval responses).
                        iter_accumulator.observe_function_call_from_approval(fn_call)
                        tool_name_pending = getattr(fn_call, "name", "") or "<unknown>"
                        call_id_pending = getattr(fn_call, "call_id", "") or _generate_id()
                        raw_args = getattr(fn_call, "arguments", None)
                        full_args = _arguments_to_dict(raw_args)
                        preview_args = truncate_arguments_preview(full_args)
                        record = await approval_store.register(
                            thread_id=thread_id,
                            tool_name=tool_name_pending,
                            call_id=call_id_pending,
                            arguments_preview=preview_args,
                        )
                        # Check session-scoped cache (UDR-0043 D8). A hit
                        # bypasses the UI roundtrip: we resolve the record
                        # immediately with source="session-cache".
                        cached = await approval_store.lookup_session_cache(
                            thread_id=thread_id,
                            tool_name=tool_name_pending,
                        )
                        if cached is not None:
                            await approval_store.resolve(record.id, approved=cached, source="session-cache")
                        yield encoder.encode(
                            CustomEvent(
                                type=EventType.CUSTOM,
                                name="tool_approval_request",
                                value={
                                    "id": record.id,
                                    "call_id": call_id_pending,
                                    "tool_name": tool_name_pending,
                                    "arguments": preview_args,
                                    "expires_at_unix": record.expires_at,
                                    "cached_decision": cached,
                                },
                            )
                        )
                        pending_approvals.append((record, content))

                    elif content_type == "function_result":
                        # PRP-0069 follow-up: capture every executed result for
                        # iter N+1 (pairs with the matching function_call so
                        # Anthropic sees complete tool_use/tool_result pairs).
                        iter_accumulator.observe_function_result(content)
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
                        # Per-message reasoning effort (PRP-0071, CTR-0030). Echoed
                        # for every provider (incl. demo) so the SPA can label and
                        # persist it next to the model name.
                        usage_value["reasoning"] = resolved_reasoning
                        yield encoder.encode(
                            CustomEvent(
                                type=EventType.CUSTOM,
                                name="usage",
                                value=usage_value,
                            )
                        )

            # End of inner async-for. If no approvals are pending the agent
            # finished naturally and we exit the outer loop. Otherwise wait
            # on each pending record, build approval-response contents, and
            # append a fresh "user" message so MAF resumes on the next
            # iteration (PRP-0067 / UDR-0043 D1).
            if not pending_approvals:
                break

            approval_response_contents: list[Content] = []
            for record, request_content in pending_approvals:
                if record.resolution is None:
                    try:
                        await asyncio.wait_for(
                            record.event.wait(),
                            timeout=float(settings.tool_approval_timeout_sec),
                        )
                    except TimeoutError:
                        await approval_store.resolve(record.id, approved=False, source="timeout")
                resolution = record.resolution
                approved = bool(resolution and resolution.approved)
                source = resolution.source if resolution else "timeout"
                if source == "timeout":
                    logger.warning(
                        "approval timed out after %ds for tool=%s (call_id=%s, thread_id=%s)",
                        settings.tool_approval_timeout_sec,
                        record.tool_name,
                        record.call_id,
                        thread_id,
                    )
                # Emit the AG-UI tool_approval_response event so the SPA
                # collapses the inline card into a chip immediately, even
                # before MAF re-streams the function_result.
                yield encoder.encode(
                    CustomEvent(
                        type=EventType.CUSTOM,
                        name="tool_approval_response",
                        value={
                            "id": record.id,
                            "approved": approved,
                            "source": source,
                        },
                    )
                )
                # Build the MAF response content from the original request
                # so MAF can match by content.id on the next agent.run().
                approval_response_contents.append(request_content.to_function_approval_response(approved=approved))
                # The approval is fully resolved; the parked Event is no
                # longer needed. Drop the record so the GC sweeper has
                # less work to do.
                await approval_store.drop(record.id)

            # PRP-0069 follow-up: append BOTH the iter-N synthetic assistant
            # message (carrying the originating function_call(s) plus the
            # accumulated reasoning + text, with the Anthropic thinking signature
            # preserved) AND the user message bundling every approval response.
            # Without the synthetic assistant, MAF's Anthropic connector emits a
            # tool_result block with no preceding tool_use (HTTP 400 orphan
            # tool_result). OpenAI tolerated the missing context; Anthropic
            # enforces strict tool_use/tool_result pairing. The construction is
            # provider-agnostic -- OpenAI sees a structurally explicit context,
            # which is neutral or beneficial (no model behaviour change).
            iter_synthetic_messages = iter_accumulator.build_iteration_messages(approval_response_contents)
            iteration_messages = [*iteration_messages, *iter_synthetic_messages]
            # Reset server-side response chaining before the post-approval
            # re-run. The approval handshake interrupts the iteration-N
            # response stream, so the resp_... id MAF stored on the session
            # (store=True is OpenAIChatClient.STORES_BY_DEFAULT) may name a
            # response Azure never committed -> the next agent.run sends it as
            # previous_response_id and the Responses API returns 400
            # previous_response_not_found. The next iteration already carries
            # the full, explicit context (iteration_messages built above +
            # FileHistoryProvider history injected by before_run), so clearing
            # service_session_id makes the re-run rely on that explicit context
            # for every provider -- the same structurally-explicit path the
            # PRP-0069 follow-up established for Anthropic.
            session.service_session_id = None
        else:
            # The for-else fires when max_approval_iterations is exhausted
            # without a `break`. Defensive: emit a RUN_ERROR so the SPA
            # surfaces the run as failed instead of silently terminating.
            run_error = True
            error_message = f"Tool approval loop exceeded {max_approval_iterations} rounds; aborting run."
            logger.warning("approval loop exceeded for thread %s", thread_id)

    except (OpenAINotFoundError, ChatClientException, TypeError) as exc:
        run_error = True
        if continuation_token:
            error_message = "Background response token not found or expired. Please resend your message."
            logger.warning("Continuation token error for thread %s: %s", thread_id, exc)
        elif _is_previous_response_not_found(exc):
            error_message = (
                "The conversation's server-side response reference expired or "
                "was not found. Please resend your message to continue."
            )
            logger.warning("AG-UI previous_response_not_found for thread %s: %s", thread_id, exc)
        elif _is_rate_limit_error(exc):
            error_message = (
                "Rate limited by the model provider (429 Too Many Requests). "
                "A high reasoning effort generates many tokens quickly and is the "
                "most common trigger for the Azure OpenAI per-minute (TPM) limit. "
                "Please wait a moment and retry, lower the reasoning effort, or "
                "raise the deployment quota."
            )
            logger.warning("AG-UI stream rate limited (429) for thread %s: %s", thread_id, exc)
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
        logger.warning("AG-UI run finished with error: thread=%s message=%s", thread_id, error_message)
        yield encoder.encode(
            RunErrorEvent(
                type=EventType.RUN_ERROR,
                message=error_message,
            )
        )
    else:
        logger.info("AG-UI run finished normally: thread=%s", thread_id)
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
