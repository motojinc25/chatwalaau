"""OpenAI Responses API endpoint (CTR-0057, PRP-0030, PRP-0035, PRP-0252).

Custom OpenAI-compatible Responses API on the main server.
Uses the AgentRegistry (CTR-0070) to route model parameter to the
corresponding Agent instance. The operator controls of PRP-0252 / UDR-0233 (output
cap, tool output mode, input role policy) live in app.openai_api.controls; the admission
limits of PRP-0254 / UDR-0235 (requests, tokens, concurrency, input size) live in
app.openai_api.quota.

PRP-0256 / UDR-0237: a key bound to a declarative agent runs that agent
(app.openai_api.bound_agent, CTR-0286) and never the registry fallback; its strict
grounding retrieves before any model call (app.openai_api.grounding, CTR-0287); a key may
force temporary storage, temporary threads chain, and an unknown model can be refused.
"""

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
import json
import logging
from typing import Any

from agent_framework import AgentSession
from agent_framework.exceptions import ChatClientException
from agent_framework_ag_ui._agent_run import _normalize_response_stream
from agent_framework_ag_ui._message_adapters import normalize_agui_input_messages
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from openai import NotFoundError as OpenAINotFoundError
from starlette.background import BackgroundTask

from app import providers
from app.agent.stream_merge import ArgumentFold, TextFold, UsageFold, fold_contents, is_refusal, merge_mode
from app.agent.temporary import is_temporary, new_temporary_thread_id, schedule_sweep, set_temporary_run
from app.agui.agent_registry import AgentRegistry
from app.auth import Identity, verify_api_key_strict
from app.image_gen.tools import current_thread_id as _image_gen_thread_id
from app.openai_api import bound_agent, controls, grounding, quota
from app.openai_api import session as api_session
from app.openai_api.converter import api_usage, maf_contents_to_openai_output, openai_input_to_maf_messages
from app.openai_api.models import ResponsesRequest, ResponsesResponse, UsageInfo
from app.openai_api.session import (
    create_api_session,
    generate_response_id,
    resolve_thread_id,
    update_api_session,
)

logger = logging.getLogger(__name__)


def _accumulate_usage_contents(contents: Any) -> tuple[dict[str, Any] | None, int]:
    """The turn total and model-call count of ``contents`` (CTR-0200, PRP-0158).

    The non-streaming lane holds the whole content list, so the turn total is a
    fold over it -- the CTR-0266 fold (PRP-0229, UDR-0212 D3.3), because since MAF
    1.22.0 the second and later calls of a tool loop report a CUMULATIVE `replace`.
    `maf_contents_to_openai_output` deliberately keeps its own last-call semantics for
    the API RESPONSE; this is the ledger's view of the same contents.
    """
    usage = fold_contents(contents or []).usage
    return usage.turn, usage.calls


def _turn_summary(turn_usage: dict[str, Any] | None, model_calls: int, model: str) -> dict[str, Any] | None:
    """The ledger-shaped turn total (price-point breakdown), or None when nothing was measured."""
    if not turn_usage or model_calls <= 0:
        return None
    try:
        from app.agui.token_usage import turn_summary

        return dict(
            turn_summary(
                turn_usage,
                model_calls=model_calls,
                includes_cache_read=providers.input_tokens_include_cache_read(model),
            )
        )
    except Exception:
        logger.warning("openai-api turn summary failed", exc_info=True)
        return None


def _record_api_turn(
    summary: dict[str, Any] | None,
    requested_model: str,
    thread_id: str,
    *,
    temporary: bool,
    key_id: str | None = None,
) -> None:
    """Append an OpenAI-compatible turn to the CTR-0200 ledger (PRP-0158).

    This lane writes no browsable chat, so its consumption reached no record at all
    before now. Self-silencing: statistics may never fail a request (UDR-0136 D6).
    ``key_id`` is the managed key's public id (PRP-0254, UDR-0235 D9).
    """
    if not summary:
        return
    try:
        from app.usage.ledger import append_turn_usage

        append_turn_usage(
            lane="openai-api",
            turn=summary,
            model=requested_model,
            thread_id=thread_id,
            temporary=temporary,
            key_id=key_id,
        )
    except Exception:
        logger.warning("usage ledger openai-api append failed for thread %s", thread_id, exc_info=True)


