"""OpenAI Responses API endpoint (CTR-0057, PRP-0030, PRP-0035).

Custom OpenAI-compatible Responses API on the main server.
Uses the AgentRegistry (CTR-0070) to route model parameter to the
corresponding Agent instance.
"""

from collections.abc import AsyncGenerator
import json
import logging
from typing import Any

from agent_framework import AgentSession, Message
from agent_framework.exceptions import ChatClientException
from agent_framework_ag_ui._agent_run import _normalize_response_stream
from agent_framework_ag_ui._message_adapters import normalize_agui_input_messages
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from openai import NotFoundError as OpenAINotFoundError

from app.agui.agent_registry import AgentRegistry
from app.auth import verify_api_key_strict
from app.image_gen.tools import current_thread_id as _image_gen_thread_id
from app.openai_api.converter import maf_contents_to_openai_output, openai_input_to_maf_messages
from app.openai_api.models import ResponsesRequest, ResponsesResponse, UsageInfo
from app.openai_api.session import (
    create_api_session,
    generate_response_id,
    resolve_thread_id,
    update_api_session,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["OpenAI API"])


def _build_session_message(role: str, text: str) -> dict[str, Any]:
    """Build a session-compatible message dict."""
    return {
        "type": "chat_message",
        "role": role,
        "contents": [{"type": "text", "text": text}],
    }