router = APIRouter(prefix="/v1", tags=["OpenAI API"])


def _build_session_message(role: str, text: str) -> dict[str, Any]:
    """Build a session-compatible message dict."""
    from app.session.provider import MESSAGE_TYPE_ID

    return {
        # DERIVED from the installed MAF, never a literal: the id was renamed
        # chat_message -> message between 1.10 and 1.13, and a literal here wrote
        # sessions that Message.from_dict() rejected on the next turn (PRP-0126).
        "type": MESSAGE_TYPE_ID,
        "role": role,
        "contents": [{"type": "text", "text": text}],
    }


def _structured_run_options(
    request: ResponsesRequest, agent_registry: AgentRegistry, model_override: str | None = None
) -> dict[str, Any]:
    """Resolve the standard OpenAI `text.format` field into a provider-native
    structured-output run-options fragment (CTR-0057 v3, PRP-0082, UDR-0058 D8).

    Honors the standard field shape (`text.format.{type,schema,...}`) and routes it
    through the same CTR-0102 seam as the SPA path. Returns {} when absent / off.
    Never raises.
    """
    text = getattr(request, "text", None)
    fmt = text.get("format") if isinstance(text, dict) else None
    if not isinstance(fmt, dict):
        return {}
    model = model_override or (request.model if request.model != "chatwalaau" else agent_registry.default_model)
    ftype = fmt.get("type")
    if ftype == "json_schema":
        schema = fmt.get("schema")
        if not isinstance(schema, dict):
            # tolerate the nested {json_schema: {schema: ...}} variant
            nested = fmt.get("json_schema")
            schema = nested.get("schema") if isinstance(nested, dict) else None
        return providers.build_structured_output(model, schema if isinstance(schema, dict) else None, "json_schema")
    if ftype == "json_object":
        return providers.build_structured_output(model, None, "json_object")
    return {}


#: Fixed texts (English, PRP-0256).
STORAGE_MISMATCH = "previous_response_id belongs to a saved conversation; a temporary request cannot continue it."


@dataclass
class RunPlan:
    """What one admitted request runs (PRP-0256): which agent, which instructions, and the
    grounding retrieval when the bound agent is strict. ``agent`` is None for a no-match
    answer, which calls no model (UDR-0237 D6)."""

    agent: Any
    #: The model the structured-output fragment is built for (None = from request.model).
    model: str | None
    instructions: str | None
    temporary: bool
    retrieval: grounding.Retrieval | None = None

    @property
    def no_match(self) -> bool:
        return self.retrieval is not None and not self.retrieval.found


def _api_error(status: int, code: str, message: str, error_type: str = "invalid_request_error") -> JSONResponse:
    """A refusal in the OpenAI error shape (the PRP-0254 429 shape)."""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": error_type, "code": code, "param": None}},
    )


def _user_text(request: ResponsesRequest) -> str:
    user_text = request.input if isinstance(request.input, str) else ""
    if not user_text and isinstance(request.input, list):
        for item in request.input:
            if item.get("role") == "user":
                user_text = item.get("content", "") if isinstance(item.get("content"), str) else ""
                break
    return user_text


def _grounding_metadata(plan: RunPlan, answer: str) -> dict[str, Any]:
    """``{"metadata": {"grounding": ...}}`` for a strict run, else {} (byte-identical default)."""
    if plan.retrieval is None:
        return {}
    return {"metadata": {"grounding": grounding.final_status(plan.retrieval, answer)}}


def _citation_items(plan: RunPlan) -> list[dict[str, Any]]:
    return grounding.citation_items(plan.retrieval) if plan.retrieval is not None else []


_api_sweep_tasks: set[asyncio.Task[int]] = set()


def _maybe_sweep_api_sessions() -> None:
    """Opportunistic retention sweep, at most hourly, off the loop (UDR-0237 D10)."""
    if not api_session.sweep_due():
        return
    task = asyncio.create_task(asyncio.to_thread(api_session.sweep_now))
    _api_sweep_tasks.add(task)
    task.add_done_callback(_api_sweep_tasks.discard)


def _sse(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


def _no_match_text_part() -> dict[str, Any]:
    text_part: dict[str, Any] = {"type": "output_text", "text": grounding.NO_MATCH_ANSWER}
    if controls.tool_output_mode() == "sources":
        text_part["annotations"] = []
    return text_part


async def _stream_no_match(
    request: ResponsesRequest,
    thread_id: str,
    response_id: str,
    plan: RunPlan,
    ticket: quota.Ticket | None = None,
) -> AsyncGenerator[str, None]:
    """The fixed no-match answer as a Responses SSE stream; no model call (UDR-0237 D6)."""
    answer = grounding.NO_MATCH_ANSWER
    base = {"id": response_id, "object": "response", "model": request.model}
    yield _sse({"type": "response.created", "response": base | {"output": [], "status": "in_progress"}})
    yield _sse(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "message", "role": "assistant", "content": []},
        }
    )
    yield _sse(
        {
            "type": "response.content_part.added",
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": ""},
        }
    )
    yield _sse({"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": answer})
    text_part = _no_match_text_part()
    yield _sse({"type": "response.content_part.done", "output_index": 0, "content_index": 0, "part": text_part})
    msg_item = {"type": "message", "role": "assistant", "content": [text_part]}
    yield _sse({"type": "response.output_item.done", "output_index": 0, "item": msg_item})
    update_api_session(
        thread_id,
        response_id,
        _build_session_message("user", _user_text(request)),
        _build_session_message("assistant", answer),
    )
    if ticket is not None:
        ticket.finish(None)  # no model call: no tokens, no ledger record
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    completed = base | {"output": [msg_item], "usage": usage, "status": "completed"} | _grounding_metadata(plan, answer)
    yield _sse({"type": "response.completed", "response": completed})


def _no_match_response(
    request: ResponsesRequest, thread_id: str, response_id: str, plan: RunPlan, ticket: quota.Ticket
) -> dict[str, Any]:
    """The fixed no-match answer, non-streaming; no model call (UDR-0237 D6)."""
    answer = grounding.NO_MATCH_ANSWER
    update_api_session(
        thread_id,
        response_id,
        _build_session_message("user", _user_text(request)),
        _build_session_message("assistant", answer),
    )
    ticket.finish(None)
    output_items = [{"type": "message", "role": "assistant", "content": [_no_match_text_part()]}]
    return (
        ResponsesResponse(id=response_id, model=request.model, output=[], usage=UsageInfo()).model_dump()
        | {"output": output_items}
        | _grounding_metadata(plan, answer)
    )