async def _stream_responses(
    agent_registry: AgentRegistry,
    request: ResponsesRequest,
    thread_id: str,
    response_id: str,
) -> AsyncGenerator[str, None]:
    """Stream OpenAI Responses API SSE events."""
    # Emit response.created
    created_event = {
        "type": "response.created",
        "response": {
            "id": response_id,
            "object": "response",
            "model": request.model,
            "output": [],
            "status": "in_progress",
        },
    }
    yield f"event: response.created\ndata: {json.dumps(created_event)}\n\n"

    # Select agent from registry based on model parameter (CTR-0070, PRP-0035)
    agent = agent_registry.get(request.model if request.model != "chatwalaau" else None)

    messages_raw = openai_input_to_maf_messages(request.input)
    messages, _ = normalize_agui_input_messages(messages_raw)

    session = AgentSession()
    session.metadata = {
        "ag_ui_thread_id": thread_id,
        "ag_ui_run_id": response_id,
    }

    run_options: dict[str, Any] = {}
    if request.temperature is not None:
        run_options["temperature"] = request.temperature
    if request.top_p is not None:
        run_options["top_p"] = request.top_p
    if request.max_output_tokens is not None:
        run_options["max_output_tokens"] = request.max_output_tokens

    _image_gen_thread_id.set(thread_id)

    output_items: list[dict[str, Any]] = []
    text_parts: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    msg_started = False

    try:
        # PRP-0067 / UDR-0043 D5: headless consumer auto-approve loop.
        # The OpenAI Responses API has no human-in-the-loop UI. If MAF
        # emits function_approval_request we approve it inline with a
        # WARNING log line so the audit trail survives in process logs,
        # then re-run with the appended approval_response. The loop is
        # bounded the same way as the AG-UI side to defend against
        # pathological tool chains.
        iteration_messages = list(messages)
        max_iter = 16
        completed_naturally = False
        for _ in range(max_iter):
            pending_responses = []
            response_stream = agent.run(
                iteration_messages,
                stream=True,
                session=session,
                options=run_options or None,
            )
            stream = await _normalize_response_stream(response_stream)

            async for update in stream:
                contents = getattr(update, "contents", None) or []
                for content in contents:
                    content_type = getattr(content, "type", None)

                    if content_type == "function_approval_request":
                        # Auto-approve. Log so a release-time audit can find it.
                        fn_call = getattr(content, "function_call", None)
                        tool_name = getattr(fn_call, "name", "<unknown>") if fn_call else "<unknown>"
                        call_id_log = getattr(fn_call, "call_id", "") if fn_call else ""
                        logger.warning(
                            "approval auto-granted for tool=%s by API consumer (call_id=%s)",
                            tool_name,
                            call_id_log,
                        )
                        pending_responses.append(content.to_function_approval_response(approved=True))

                    elif content_type == "text":
                        text = getattr(content, "text", "")
                        if not text:
                            continue
                        if not msg_started:
                            msg_started = True
                            item_added = {
                                "type": "response.output_item.added",
                                "output_index": len(output_items),
                                "item": {"type": "message", "role": "assistant", "content": []},
                            }
                            yield f"event: response.output_item.added\ndata: {json.dumps(item_added)}\n\n"
                            part_added = {
                                "type": "response.content_part.added",
                                "output_index": len(output_items),
                                "content_index": 0,
                                "part": {"type": "output_text", "text": ""},
                            }
                            yield f"event: response.content_part.added\ndata: {json.dumps(part_added)}\n\n"
                        text_parts.append(text)
                        delta_event = {
                            "type": "response.output_text.delta",
                            "output_index": len(output_items),
                            "content_index": 0,
                            "delta": text,
                        }
                        yield f"event: response.output_text.delta\ndata: {json.dumps(delta_event)}\n\n"

                    elif content_type == "function_call":
                        call_id = getattr(content, "call_id", "")
                        name = getattr(content, "name", "")
                        arguments = getattr(content, "arguments", "")
                        if isinstance(arguments, dict):
                            arguments = json.dumps(arguments)
                        if name:
                            fc_item = {
                                "type": "function_call",
                                "name": name,
                                "arguments": arguments or "",
                                "call_id": call_id,
                            }
                            output_items.append(fc_item)
                            fc_added = {
                                "type": "response.output_item.added",
                                "output_index": len(output_items) - 1,
                                "item": fc_item,
                            }
                            yield f"event: response.output_item.added\ndata: {json.dumps(fc_added)}\n\n"

                    elif content_type == "function_result":
                        call_id = getattr(content, "call_id", "")
                        result = getattr(content, "result", "")
                        if not isinstance(result, str):
                            result = json.dumps(result)
                        fr_item = {"type": "function_call_output", "call_id": call_id, "output": result}
                        output_items.append(fr_item)
                        fr_added = {
                            "type": "response.output_item.added",
                            "output_index": len(output_items) - 1,
                            "item": fr_item,
                        }
                        yield f"event: response.output_item.added\ndata: {json.dumps(fr_added)}\n\n"

                    elif content_type == "usage":
                        usage_details = getattr(content, "usage_details", None) or {}
                        usage["input_tokens"] = getattr(usage_details, "input_token_count", 0) or usage_details.get(
                            "input_token_count", 0
                        )
                        usage["output_tokens"] = getattr(usage_details, "output_token_count", 0) or usage_details.get(
                            "output_token_count", 0
                        )
                        usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]

            if not pending_responses:
                completed_naturally = True
                break

            iteration_messages = [*iteration_messages, Message(role="user", contents=pending_responses)]

        if not completed_naturally:
            logger.warning("OpenAI API approval loop exceeded %d iterations; returning partial response", max_iter)

    except (OpenAINotFoundError, ChatClientException, TypeError, Exception):
        logger.exception("OpenAI API stream error")
        error_data = {"type": "error", "message": "An internal error occurred during agent execution."}
        yield f"event: error\ndata: {json.dumps(error_data)}\n\n"

    # Finalize text message
    full_text = "".join(text_parts)
    if msg_started:
        msg_output_index = len(output_items)
        part_done = {
            "type": "response.content_part.done",
            "output_index": msg_output_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": full_text},
        }
        yield f"event: response.content_part.done\ndata: {json.dumps(part_done)}\n\n"

        msg_item = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": full_text}],
        }
        item_done = {
            "type": "response.output_item.done",
            "output_index": msg_output_index,
            "item": msg_item,
        }
        yield f"event: response.output_item.done\ndata: {json.dumps(item_done)}\n\n"
        output_items.append(msg_item)

    # Save session
    user_text = request.input if isinstance(request.input, str) else ""
    if not user_text and isinstance(request.input, list):
        for item in request.input:
            if item.get("role") == "user":
                user_text = item.get("content", "") if isinstance(item.get("content"), str) else ""
                break
    update_api_session(
        thread_id,
        response_id,
        _build_session_message("user", user_text),
        _build_session_message("assistant", full_text),
    )

    # Emit completed event
    completed = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "model": request.model,
            "output": output_items,
            "usage": usage,
            "status": "completed",
        },
    }
    yield f"event: response.completed\ndata: {json.dumps(completed)}\n\n"