async def _stream_responses(
    agent_registry: AgentRegistry,
    request: ResponsesRequest,
    thread_id: str,
    response_id: str,
    ticket: quota.Ticket | None = None,
    plan: RunPlan | None = None,
) -> AsyncGenerator[str, None]:
    """Stream OpenAI Responses API SSE events.

    ``ticket`` is the CTR-0285 admission; it sees each turn total and is finished before
    the ledger append (UDR-0235 D7). The caller also finishes it on every other path.
    ``plan`` (PRP-0256) names the agent and instructions; None means the registry agent
    for ``request.model``, as before.
    """
    # Temporary Chat (CTR-0057 / CTR-0106, UDR-0052): set inside the generator so
    # the contextvar is active in the same context that runs agent.run (the
    # memory tool reads it to no-op). request.temporary defaults false.
    if plan is None:
        plan = _registry_plan(agent_registry, request, bool(getattr(request, "temporary", False)))
    temporary = plan.temporary
    set_temporary_run(temporary)
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

    # The agent of the plan: the registry agent for request.model (CTR-0070) or the
    # key-bound agent (CTR-0286).
    agent = plan.agent

    # Token Usage Ledger accumulation (CTR-0200, PRP-0158) over every model call of the
    # run below, so the total spans the whole turn. Folded by CTR-0266 (PRP-0229,
    # UDR-0212 D3), like the deltas: MAF 1.22.0 streams `merge_mode`.
    usage_fold = UsageFold()
    text_fold = TextFold("text")
    argument_fold = ArgumentFold()
    call_items: dict[str, dict[str, Any]] = {}
    turn_usage: dict[str, Any] | None = None
    model_calls = 0

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
    # Operator cap (CTR-0057 v5, PRP-0252, UDR-0233 D2): min(requested, cap).
    max_output_tokens = controls.effective_max_output_tokens(request.max_output_tokens)
    if max_output_tokens is not None:
        run_options["max_output_tokens"] = max_output_tokens
    # Structured output passthrough (CTR-0057 v3, PRP-0082, UDR-0058 D8).
    providers.merge_generation_options(run_options, _structured_run_options(request, agent_registry, plan.model))

    # User Preference Memory (PRP-0075, CTR-0105): the registry agents bake only
    # the Identity, so supply the capability remainder per run. The frozen User
    # Profile snapshot is the AG-UI session path and is NOT injected here
    # (UDR-0051 D10) -> user_profile_block omitted. For a Temporary Chat run the
    # remainder is empty (Identity-only, UDR-0052 D6). The plan also carries the
    # grounding excerpts of a strict bound agent (CTR-0287).
    if plan.instructions:
        run_options["instructions"] = plan.instructions

    _image_gen_thread_id.set(thread_id)

    output_items: list[dict[str, Any]] = []
    # Tool output mode (PRP-0252, UDR-0233 D3): every tool item is kept here for the
    # citations; it reaches output_items and the wire only in `full`.
    tool_mode = controls.tool_output_mode()
    # Server grounding hits feed the sources-mode citations like rag_search results
    # (CTR-0287); they never become output items.
    tool_items: list[dict[str, Any]] = list(_citation_items(plan))
    text_parts: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    msg_started = False

    try:
        # ONE run per response (PRP-0179, UDR-0161 D2): no tool is approval-gated, so
        # the former headless auto-approve loop (UDR-0043 D5) is gone.
        unexpected_approval_tool: str | None = None
        response_stream = agent.run(
            messages,
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
                    # UDR-0161 D3: no tool is approval-gated since PRP-0179, so this is a
                    # construction defect. It is not auto-approved (the old headless
                    # behaviour) -- the response ends with a named error.
                    fn_call = getattr(content, "function_call", None)
                    unexpected_approval_tool = getattr(fn_call, "name", "") or "<unknown>"
                    logger.error(
                        "Tool %r asked for approval (call_id=%s) on the OpenAI API lane, but no "
                        "tool is approval-gated since PRP-0179 (UDR-0161 D3).",
                        unexpected_approval_tool,
                        getattr(fn_call, "call_id", None),
                    )
                    break

                if content_type == "text":
                    text = text_fold.feed(
                        getattr(content, "text", ""), merge_mode(content), refusal=is_refusal(content)
                    )
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
                    text_fold.reset()
                    call_id = getattr(content, "call_id", "")
                    name = getattr(content, "name", "")
                    # One item per call (UDR-0212 D3.2): later fragments -- argument
                    # deltas and the full-argument `replace` -- complete its arguments.
                    argument_fold.feed(call_id, getattr(content, "arguments", None), merge_mode(content))
                    arguments = argument_fold.shown(call_id)
                    if call_id in call_items:
                        call_items[call_id]["arguments"] = arguments
                    elif name:
                        fc_item = {
                            "type": "function_call",
                            "name": name,
                            "arguments": arguments or "",
                            "call_id": call_id,
                        }
                        call_items[call_id] = fc_item
                        tool_items.append(fc_item)
                        if tool_mode == "full":
                            output_items.append(fc_item)
                            fc_added = {
                                "type": "response.output_item.added",
                                "output_index": len(output_items) - 1,
                                "item": fc_item,
                            }
                            yield f"event: response.output_item.added\ndata: {json.dumps(fc_added)}\n\n"

                elif content_type == "function_result":
                    usage_fold.call_boundary()
                    text_fold.reset()
                    call_id = getattr(content, "call_id", "")
                    result = getattr(content, "result", "")
                    if not isinstance(result, str):
                        result = json.dumps(result)
                    fr_item = {"type": "function_call_output", "call_id": call_id, "output": result}
                    tool_items.append(fr_item)
                    if tool_mode == "full":
                        output_items.append(fr_item)
                        fr_added = {
                            "type": "response.output_item.added",
                            "output_index": len(output_items) - 1,
                            "item": fr_item,
                        }
                        yield f"event: response.output_item.added\ndata: {json.dumps(fr_added)}\n\n"

                elif content_type == "usage":
                    # CTR-0200 (PRP-0158, UDR-0136 D5). The `usage` dict below keeps
                    # its existing last-call semantics -- changing this lane's
                    # RESPONSE shape is a wire change nobody approved -- so the
                    # ledger gets the turn total, folded the same way CTR-0009 folds
                    # it (CTR-0266).
                    usage_fold.observe(getattr(content, "usage_details", None), merge_mode(content))
                    turn_usage = usage_fold.turn
                    model_calls = usage_fold.calls
                    usage = api_usage(usage_fold.last_call)
                    if ticket is not None:
                        ticket.observe(_turn_summary(turn_usage, model_calls, request.model))
            if unexpected_approval_tool is not None:
                break

        if unexpected_approval_tool is not None:
            error_data = {
                "type": "error",
                "message": (
                    f"Tool '{unexpected_approval_tool}' asked for approval, which this version no "
                    "longer supports. This is a defect -- please report it."
                ),
            }
            yield f"event: error\ndata: {json.dumps(error_data)}\n\n"

    except (OpenAINotFoundError, ChatClientException, TypeError, Exception):
        logger.exception("OpenAI API stream error")
        error_data = {"type": "error", "message": "An internal error occurred during agent execution."}
        yield f"event: error\ndata: {json.dumps(error_data)}\n\n"

    # Finalize text message
    full_text = "".join(text_parts)
    if msg_started:
        msg_output_index = len(output_items)
        text_part: dict[str, Any] = {"type": "output_text", "text": full_text}
        if tool_mode == "sources":
            # rag_search retrievals as file_citation annotations (UDR-0233 D3, D8).
            text_part["annotations"] = controls.rag_citations(tool_items, full_text)
        part_done = {
            "type": "response.content_part.done",
            "output_index": msg_output_index,
            "content_index": 0,
            "part": text_part,
        }
        yield f"event: response.content_part.done\ndata: {json.dumps(part_done)}\n\n"

        msg_item = {
            "type": "message",
            "role": "assistant",
            "content": [text_part],
        }
        item_done = {
            "type": "response.output_item.done",
            "output_index": msg_output_index,
            "item": msg_item,
        }
        yield f"event: response.output_item.done\ndata: {json.dumps(item_done)}\n\n"
        output_items.append(msg_item)

    # Save session
    update_api_session(
        thread_id,
        response_id,
        _build_session_message("user", _user_text(request)),
        _build_session_message("assistant", full_text),
    )

    # CTR-0200 append (PRP-0158, UDR-0136 D5). This lane writes no chat the operator
    # can browse, so before now its consumption was recorded nowhere at all. The
    # CTR-0285 counters take the turn FIRST, so a failed append cannot skip them.
    summary = _turn_summary(turn_usage, model_calls, request.model)
    if ticket is not None:
        ticket.finish(summary)
    _record_api_turn(summary, request.model, thread_id, temporary=temporary, key_id=ticket.key_id if ticket else None)

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
        }
        | _grounding_metadata(plan, full_text),
    }
    yield f"event: response.completed\ndata: {json.dumps(completed)}\n\n"


def _registry_plan(agent_registry: AgentRegistry, request: ResponsesRequest, temporary: bool) -> RunPlan:
    """The plan of an unbound request: the registry agent for request.model, as before."""
    model = request.model if request.model != "chatwalaau" else None
    return RunPlan(
        agent=agent_registry.get(model),
        model=None,
        instructions=agent_registry.run_instructions(model, temporary=temporary),
        temporary=temporary,
    )


async def _bound_plan(
    agent_registry: AgentRegistry, spec: Any, request: ResponsesRequest, temporary: bool, prior_thread_id: str | None
) -> RunPlan:
    """The plan of a key-bound request (CTR-0286), with the strict retrieval (CTR-0287)."""
    bound = bound_agent.get_bound_agent(spec, getattr(agent_registry, "generation", 0))
    # A temporary run stays de-personalized: no capability remainder (UDR-0052 D6).
    instructions = None if temporary else (bound.capabilities or None)
    retrieval = None
    if bound.knowledge is not None and bound.knowledge.strict:
        query = grounding.retrieval_query(request.input, prior_thread_id)
        retrieval = await run_in_threadpool(grounding.retrieve, bound.knowledge, query)
        if not retrieval.found:
            logger.info("Grounding: no match for agent %s; answered without a model call", bound.agent_id)
            return RunPlan(agent=None, model=bound.model, instructions=None, temporary=temporary, retrieval=retrieval)
        block = grounding.excerpts_instruction(retrieval)
        instructions = f"{instructions}\n\n{block}" if instructions else block
    return RunPlan(
        agent=bound.agent, model=bound.model, instructions=instructions, temporary=temporary, retrieval=retrieval
    )