def register_openai_api(app: FastAPI, *, agent_registry: AgentRegistry) -> None:
    """Register OpenAI-compatible Responses API endpoint (CTR-0057).

    Uses the AgentRegistry for model-based routing (CTR-0070).
    """

    @app.post("/v1/responses", tags=["OpenAI API"], dependencies=[Depends(verify_api_key_strict)])
    async def create_response(request: ResponsesRequest):
        """OpenAI Responses API-compatible endpoint."""
        # Resolve session
        response_id = generate_response_id()

        if request.previous_response_id:
            thread_id = resolve_thread_id(request.previous_response_id)
            if thread_id is None:
                raise HTTPException(
                    status_code=404, detail=f"Previous response not found: {request.previous_response_id}"
                )
        else:
            thread_id = response_id
            # Extract title from input
            title = ""
            if isinstance(request.input, str):
                title = request.input[:100]
            elif isinstance(request.input, list):
                for item in request.input:
                    if item.get("role") == "user":
                        c = item.get("content", "")
                        title = c[:100] if isinstance(c, str) else ""
                        break
            create_api_session(thread_id, response_id, title)

        if request.stream:
            return StreamingResponse(
                _stream_responses(agent_registry, request, thread_id, response_id),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # Non-streaming response
        agent = agent_registry.get(request.model if request.model != "chatwalaau" else None)

        messages_raw = openai_input_to_maf_messages(request.input)
        messages, _ = normalize_agui_input_messages(messages_raw)

        session = AgentSession()
        session.metadata = {
            "ag_ui_thread_id": thread_id,
            "ag_ui_run_id": response_id,
        }

        run_options: dict[str, Any] = {}
        if request.temperature is not None:
            run_options["temperature"] = request.temperature
        if request.top_p is not None:
            run_options["top_p"] = request.top_p
        if request.max_output_tokens is not None:
            run_options["max_output_tokens"] = request.max_output_tokens

        _image_gen_thread_id.set(thread_id)

        try:
            # PRP-0067 / UDR-0043 D5: non-streaming consumer auto-approve.
            # Same shape as the streaming branch: re-run agent.run() with
            # appended function_approval_response messages until no
            # approval request is left.
            iteration_messages = list(messages)
            max_iter = 16
            all_contents: list[Any] = []
            for _ in range(max_iter):
                pending_responses: list[Any] = []
                response_stream = agent.run(
                    iteration_messages,
                    stream=True,
                    session=session,
                    options=run_options or None,
                )
                stream = await _normalize_response_stream(response_stream)
                async for update in stream:
                    contents = getattr(update, "contents", None) or []
                    for content in contents:
                        if getattr(content, "type", None) == "function_approval_request":
                            fn_call = getattr(content, "function_call", None)
                            tool_name = getattr(fn_call, "name", "<unknown>") if fn_call else "<unknown>"
                            call_id_log = getattr(fn_call, "call_id", "") if fn_call else ""
                            logger.warning(
                                "approval auto-granted for tool=%s by API consumer (call_id=%s)",
                                tool_name,
                                call_id_log,
                            )
                            pending_responses.append(content.to_function_approval_response(approved=True))
                            continue
                        all_contents.append(content)
                if not pending_responses:
                    break
                iteration_messages = [*iteration_messages, Message(role="user", contents=pending_responses)]

        except (OpenAINotFoundError, ChatClientException, TypeError, Exception) as exc:
            logger.exception("OpenAI API error")
            raise HTTPException(status_code=500, detail="Agent execution failed.") from exc

        output_items, usage = maf_contents_to_openai_output(all_contents)

        # Save session
        full_text = ""
        for item in output_items:
            if item.get("type") == "message" and item.get("content"):
                for c in item["content"]:
                    if c.get("type") == "output_text":
                        full_text = c.get("text", "")
                        break

        user_text = request.input if isinstance(request.input, str) else ""
        if not user_text and isinstance(request.input, list):
            for item in request.input:
                if item.get("role") == "user":
                    user_text = item.get("content", "") if isinstance(item.get("content"), str) else ""
                    break
        update_api_session(
            thread_id,
            response_id,
            _build_session_message("user", user_text),
            _build_session_message("assistant", full_text),
        )

        return ResponsesResponse(
            id=response_id,
            model=request.model,
            output=[],  # Simplified: use raw dict for flexibility
            usage=UsageInfo(**usage),
        ).model_dump() | {"output": output_items}

    logger.info("OpenAI Responses API registered at /v1/responses")