async def _guarded_stream(inner: AsyncGenerator[str, None], ticket: quota.Ticket) -> AsyncGenerator[str, None]:
    """Release the admission however the stream ends: completion, error or disconnect."""
    try:
        async for chunk in inner:
            yield chunk
    finally:
        ticket.finish()
        await inner.aclose()


def _quota_response(exc: quota.QuotaError) -> JSONResponse:
    """The OpenAI error shape for a refusal (UDR-0235 D8)."""
    return JSONResponse(status_code=exc.status, content=exc.body(), headers=exc.headers())


def _key_record(identity: Identity | None) -> Any:
    key_id = getattr(identity, "key_id", None)
    if not key_id:
        return None
    try:
        from app.api_keys.store import get_store

        return get_store().get(key_id)
    except Exception:
        return None


def register_openai_api(app: FastAPI, *, agent_registry: AgentRegistry) -> None:
    """Register OpenAI-compatible Responses API endpoint (CTR-0057).

    Uses the AgentRegistry for model-based routing (CTR-0070).
    """

    def _known_model(model: str) -> bool:
        return model == bound_agent.GENERIC_MODEL or model in agent_registry.available_models

    @app.post("/v1/responses", tags=["OpenAI API"])
    async def create_response(request: ResponsesRequest, identity: Identity = Depends(verify_api_key_strict)):
        """OpenAI Responses API-compatible endpoint."""
        # Input role policy (PRP-0252, UDR-0233 D5): reject before any session exists.
        controls.check_input_roles(request.input)

        # Key-bound agent (CTR-0286, PRP-0256, UDR-0237 D1): resolved before admission, so
        # a refusal consumes nothing. An unbound key keeps the registry routing; it may
        # refuse an unknown model when the operator turned that on (D2).
        record = _key_record(identity)
        bound_id = getattr(record, "agent", None)
        spec = None
        try:
            if bound_id:
                bound_agent.check_model(request.model, bound_id)
                spec = bound_agent.resolve_spec_for_key(bound_id)
            elif controls.reject_unknown_model() and not _known_model(request.model):
                return _api_error(400, "model_not_found", f"Unknown model {request.model!r}.")
        except bound_agent.BoundAgentError as exc:
            return JSONResponse(status_code=exc.status, content=exc.body())

        # Storage (UDR-0237 D8): a temporary key forces temporary; a thread's storage is
        # fixed by its first turn, so a temporary thread continues temporary and a
        # temporary request cannot continue a saved thread.
        temporary = bool(request.temporary) or getattr(record, "storage", "saved") == "temporary"

        # Resolve session
        response_id = generate_response_id()

        if request.previous_response_id:
            thread_id = resolve_thread_id(request.previous_response_id)
            if thread_id is None:
                raise HTTPException(
                    status_code=404, detail=f"Previous response not found: {request.previous_response_id}"
                )
            if is_temporary(thread_id):
                temporary = True
            elif temporary:
                return _api_error(400, "storage_mismatch", STORAGE_MISMATCH)

        # Admission (CTR-0285, PRP-0254, UDR-0235): input size, then the limits. A refusal
        # runs nothing and creates no session.
        try:
            quota.check_input_size(request.input, thread_id if request.previous_response_id else None)
            ticket = quota.guard().admit(getattr(identity, "key_id", None), _key_record(identity))
        except quota.QuotaError as exc:
            return _quota_response(exc)

        try:
            prior = thread_id if request.previous_response_id else None
            return await _run_admitted(request, ticket, response_id, temporary, prior, spec)
        except BaseException:
            ticket.finish()
            raise

    async def _run_admitted(
        request: ResponsesRequest,
        ticket: quota.Ticket,
        response_id: str,
        temporary: bool,
        prior_thread_id: str | None,
        spec: Any = None,
    ) -> Any:
        """Everything after admission. The ticket is finished on every path."""
        # The plan first: a bound agent that cannot be built refuses before a session exists.
        if spec is not None:
            try:
                plan = await _bound_plan(agent_registry, spec, request, temporary, prior_thread_id)
            except bound_agent.BoundAgentError as exc:
                ticket.finish()
                return JSONResponse(status_code=exc.status, content=exc.body())
        else:
            plan = None
        _maybe_sweep_api_sessions()

        if prior_thread_id:
            thread_id = prior_thread_id
        else:
            # Temporary: a temp_ thread id routes the session to the .temporary/
            # quarantine (CTR-0014) so it is excluded from listing/search and
            # swept by retention; otherwise the response_id is the thread id.
            thread_id = new_temporary_thread_id() if temporary else response_id
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
            if temporary:
                # Opportunistic retention sweep (UDR-0052 D4); fire-and-forget.
                schedule_sweep()

        if plan is not None and plan.no_match:
            # Strict grounding found nothing: the fixed answer, no model call (UDR-0237 D6).
            if request.stream:
                return StreamingResponse(
                    _guarded_stream(_stream_no_match(request, thread_id, response_id, plan, ticket), ticket),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
                    background=BackgroundTask(ticket.finish),
                )
            return _no_match_response(request, thread_id, response_id, plan, ticket)

        if plan is None:
            plan = _registry_plan(agent_registry, request, temporary)

        if request.stream:
            return StreamingResponse(
                _guarded_stream(
                    _stream_responses(agent_registry, request, thread_id, response_id, ticket, plan), ticket
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
                # A stream that never starts (the client left first) still releases the slot.
                background=BackgroundTask(ticket.finish),
            )

        # Non-streaming response: the plan's agent (registry or key-bound).
        agent = plan.agent

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
        # Operator cap (CTR-0057 v5, PRP-0252, UDR-0233 D2): min(requested, cap).
        max_output_tokens = controls.effective_max_output_tokens(request.max_output_tokens)
        if max_output_tokens is not None:
            run_options["max_output_tokens"] = max_output_tokens
        # Structured output passthrough (CTR-0057 v3, PRP-0082, UDR-0058 D8).
        providers.merge_generation_options(run_options, _structured_run_options(request, agent_registry, plan.model))

        # User Preference Memory (PRP-0075, CTR-0105): supply the capability
        # remainder per run under Identity-only baking; no frozen snapshot here
        # (AG-UI session path only, UDR-0051 D10). For a Temporary Chat run the
        # remainder is empty (Identity-only, UDR-0052 D6) and the memory tool
        # no-ops via the contextvar set below. The plan carries it (and the grounding
        # excerpts of a strict bound agent, CTR-0287).
        set_temporary_run(temporary)
        if plan.instructions:
            run_options["instructions"] = plan.instructions

        _image_gen_thread_id.set(thread_id)

        try:
            # ONE run (PRP-0179, UDR-0161 D2): no tool is approval-gated, so the former
            # headless auto-approve loop (UDR-0043 D5) is gone.
            all_contents: list[Any] = []
            unexpected_approval_tool: str | None = None
            response_stream = agent.run(
                messages,
                stream=True,
                session=session,
                options=run_options or None,
            )
            stream = await _normalize_response_stream(response_stream)
            async for update in stream:
                contents = getattr(update, "contents", None) or []
                for content in contents:
                    if getattr(content, "type", None) == "function_approval_request":
                        # UDR-0161 D3: a construction defect, surfaced by name.
                        fn_call = getattr(content, "function_call", None)
                        unexpected_approval_tool = getattr(fn_call, "name", "") or "<unknown>"
                        logger.error(
                            "Tool %r asked for approval (call_id=%s) on the OpenAI API lane, but no "
                            "tool is approval-gated since PRP-0179 (UDR-0161 D3).",
                            unexpected_approval_tool,
                            getattr(fn_call, "call_id", None),
                        )
                        break
                    all_contents.append(content)
                if unexpected_approval_tool is not None:
                    break

        except (OpenAINotFoundError, ChatClientException, TypeError, Exception) as exc:
            logger.exception("OpenAI API error")
            ticket.finish()
            raise HTTPException(status_code=500, detail="Agent execution failed.") from exc

        if unexpected_approval_tool is not None:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Tool '{unexpected_approval_tool}' asked for approval, which this version no "
                    "longer supports. This is a defect -- please report it."
                ),
            )

        output_items, usage = maf_contents_to_openai_output(all_contents)
        # CTR-0200 append (PRP-0158, UDR-0136 D5). Folded over the same contents the
        # converter reads, so the ledger records the TURN while the response keeps
        # its existing field semantics.
        _ledger_turn, _ledger_calls = _accumulate_usage_contents(all_contents)
        summary = _turn_summary(_ledger_turn, _ledger_calls, request.model)
        ticket.finish(summary)  # CTR-0285 first, independent of the append (UDR-0235 D7)
        _record_api_turn(summary, request.model, thread_id, temporary=temporary, key_id=ticket.key_id)

        # Save session
        full_text = ""
        for item in output_items:
            if item.get("type") == "message" and item.get("content"):
                for c in item["content"]:
                    if c.get("type") == "output_text":
                        full_text = c.get("text", "")
                        break

        update_api_session(
            thread_id,
            response_id,
            _build_session_message("user", _user_text(request)),
            _build_session_message("assistant", full_text),
        )

        # Tool output mode (PRP-0252, UDR-0233 D3/D4): shapes the RESPONSE only; the
        # session and the ledger above already saw the full turn. In sources mode the
        # server grounding hits become citations like rag_search results (CTR-0287).
        mode = controls.tool_output_mode()
        if mode == "sources":
            output_items = _citation_items(plan) + output_items
        output_items = controls.shape_output(output_items, mode)

        return (
            ResponsesResponse(
                id=response_id,
                model=request.model,
                output=[],  # Simplified: use raw dict for flexibility
                usage=UsageInfo(**usage),
            ).model_dump()
            | {"output": output_items}
            | _grounding_metadata(plan, full_text)
        )

    logger.info("OpenAI Responses API registered at /v1/responses")
