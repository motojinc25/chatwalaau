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
import sys
import time
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
from agent_framework import AgentSession, Content, Message
from agent_framework.exceptions import ChatClientException
from agent_framework_ag_ui._agent_run import _normalize_response_stream
from agent_framework_ag_ui._message_adapters import normalize_agui_input_messages
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from openai import NotFoundError as OpenAINotFoundError
from pydantic import AliasChoices, BaseModel, Field

from app import providers
from app.agent import approval_debug
from app.agent.agent_memory import session_agent_memory_snapshot
from app.agent.approval import (
    ApprovalRecord,
    approval_store,
    truncate_arguments_preview,
)
from app.agent.approval_iteration import (
    IterationContentAccumulator,
    agent_self_reinvokes,
    history_is_self_persisting,
    maf_resumable_call_ids,
    round_is_productive,
)
from app.agent.declarative import active_spec
from app.agent.identity import load_identity
from app.agent.prompt_dump import dump_prompt
from app.agent.temporary import schedule_sweep, set_temporary_run, temporary_path
from app.agent.user_memory import session_user_profile_snapshot
from app.agui.agent_registry import AgentRegistry
from app.auth import verify_api_key
from app.core import provider_errors
from app.core.config import settings
from app.demo import is_demo_mode
from app.image_gen.tools import current_image_options as _image_gen_options
from app.image_gen.tools import current_thread_id as _image_gen_thread_id
from app.providers.structured import resolve_request as resolve_structured_request
from app.providers.structured import soft_validate

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


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    """Return the text of the most recent user message (workflow input, PRP-0118).

    Content may be a plain string or an AG-UI content-part list; text parts are
    concatenated. Returns "" when there is no user text.
    """
    for msg in reversed(messages or []):
        if (msg.get("role") if isinstance(msg, dict) else None) != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            return "".join(parts)
        return ""
    return ""


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


def _is_transient_upstream_error(exc: BaseException) -> bool:
    """Detect a transient upstream 5xx anywhere in the exception chain.

    Azure/OpenAI may return a generic server error mid-stream ("The server had
    an error processing your request ... You can retry your request"). The
    mid-stream variant is raised as a plain ``openai.APIError`` with no
    ``status_code``, so match by type name and message text as well as by code.
    agent-framework wraps it in ChatClientException, hence the chain walk.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        status = getattr(cur, "status_code", None)
        if isinstance(status, int) and 500 <= status < 600:
            return True
        if type(cur).__name__ in {"InternalServerError", "APITimeoutError", "APIConnectionError"}:
            return True
        text = str(cur).lower()
        if "the server had an error processing your request" in text or "internal server error" in text:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


# --- Run-error classification (CTR-0009) -------------------------------------
# Map an exception raised mid-stream to a clear, actionable RUN_ERROR message.
# Applied to BOTH the typed and the generic except blocks so a RAW provider error
# (e.g. anthropic.BadRequestError, which is NOT wrapped in ChatClientException)
# gets the same actionable treatment as an OpenAI-wrapped one.

_MSG_CONTINUATION = "Background response token not found or expired. Please resend your message."
_MSG_PREV_RESPONSE = (
    "The conversation's server-side response reference expired or "
    "was not found. Please resend your message to continue."
)
_MSG_BILLING = (
    "The model provider rejected the request because the account is out of "
    "credits or quota (a billing issue, not a transient error -- retrying will "
    "not help). Add credits or upgrade the plan for the active provider "
    "(e.g. OpenAI Billing, Anthropic Plans & Billing, or raise the Azure "
    "OpenAI quota), or switch to another configured model, then resend."
)
_MSG_RATE_LIMIT = (
    "Rate limited by the model provider (429 Too Many Requests). "
    "A high reasoning effort generates many tokens quickly and is the "
    "most common trigger for the Azure OpenAI per-minute (TPM) limit. "
    "Please wait a moment and retry, lower the reasoning effort, or "
    "raise the deployment quota."
)
_MSG_CAPACITY = (
    "The model provider is out of shared capacity right now. Azure OpenAI "
    "rejected this turn under peak load because the request was too large for "
    "the deployment's standard (pay-as-you-go) capacity pool -- it is a "
    "provider-side condition, not a defect in the conversation. Wait a moment "
    "and resend, start a new chat or remove a large attachment so the request "
    "carries less input, switch to another configured model, or have the "
    "operator move the deployment to Provisioned Throughput (PTU)."
)
_MSG_STREAM_INTERRUPTED = (
    "The connection to the model provider dropped part-way through the answer. "
    "The request itself was accepted (the provider returned 200 and started "
    "streaming), so this is a network interruption rather than a problem with "
    "the conversation -- nothing was charged for a completed answer, and no tool "
    "ran twice. It was retried automatically if it failed before any output "
    "appeared. Please resend your message. If it keeps happening at roughly the "
    "same point each time, ask the operator to check for an idle timeout on the "
    "path to the provider."
)
_MSG_TRANSIENT = (
    "The model provider returned a temporary server error (HTTP 5xx) "
    "while generating the response. This is usually transient -- "
    "please resend your message. On a very long conversation it can "
    "also help to start a new chat."
)
_MSG_CONTEXT_LENGTH = (
    "The request exceeded the model's context window. This turn carried more "
    "input than the model accepts -- a long conversation, a large attached "
    "document, or a big tool result (e.g. many rag_search chunks) are the usual "
    "causes. Start a new chat, remove a large attachment, or lower RAG_TOP_K / "
    "RAG_CHUNK_SIZE so the search result is smaller, then resend."
)
# PRP-0144 / UDR-0125 D6: a run-error message must name only actions reachable in
# the failing run's lane. A harness run has no attachment surface and no
# rag_search chunk budget, so the advice above was inapplicable in every clause
# for the operator who reported this failure.
_MSG_CONTEXT_LENGTH_HARNESS = (
    "The request exceeded the model's context window. A harness conversation "
    "keeps its history in memory for the whole conversation, so a long "
    "tool-heavy session can outgrow the window. Start a new chat with this "
    "harness agent to reset its history, or lower "
    "`compaction.maxOutputTokens` / set `compaction.maxContextWindowTokens` in "
    "the agent's YAML so compaction trims earlier, then resend."
)
_MSG_CONTENT_FILTER = (
    "The model provider's content filter rejected this turn. When it fires right "
    "after a document search, the trigger is usually the retrieved document text "
    "sent back to the model, not your question. Rephrase the question so it "
    "matches different passages, narrow the search, or have the operator adjust "
    "the content-filter policy for the deployment."
)
# Fallback for a cause we do not recognize. The exception TYPE is appended by
# _classify_run_error (v0.117.2) so an operator can correlate the chat message
# with the `AG-UI stream error` traceback in the server log; the message text
# itself is never echoed (it can carry request content).
_MSG_INVALID_SCHEMA_PREFIX = "The JSON Schema set for Structured Output was rejected by the model provider"
_MSG_INVALID_SCHEMA_HELP = (
    "Fix it in the JSON Schema editor next to the composer, or turn Structured Output "
    "off. An explicit schema is sent in STRICT mode, which requires: every array "
    "declares `items`; every object declares `properties` and `additionalProperties: "
    "false`; and every key in `properties` is listed in `required`."
)
_MSG_GENERIC = "An internal error occurred during agent execution."

# PRP-0130 / UDR-0112 D8. A deployment can refuse a FEATURE the request used, which
# is neither a bug nor anything the user can change from the chat. The motivating
# case: a Claude deployment created in Microsoft Foundry with the "Hosted on Azure"
# hosting option supports neither server-side tools (hosted web search) nor native
# structured outputs, and such requests are rejected 400 BY DESIGN. Before this
# message the turn failed with the generic text, so the operator had to read vendor
# documentation to discover the cause -- which is the failure this classification
# exists to prevent.
_MSG_DEPLOYMENT_FEATURE = (
    "This model deployment does not support a feature this turn used. Claude "
    'deployments created in Microsoft Foundry with the "Hosted on Azure" hosting '
    "option do not support server-side tools (hosted web search) or native structured "
    "outputs; those requests are rejected by design. Either recreate the deployment "
    'with the "Hosted on Anthropic" hosting option and point the offering\'s '
    "model_ref at the new deployment name, or turn the affected capability off for "
    "this offering in Model Settings (Hosted web search / Native structured output)."
)

# Markers that identify a DEPLOYMENT-level feature refusal. Deliberately narrow: a
# miss degrades to the previous generic message, whereas a false positive would
# mislabel an unrelated 400 as a configuration problem and suppress its traceback.
_DEPLOYMENT_FEATURE_MARKERS = (
    "not supported in your workspace",
    "not supported for this deployment",
    "hosted on azure",
)


# Markers that identify an Azure OpenAI PEAK-LOAD capacity refusal (v0.131.5).
# Distinct from a 429 rate limit (the per-minute token budget) and from a 5xx: the
# deployment's shared standard capacity cannot take a request of this size right now.
_CAPACITY_MARKERS = (
    "currently experiencing high demand",
    "maximum usage size allowed during peak load",
    "switching to provisioned throughput",
)


# Markers that identify a TRANSPORT termination of an already-accepted response
# (PRP-0155 / UDR-0133 C1). The status line arrived (200) and the body did not
# finish. Distinct from every status-carrying condition above, and from an
# APITimeoutError, which the transient detector already names.
_STREAM_INTERRUPTED_MARKERS = (
    "peer closed connection",
    "incomplete chunked read",
)


# Markers that identify an out-of-credits / quota-exhausted condition (as
# opposed to a transient 429). Anthropic: "Your credit balance is too low ...
# Plans & Billing ... purchase credits". OpenAI: code "insufficient_quota" /
# "exceeded your current quota".
_BILLING_MARKERS = (
    "credit balance is too low",
    "purchase credits",
    "plans & billing",
    "insufficient_quota",
    "exceeded your current quota",
    "billing",
    "payment required",
)


def _is_billing_or_quota_error(exc: BaseException) -> bool:
    """Detect a provider billing / credit / quota-exhausted error in the chain.

    Anthropic raises a 400 invalid_request_error ("Your credit balance is too
    low ...") and OpenAI a 429 with code "insufficient_quota"; both mean the
    account is out of funds/quota and a retry will NOT help -- distinct from a
    transient 429 rate limit. agent-framework may wrap them, so walk the chain.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if getattr(cur, "status_code", None) == 402:
            return True
        if getattr(cur, "code", None) == "insufficient_quota":
            return True
        text = str(cur).lower()
        if any(marker in text for marker in _BILLING_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _is_deployment_feature_unsupported(exc: BaseException) -> bool:
    """Detect a DEPLOYMENT-level feature refusal anywhere in the chain (UDR-0112 D8).

    A deployment can support the model but not a feature the request used. Anthropic
    on Microsoft Foundry is the known case: a "Hosted on Azure" deployment rejects
    server-side tools and structured outputs with a 400 by design. Retrying will not
    help and no chat-surface control can fix it -- the remedy is the deployment or the
    offering's capability declaration, so the message must say so.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur).lower()
        if any(marker in text for marker in _DEPLOYMENT_FEATURE_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _is_stream_interrupted_error(exc: BaseException) -> bool:
    """Detect a transport termination of an ALREADY-ACCEPTED response (UDR-0133 C1).

    The provider returned 200 and began streaming; the connection carrying the
    body was then closed by the far end without its terminating chunk::

        httpcore.RemoteProtocolError: peer closed connection without sending
          complete message body (incomplete chunked read)

    Nothing in this codebase erred and nothing internal happened, but the
    condition matched no detector: there IS a status and it is 200 (so the 5xx
    branch cannot fire), ``RemoteProtocolError`` is absent from the transient
    type set, and its message is unlike either transient marker. It therefore
    reached the terminal branch of :func:`_classify_run_error`, which returns
    ``is_known=False`` -- the contract for "unexpected, log a traceback" -- and
    a routine network event was reported as an internal defect.

    Note that ``APIConnectionError`` is ALREADY in the transient type set: that is
    this same condition on the other side of the response, a connection that fails
    BEFORE the status line. Its mid-stream sibling was missing only because it had
    not yet been observed. This predicate is written against the TRANSPORT rather
    than against a provider, so it holds on every lane.

    Markers are deliberately narrow, matching the discipline of
    :func:`_is_capacity_overload_error`: a miss degrades to the generic message,
    while a false positive would both mislabel an unrelated failure AND suppress
    its traceback.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        # httpx and httpcore both name it RemoteProtocolError; http.client calls
        # the same condition IncompleteRead.
        if type(cur).__name__ in {"RemoteProtocolError", "IncompleteRead"}:
            return True
        text = str(cur).lower()
        if any(marker in text for marker in _STREAM_INTERRUPTED_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _is_capacity_overload_error(exc: BaseException) -> bool:
    """Detect an Azure OpenAI peak-load capacity refusal in the chain (v0.131.5).

    A standard (pay-as-you-go) Azure OpenAI deployment can refuse a LARGE request
    while the shared pool is saturated: "The system is currently experiencing high
    demand and cannot process your request. Your request exceeds the maximum usage
    size allowed during peak load. For improved capacity reliability, consider
    switching to Provisioned Throughput."

    It arrives MID-STREAM, after the POST already returned 200, so it is a plain
    ``openai.APIError`` with no ``status_code`` and no ``code`` -- it matched no
    detector and collapsed to the generic message. It is neither a 429 (no
    "Too Many Requests" / rate-limit text) nor a 5xx, so neither existing branch
    could name it. Markers are deliberately narrow: a miss degrades to the generic
    message, whereas a false positive would mislabel an unrelated failure as a
    provider capacity problem AND suppress its traceback.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur).lower()
        if any(marker in text for marker in _CAPACITY_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _is_context_length_error(exc: BaseException) -> bool:
    """Detect a context-window overflow anywhere in the exception chain (v0.117.2).

    A turn that calls ``rag_search`` (CTR-0077) feeds the retrieved chunks back
    to the model on the FOLLOW-UP request, so a conversation that fit before the
    tool call can exceed the window right after it -- which surfaced as the
    unclassified generic error. OpenAI / Azure OpenAI report it as a 400 with
    code ``context_length_exceeded`` (Responses API: ``string_above_max_length``
    on the input), Anthropic as a 400 "prompt is too long". agent-framework wraps
    them in ChatClientException, hence the chain walk.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if getattr(cur, "code", None) in {"context_length_exceeded", "string_above_max_length"}:
            return True
        text = str(cur).lower()
        if (
            "context_length_exceeded" in text
            or "maximum context length" in text
            or "prompt is too long" in text
            or "reduce the length of the messages" in text
        ):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _is_content_filter_error(exc: BaseException) -> bool:
    """Detect an Azure OpenAI content-filter rejection in the exception chain (v0.117.2).

    Azure raises a 400 with code ``content_filter`` /
    ``ResponsibleAIPolicyViolation`` when the PROMPT trips the policy. On a
    ``rag_search`` turn the prompt includes the retrieved document text, so a
    corpus passage -- not the user's question -- can trip it; the operator needs
    to be told that, rather than shown the generic message.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if getattr(cur, "code", None) in {"content_filter", "ResponsibleAIPolicyViolation"}:
            return True
        text = str(cur).lower()
        if "content_filter" in text or "responsibleaipolicyviolation" in text or "content management policy" in text:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _provider_error_message(exc: BaseException) -> str | None:
    """Pull the provider's own ``error.message`` out of the exception chain (v0.117.5).

    For a schema rejection the provider's message holds the single most useful fact
    there is -- the exact path at fault, e.g. "In context=('properties', 'steps'),
    array schema missing items." Echoing it is safe for THIS class of error only: it
    describes the schema the operator authored, never the conversation content.

    v0.117.6: delegated to ``app.core.provider_errors``. This used to read
    ``exc.body["error"]["message"]``, but the OpenAI SDK unwraps the ``error`` key
    before constructing the exception, so the lookup always missed and the actionable
    message lost the provider's diagnosis.
    """
    return provider_errors.error_message(exc)


def _is_invalid_schema_error(exc: BaseException) -> bool:
    """Detect a provider rejection of the structured-output JSON Schema (v0.117.5).

    An explicit schema (CTR-0118) is forwarded verbatim and sent with
    ``strict: true``, whose requirements are stricter than plain JSON Schema -- an
    array without ``items``, or an object whose ``required`` omits a declared
    property, is rejected with a 400 ``invalid_json_schema`` on ``text.format.schema``
    (OpenAI / Azure OpenAI). This is a CONFIGURATION error the operator can fix, not
    a defect, and it previously collapsed to the generic "internal error".
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        # The SDK exposes `.code` directly; provider_errors additionally covers the
        # body in either the wrapped or the unwrapped shape (v0.117.6).
        if getattr(cur, "code", None) == "invalid_json_schema":
            return True
        detail = provider_errors.error_detail(cur)
        if detail is not None and detail.get("code") == "invalid_json_schema":
            return True
        text = str(cur).lower()
        if "invalid_json_schema" in text or "invalid schema for response_format" in text:
            return True
        # PRP-0131: the ANTHROPIC shape. It reports `invalid_request_error` with the
        # offending path in the message ("output_config.format.schema: For 'object'
        # type, 'additionalProperties: true' is not supported"), so none of the
        # OpenAI-shaped matches above fire and the turn fell through to the generic
        # message -- the third Anthropic path to do so. The existing help text (strict
        # mode needs `additionalProperties: false` and a complete `required`) is
        # exactly Anthropic's rule, so it is the right advice for both providers.
        if "output_config.format.schema" in text or "'additionalproperties' to false" in text:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _classify_run_error(exc: BaseException, *, continuation_token: bool, harness_run: bool = False) -> tuple[str, bool]:
    """Return (user-facing RUN_ERROR message, is_known) for a mid-stream error.

    ``is_known`` False means the cause is unexpected and the caller should log a
    full traceback (logger.exception); True means it is a recognized, actionable
    condition worth only a WARNING. Billing is checked BEFORE rate-limit so an
    OpenAI ``insufficient_quota`` 429 is reported as a billing issue, not as a
    transient rate limit.
    """
    if continuation_token:
        return _MSG_CONTINUATION, True
    if _is_previous_response_not_found(exc):
        return _MSG_PREV_RESPONSE, True
    if _is_billing_or_quota_error(exc):
        return _MSG_BILLING, True
    # Before context-length: a schema rejection is also a 400 and must not be
    # mistaken for one (v0.117.5).
    if _is_invalid_schema_error(exc):
        provider_reason = _provider_error_message(exc)
        detail = f": {provider_reason}" if provider_reason else "."
        return f"{_MSG_INVALID_SCHEMA_PREFIX}{detail} {_MSG_INVALID_SCHEMA_HELP}", True
    # After the schema check (both are 400s and a schema rejection is the more
    # specific diagnosis), before context length (PRP-0130, UDR-0112 D8).
    if _is_deployment_feature_unsupported(exc):
        return _MSG_DEPLOYMENT_FEATURE, True
    if _is_context_length_error(exc):
        # UDR-0125 D6: lane-aware remedy text; the classification is identical.
        return (_MSG_CONTEXT_LENGTH_HARNESS if harness_run else _MSG_CONTEXT_LENGTH), True
    if _is_content_filter_error(exc):
        return _MSG_CONTENT_FILTER, True
    # Before rate-limit and transient (v0.131.5): a peak-load capacity refusal is
    # neither a 429 nor a 5xx, but if Azure ever returns it with a 429 status the
    # capacity diagnosis is the more specific one and must win.
    if _is_capacity_overload_error(exc):
        return _MSG_CAPACITY, True
    if _is_rate_limit_error(exc):
        return _MSG_RATE_LIMIT, True
    # Before the transient 5xx branch (PRP-0155, UDR-0133 C1). The two cannot
    # collide -- a terminated stream carries a 200, not a 5xx -- but the more
    # specific diagnosis leads, the ordering rule v0.131.5 established. Reusing
    # _MSG_TRANSIENT here would tell the operator the provider returned an HTTP
    # 5xx, which the log disproves; a confidently wrong diagnosis costs more than
    # a generic one because it is acted upon.
    if _is_stream_interrupted_error(exc):
        return _MSG_STREAM_INTERRUPTED, True
    if _is_transient_upstream_error(exc):
        return _MSG_TRANSIENT, True
    # v0.117.2: name the exception TYPE so an opaque report ("the chat just says
    # an internal error") can be matched to the logged traceback without asking
    # the operator to reproduce. Type name only -- the message can echo request
    # content, which must not reach the chat surface.
    return f"{_MSG_GENERIC} ({type(exc).__name__})", False


# --- Transient upstream auto-retry (CTR-0009, v0.77.1) ------------------------
# A coding turn fans out into many sequential model calls; any one can hit a
# transient Azure/OpenAI 5xx ("The server had an error processing your request").
# When that happens BEFORE any output has been streamed for the attempt, retrying
# is safe -- nothing was sent to the client or committed server-side -- so the
# model simply restarts the turn. A failure AFTER the first update is NOT retried
# (it would duplicate streamed output); billing / rate-limit / not-found causes
# are never retried (a retry will not help). Fixed small bound, no env var.
_MAX_TRANSIENT_RETRIES = 2
_RETRY_BACKOFF_BASE_SEC = 0.8


class _RetryNotice:
    """Sentinel yielded by ``_resilient_run`` to signal a pending auto-retry.

    The endpoint translates it into a CUSTOM ``run_retry`` event so the SPA can
    show that a transient error is being retried (the run continues; no
    RUN_ERROR is emitted).
    """

    __slots__ = ("attempt", "delay_ms", "max_attempts")

    def __init__(self, attempt: int, max_attempts: int, delay_ms: int) -> None:
        self.attempt = attempt
        self.max_attempts = max_attempts
        self.delay_ms = delay_ms


class _RunStats:
    """The two facts a failed run must be able to report (PRP-0155, UDR-0133 C3).

    ``updates`` is how many updates the CURRENT attempt yielded, and it is the
    fact that decides whether the retry in :func:`_resilient_run` was reachable
    at all -- the gate only opens when an attempt produced nothing. Without it a
    mid-stream failure cannot be told apart from a pre-output one after the fact,
    which is the position that made three prior corrections on this path
    unattributable (UDR-0127).

    ``started`` is reset at the start of every attempt, so the elapsed time
    reported is the failing attempt's, not the whole run's. A drop that lands
    near the same interval every time points at an idle timeout in the network
    path; a scattered one does not.
    """

    __slots__ = ("attempt", "started", "updates")

    def __init__(self) -> None:
        self.attempt = 0
        self.started = time.monotonic()
        self.updates = 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


async def _resilient_run(
    agent: Any, messages: Any, session: Any, run_options: dict[str, Any], stats: _RunStats | None = None
) -> AsyncGenerator[Any, None]:
    """Yield MAF updates, auto-retrying a TRANSIENT upstream 5xx that fails before
    any update is produced (v0.77.1).

    ``produced`` flips True on the first update; once output exists the attempt is
    never retried (mid-stream failures fall through to the caller's classifier and
    surface the normal transient RUN_ERROR). Non-transient, billing, rate-limit,
    and previous-response-not-found errors propagate immediately. Before each
    retry the (uncommitted) server-side response id is cleared so the retry relies
    on explicit context, and a ``_RetryNotice`` is yielded so the caller can inform
    the SPA. ``asyncio.CancelledError`` is not an ``Exception`` subclass, so a
    client disconnect propagates untouched.
    """
    attempt = 0
    while True:
        produced = False
        if stats is not None:
            # Per ATTEMPT, not per run (UDR-0133 C3): a retry starts a new clock
            # and a new count, so what a failure reports is the failing attempt's.
            stats.attempt = attempt
            stats.started = time.monotonic()
            stats.updates = 0
        try:
            response_stream = agent.run(messages, stream=True, session=session, options=run_options or None)
            stream = await _normalize_response_stream(response_stream)
            async for update in stream:
                produced = True
                if stats is not None:
                    stats.updates += 1
                yield update
            return
        except Exception as exc:
            retryable = (
                not produced
                and attempt < _MAX_TRANSIENT_RETRIES
                # UDR-0133 C2: a terminated response stream joins the retry set.
                # Every completed tool result is already in the request body, so a
                # re-POST re-sends finished work rather than repeating it. The
                # `not produced` gate above is NOT relaxed for it -- retrying after
                # output would duplicate streamed text on the client, the same
                # commit boundary UDR-0088 D3 drew on the SPA side.
                and (_is_transient_upstream_error(exc) or _is_stream_interrupted_error(exc))
                and not _is_billing_or_quota_error(exc)
                and not _is_rate_limit_error(exc)
                and not _is_previous_response_not_found(exc)
            )
            if not retryable:
                raise
            attempt += 1
            delay = _RETRY_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
            logger.warning(
                "AG-UI transient upstream error before output; auto-retry %d/%d after %.1fs: %s",
                attempt,
                _MAX_TRANSIENT_RETRIES,
                delay,
                exc,
            )
            # Clear any uncommitted server-side response id so the retry relies on
            # the explicit context (mirrors the post-approval reset).
            session.service_session_id = None
            yield _RetryNotice(attempt, _MAX_TRANSIENT_RETRIES, int(delay * 1000))
            await asyncio.sleep(delay)


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


# Providers whose MAF connector accepts a native PDF document (sent as a Responses
# `input_file`). The anthropic connector currently drops non-image data, so its PDFs
# take the text-extraction path (PRP-0116, UDR-0099 D1).
_NATIVE_PDF_PROVIDERS = frozenset({"azure-openai", "openai", "foundry"})


def _pdf_wants_native(model: str | None) -> bool:
    """Decide whether an attached PDF is sent to the model natively (like an image)
    or extracted to text (PRP-0116, UDR-0099 D1). Controlled by PDF_ATTACH_MODE:
    ``text`` -> never native; ``native`` -> always native; ``auto`` (default) ->
    native only for a provider whose connector supports it."""
    mode = (settings.pdf_attach_mode or "auto").strip().lower()
    if mode == "text":
        return False
    if mode == "native":
        return True
    try:
        provider_name = providers.provider_for(model or "").name
    except Exception:
        provider_name = ""
    return provider_name in _NATIVE_PDF_PROVIDERS


def _extract_pdf_inline(file_path: Path, name: str) -> str:
    """Extract an attached PDF's text for inline injection (PRP-0116, UDR-0099).

    Returns a ``<pdf-content>`` block capped at ``PDF_INLINE_MAX_CHARS`` so the
    agent can analyze the PDF directly. A scanned / image-only PDF (no extractable
    text) or a parse failure degrades to a short note pointing at the RAG pipeline
    (UDR-0099 D3) -- never raises.
    """
    rag_note = (
        f"[Attached PDF: {name} -- no extractable text (likely a scanned/image PDF). "
        f"Ingest it with the RAG pipeline to search it.]"
    )
    if not file_path.is_file():
        return f"[Attached PDF: {name} -- file not found on disk.]"
    try:
        from app.pipeline.rag.pdf_parser import extract_pages

        pages = extract_pages(file_path)
    except Exception:  # corrupt / unreadable PDF -- degrade, never fail the chat
        logger.warning("inline PDF extraction failed for %s", name, exc_info=True)
        return rag_note
    if not pages:
        return rag_note

    cap = max(0, int(settings.pdf_inline_max_chars))
    text = "\n\n".join(f"--- page {p['page']} ---\n{p['text'].strip()}" for p in pages)
    truncated = len(text) > cap
    if truncated:
        text = text[:cap]
    return (
        f"[Attached PDF: {name}]\n"
        f'<pdf-content name="{name}" pages="{len(pages)}" truncated="{str(truncated).lower()}">\n'
        f"{text}\n"
        f"</pdf-content>"
    )


def _inject_image_content(
    maf_messages: list[Any],
    raw_messages: list[dict[str, Any]],
    *,
    pdf_native: bool = False,
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
            # PDF files: given to the LLM as context, like an image (PRP-0116,
            # UDR-0099). NATIVE mode attaches the raw PDF as document content (the
            # Responses connector emits an `input_file`); the TEXT path extracts the
            # PDF text and injects it inline (for a provider whose connector drops
            # non-image data, or when PDF_ATTACH_MODE=text).
            if media_type == "application/pdf" or uri.endswith(".pdf"):
                if uri.startswith("/api/uploads/"):
                    parts = uri.split("/")  # ["", "api", "uploads", thread_id, filename]
                    if len(parts) >= 5:
                        name = parts[4]
                        pdf_path = upload_dir / parts[3] / name
                        if pdf_native and pdf_path.is_file():
                            # `filename` is REQUIRED by the OpenAI Responses `input_file`
                            # block when `file_data` is used; the connector reads it from
                            # additional_properties (a bare Content has no filename), so
                            # omitting it yields a 400 "missing required parameter".
                            target.contents.append(
                                Content.from_data(
                                    data=pdf_path.read_bytes(),
                                    media_type="application/pdf",
                                    additional_properties={"filename": name},
                                )
                            )
                            # A short marker so the model also sees the file's name in text.
                            pdf_refs.append(f"[Attached PDF: {name}]")
                        else:
                            pdf_refs.append(_extract_pdf_inline(pdf_path, name))
                continue
            if uri.startswith("/api/uploads/"):
                parts = uri.split("/")  # ["", "api", "uploads", thread_id, filename]
                if len(parts) >= 5:
                    file_path = upload_dir / parts[3] / parts[4]
                    if file_path.is_file():
                        target.contents.append(Content.from_data(data=file_path.read_bytes(), media_type=media_type))
            elif uri.startswith(("http://", "https://")):
                target.contents.append(Content.from_uri(uri=uri, media_type=media_type))

        # Append the extracted PDF text (or fallback note) to the user message
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


def _is_first_assistant_turn(messages: list[dict[str, Any]]) -> bool:
    """True when the request carries no prior assistant message (CTR-0109).

    The SPA sends the full message history on each turn, so an absent assistant
    role marks the conversation's first turn -- the only turn that should trigger
    Auto Session Title. This also makes the trigger fire at most once (later
    turns carry an assistant message), so a failed first attempt is not retried
    (UDR-0053 D5/D9).
    """
    return not any(m.get("role") == "assistant" for m in messages)


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    """Return the first user message's text (AG-UI content may be str or list)."""
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif isinstance(item, str):
                    parts.append(item)
            return " ".join(parts).strip()
    return ""


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

    # Declarative Workflow run-target (PRP-0118, CTR-0181, UDR-0101 D5). When the AG-UI
    # request selects a workflow (state.workflow_id), stream the compiled workflow graph
    # instead of the active Prompt agent; stream_workflow emits its own RUN_STARTED /
    # RUN_FINISHED lifecycle and the additive workflow_* CUSTOM events. Absent the flag
    # the endpoint runs the active agent exactly as before (byte-for-byte, UDR-0101 D5).
    _workflow_id = (request_body.state or {}).get("workflow_id") if request_body.state else None
    if _workflow_id:
        from app.workflow.runtime import stream_workflow

        # PRP-0123 / UDR-0106 D5: a human-in-the-loop answer arrives as `state.workflow_resume`
        # ({request_id: answer}) on the NEXT request rather than over a held connection. Such a
        # request carries no new user message and must not be treated as one.
        _wf_resume_raw = (request_body.state or {}).get("workflow_resume")
        _wf_resume = _wf_resume_raw if isinstance(_wf_resume_raw, dict) and _wf_resume_raw else None
        _wf_input = "" if _wf_resume else _latest_user_text(request_body.messages)
        _wf_result: dict[str, Any] = {}
        async for _chunk in stream_workflow(
            str(_workflow_id),
            _wf_input,
            encoder,
            thread_id=thread_id,
            result=_wf_result,
            resume=_wf_resume,
        ):
            yield _chunk
        # Auto Session Title (PRP-0077, CTR-0109, UDR-0053 D17): the workflow branch
        # returns before the shared post-turn block, so it must drive titling itself --
        # otherwise a FIRST-turn workflow run leaves auto_title_pending set and the
        # sidebar spinner animates forever (v0.115.1). Generate a title from the
        # workflow's output when it produced text; otherwise clear the pending spinner.
        _wf_temporary = bool((request_body.state or {}).get("temporary"))
        if (
            settings.session_title_mode == "llm"
            and not _wf_temporary
            and _is_first_assistant_turn(request_body.messages)
        ):
            _wf_assistant = str(_wf_result.get("assistant_text") or "")
            _wf_user = _first_user_text(request_body.messages)
            try:
                from app.background import dispatch as _dispatch_background

                if _wf_user and _wf_assistant:
                    _dispatch_background(
                        "session-title",
                        dedup_key=thread_id,
                        ctx={
                            "thread_id": thread_id,
                            "user_text": _wf_user,
                            "assistant_text": _wf_assistant,
                            "model": "",
                        },
                    )
                else:
                    _dispatch_background("session-title-clear", dedup_key=thread_id, ctx={"thread_id": thread_id})
            except Exception:
                logger.warning("failed to dispatch workflow session title for %s", thread_id, exc_info=True)
        return

    # Harness Agent run-target (PRP-0135, CTR-0009 v-next, UDR-0119 D3/D4). When the
    # request selects a harness agent (state.harness_id), the run streams the cached
    # per-conversation harness agent through the SAME loop below -- the harness returns
    # a standard MAF Agent, so the existing TEXT/REASONING/TOOL_CALL translation and
    # the FEAT-0028 approval handshake apply unchanged. Absent the flag (and absent
    # state.workflow_id above) the endpoint runs the active Prompt agent byte-for-byte.
    _harness_id = (request_body.state or {}).get("harness_id") if request_body.state else None
    _harness_ctx: tuple[Any, Any, str] | None = None
    if _harness_id:
        from app.agent.harness.runtime import agent_for_thread as _harness_agent_for_thread
        from app.agent.harness.spec import HarnessAgentError as _HarnessAgentError

        try:
            _harness_ctx = await _harness_agent_for_thread(str(_harness_id), thread_id)
        except _HarnessAgentError as exc:
            # Unknown id / blocking warnings / demo mode: a clean RUN_ERROR run.
            yield encoder.encode(RunStartedEvent(type=EventType.RUN_STARTED, run_id=run_id, thread_id=thread_id))
            yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, message=str(exc)))
            yield encoder.encode(RunFinishedEvent(type=EventType.RUN_FINISHED, run_id=run_id, thread_id=thread_id))
            return
        except Exception:
            logger.exception("Harness agent %s failed to build", _harness_id)
            yield encoder.encode(RunStartedEvent(type=EventType.RUN_STARTED, run_id=run_id, thread_id=thread_id))
            yield encoder.encode(
                RunErrorEvent(type=EventType.RUN_ERROR, message="The harness agent could not be built.")
            )
            yield encoder.encode(RunFinishedEvent(type=EventType.RUN_FINISHED, run_id=run_id, thread_id=thread_id))
            return
    harness_run = _harness_ctx is not None

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
    # Auto Session Title (PRP-0077, CTR-0109): accumulate the visible assistant
    # text so the post-turn background task can summarize it without re-reading
    # the (frontend-saved) session file. Pre-initialized here so they are always
    # bound on the normal-completion path below.
    assistant_text_parts: list[str] = []
    temporary = False
    effective_model = ""
    # Auto Session Title spinner reconciliation (UDR-0053 D17). `auto_title_pending`
    # is set at session init (CTR-0014) and is normally cleared by the generation
    # task. If that task never runs for the FIRST turn -- a run error, a user abort /
    # client disconnect, or a first turn with no assistant text -- the spinner would
    # animate forever, so we dispatch a lightweight clear task in those cases.
    is_first_turn = _is_first_assistant_turn(request_body.messages)
    title_dispatched = False

    def _dispatch_title_clear() -> None:
        # No-op unless llm titling could have left this non-temporary session's first
        # turn pending and the generation task was not dispatched. Sync (fire-and-
        # forget) so it is safe to call during GeneratorExit / cancellation.
        if title_dispatched or temporary or not is_first_turn:
            return
        if settings.session_title_mode != "llm":
            return
        try:
            from app.background import dispatch as _dispatch_background

            _dispatch_background("session-title-clear", dedup_key=thread_id, ctx={"thread_id": thread_id})
        except Exception:
            logger.warning("failed to dispatch session-title-clear for %s", thread_id, exc_info=True)

    # PRP-0155 / UDR-0133 C3: declared BEFORE the try so the failure log below can
    # read it on any path, including one that fails before the run loop is reached.
    run_stats = _RunStats()

    try:
        # Pre-process: strip PDF image_url entries from messages before normalization.
        # normalize_agui_input_messages converts image_url content to MAF Content,
        # but Azure OpenAI Responses API rejects non-image content types (e.g., PDF).
        # PDF references are injected as text by _inject_image_content instead.
        sanitized_messages = _strip_pdf_content(request_body.messages)

        # Convert AG-UI messages to MAF Message objects
        messages, _ = normalize_agui_input_messages(sanitized_messages)

        # Inject image + PDF content from the request into MAF messages (CTR-0022).
        # A PDF is attached natively (like an image) for providers that support it,
        # else its text is extracted (PRP-0116, UDR-0099); resolve the mode from the
        # requested model's provider.
        _pdf_model = request_body.state.get("model") if request_body.state else None
        _inject_image_content(messages, request_body.messages, pdf_native=_pdf_wants_native(_pdf_model))

        # Create session with metadata (same as _agent_run.py:685-691). A harness
        # run reuses the per-conversation CACHED session instead, so its in-memory
        # history / todo / mode / loop state survives across turns (UDR-0119 D4).
        session = _harness_ctx[1] if harness_run else AgentSession()
        session.metadata = {
            "ag_ui_thread_id": thread_id,
            "ag_ui_run_id": run_id,
        }

        # Read model, reasoning, and background options from AG-UI state
        # (CTR-0070, CTR-0045, CTR-0009 reasoning PRP-0071)
        selected_model = None
        requested_options: dict[str, Any] = {}
        background = False
        continuation_token = None
        temporary = False
        image_options: dict[str, Any] = {}
        if request_body.state:
            selected_model = request_body.state.get("model")
            # Per-session image output options (PRP-0085, CTR-0120/CTR-0049,
            # UDR-0063 D6). The SPA sends state.image_options
            # {size, quality, format, compression, background}; it becomes the tool
            # DEFAULT via the image-gen contextvar (an explicit LLM argument wins).
            raw_image_options = request_body.state.get("image_options")
            if isinstance(raw_image_options, dict):
                image_options = {k: v for k, v in raw_image_options.items() if v not in (None, "")}
            # Per-message generation options (PRP-0081, CTR-0009 v13). The SPA
            # sends a `state.model_options` object (e.g. {effort, verbosity}).
            # Back-compat (UDR-0057): a legacy `state.reasoning` string is folded
            # in as {effort: <value>} when model_options omits effort.
            raw_options = request_body.state.get("model_options")
            if isinstance(raw_options, dict):
                requested_options = dict(raw_options)
            legacy_reasoning = request_body.state.get("reasoning")
            if legacy_reasoning and "effort" not in requested_options:
                requested_options["effort"] = legacy_reasoning
            background = request_body.state.get("background", False)
            continuation_token = request_body.state.get("continuation_token")
            # Temporary Chat (PRP-0076, CTR-0106, UDR-0052). When the SPA marks
            # the run temporary the thread_id is temp_-prefixed and routes to the
            # .temporary/ quarantine (CTR-0014); below we skip the Memory snapshot
            # and no-op the memory tool for a de-personalized, Identity-only run.
            temporary = bool(request_body.state.get("temporary", False))

        # Structured output (PRP-0082, CTR-0009 v14, UDR-0058). Resolve the additive
        # `state.output_schema` (object) / `state.output_format` (mode) into an
        # (explicit schema, mode) pair. mode == "none" means structured output is off
        # (default) and the request stays byte-for-byte (UDR-0058 D7).
        output_schema, output_format = resolve_structured_request(request_body.state)
        structured_active = output_format != "none"

        # Harness run-target (PRP-0135, UDR-0119 D3): the per-message Model /
        # options / Structured Output / Background controls do NOT apply -- the
        # harness YAML fixes the model and the factory fixes the policies
        # (CTR-0193). CTR-0197 hides the controls; ignoring the state here is
        # defense-in-depth so a stale client value can never leak into the run.
        if harness_run:
            selected_model = None
            requested_options = {}
            background = False
            continuation_token = None
            output_schema, output_format = None, "none"
            structured_active = False

        # Select agent: the harness run-target's cached agent (CTR-0193, UDR-0119
        # D3) or the registry agent for state.model (CTR-0070, PRP-0035).
        agent = _harness_ctx[0] if harness_run else agent_registry.get(selected_model)

        # Resolve the per-message generation options against the owning provider's
        # catalog (PRP-0081, UDR-0057 D7). Every advertised option is resolved
        # (effort plus, for gpt-5.x, verbosity); a missing / invalid value falls
        # back to the model default and never errors. The resolved values are
        # emitted in the usage event regardless of provider; the per-request
        # options are merged only for live (non-demo) agents -- DemoChatClient
        # ignores them. `resolved_reasoning` is kept as the effort alias used by
        # the usage event's back-compat `reasoning` field.
        # A harness run reports the YAML-bound offering as the effective model
        # (usage events, provider-aware approval reconstruction); the per-message
        # option resolution below stays inert for it ({} everywhere).
        effective_model = _harness_ctx[2] if harness_run else (selected_model or agent_registry.default_model)
        resolved_options = {} if harness_run else providers.resolve_options(effective_model, requested_options)

        # Active declarative agent option DEFAULTS (PRP-0094, CTR-0142, UDR-0072 D5).
        # The agent's mapped options (effort / verbosity) are applied for any option the
        # operator has NOT explicitly changed in the chat panel -- i.e. the panel still
        # reports the model's catalog default -- and are overridden by an explicit
        # per-message selection. This makes the active agent's options actually take
        # effect (the panel always reports a full selection, so they cannot ride on the
        # agent's startup default_options alone) while preserving per-message override.
        spec = active_spec()
        spec_opts = {} if harness_run else (spec.model_options_override or {})
        if spec_opts:
            catalog_defaults = {
                d["key"]: d.get("default") for d in providers.model_options_catalog(effective_model)["options"]
            }
            for key, value in spec_opts.items():
                if resolved_options.get(key) == catalog_defaults.get(key):
                    resolved_options[key] = value
        # The agent's default structured output applies when the chat has not explicitly
        # enabled structured output (UDR-0072 D5; per-message control still wins).
        # Never for a harness run (the active Prompt agent's defaults are not its own).
        if not harness_run and not structured_active and spec.structured_output is not None:
            output_schema = spec.structured_output.get("schema")
            output_format = spec.structured_output.get("mode", "json_schema")
            structured_active = output_format != "none"

        # PRP-0131 / UDR-0058 D9 removed the PRP-0131 pre-run guard: a request with no
        # schema is no longer unsatisfiable, because each provider now supplies its own
        # DEFAULT output schema (open where it can express one, a valid closed object
        # where it cannot). There is nothing left to refuse.

        resolved_reasoning = resolved_options.get("effort", providers.resolve_effort(effective_model, None))

        # Validate continuation_token format: MAF expects dict with "response_id"
        if continuation_token and isinstance(continuation_token, str):
            continuation_token = {"response_id": continuation_token}

        run_options: dict[str, Any] = {}
        # CTR-0045 / PRP-0073: background runs are an OpenAI Responses API
        # feature. Drop the flag for providers that do not support it (e.g.
        # Anthropic Opus 4.7/4.8) so a stale client toggle can never produce a
        # provider validation error. The UI also disables the toggle for these
        # models, so this is defense-in-depth.
        if background and providers.background_supported(effective_model):
            run_options["background"] = True
            # A background response is stored and polled server-side, so it needs
            # store=True. The Prompt-lane agent is built CLIENT-MANAGED
            # (default_options store=False, PRP-0142); this per-request override
            # restores server-side storage for the background turn (run options win
            # in MAF _merge_options). Non-background turns keep store=False.
            run_options["store"] = True
        elif background:
            background = False
        if continuation_token:
            run_options["continuation_token"] = continuation_token
        # Per-request generation options override the Agent's startup
        # default_options via MAF _merge_options (per key). Only when the client
        # explicitly chose at least one option; otherwise the Agent's
        # catalog-default options already apply. build_model_options keeps the
        # output-neutral default rule (a default value is omitted), so a default
        # selection is byte-for-byte with the construction-time options.
        #
        # Structured output (PRP-0082, UDR-0058 D2) folds into the same merge: when a
        # schema is requested we ALWAYS build the model options (so the provider's
        # native option container -- OpenAI `text`, Anthropic `output_config` -- is
        # present with its effort/verbosity defaults) and then deep-merge the
        # structured fragment into it via merge_generation_options, so the format and
        # the effort/verbosity coexist instead of clobbering each other.
        if (requested_options or structured_active or spec_opts) and not is_demo_mode():
            gen_options = providers.build_model_options(effective_model, resolved_options)
            if structured_active:
                providers.merge_generation_options(
                    gen_options,
                    providers.build_structured_output(effective_model, output_schema, output_format),
                )
            providers.merge_generation_options(run_options, gen_options)

        # Temporary Chat (PRP-0076, CTR-0106, UDR-0052) vs normal run.
        # Temporary: de-personalized. No User Profile snapshot is captured or
        # injected; the memory tool no-ops via the temporary contextvar; the
        # effective system prompt is the Identity block alone
        # (run_instructions(temporary=True) -> None, UDR-0052 D6). The temp_
        # thread id already routes history to the .temporary/ quarantine.
        # Normal: User Preference Memory (PRP-0075, CTR-0105, UDR-0051 D3/D4) --
        # capture the per-session FROZEN snapshot (once at session start; reused
        # on reload) and merge the per-run remainder (Memory Block slot #2 +
        # capability guidance) so the prompt is Identity -> Memory -> capabilities.
        set_temporary_run(temporary)
        profile_snapshot = None
        memory_snapshot = None
        extra_instructions: str | None = None
        if temporary:
            # Opportunistic retention sweep when this temporary thread is new
            # (UDR-0052 D4). Fire-and-forget so the stream is never blocked.
            if not temporary_path(thread_id).exists():
                schedule_sweep()
        elif not harness_run:
            profile_snapshot = session_user_profile_snapshot(thread_id)
            # Agent Curated Memory (PRP-0100, CTR-0162, UDR-0079 D6). Capture the
            # per-session FROZEN <agent-memory> snapshot (once at session start;
            # reused on reload; None when disabled or the memory is empty) and inject
            # it at slot #2b, after the User Profile.
            memory_snapshot = session_agent_memory_snapshot(thread_id)
        # A harness run never receives the Identity / Memory / capability-guidance
        # instructions: the harness OWNS its prompt assembly (UDR-0119 D2), and its
        # own file memory + skills providers fill the equivalent roles.
        if not harness_run:
            extra_instructions = agent_registry.run_instructions(
                effective_model,
                user_profile_block=profile_snapshot,
                agent_memory_block=memory_snapshot,
                temporary=temporary,
            )
        if extra_instructions:
            run_options["instructions"] = extra_instructions

        # Observe the assembled system prompt -- the Identity (slot #1), the User
        # Profile (slot #2a), the Agent Memory (slot #2b, PRP-0100/CTR-0162), and the
        # capability guidance. The effective prompt is the baked Identity plus the
        # per-run remainder (MAF concatenates run instructions after the baked
        # Identity). Logs carry METADATA ONLY (sizes / injection flags); the full
        # prompt CONTENT is written to a per-run file under PROMPT_DUMP_DIR when
        # PROMPT_DUMP_ENABLED (default off), so it is inspectable without flooding logs.
        # A harness run's system prompt is MAF-assembled inside the agent (UDR-0119
        # D2), so the Identity-based observability below reports the ChatWalaʻau-
        # injected remainder only (empty for harness; the dump is skipped too).
        _identity_text = "" if harness_run else load_identity()
        _effective_system_prompt = (
            _identity_text if not extra_instructions else f"{_identity_text}\n\n{extra_instructions}"
        )
        logger.info(
            "System prompt assembled: thread=%s model=%s first_turn=%s temporary=%s "
            "user_profile=%s agent_memory=%s (chars=%d)",
            thread_id,
            effective_model,
            is_first_turn,
            temporary,
            profile_snapshot is not None,
            memory_snapshot is not None,
            len(_effective_system_prompt),
        )
        if settings.prompt_dump_enabled and not harness_run:
            # The SPA sends only the NEW message; prior turns live in the session
            # store and are injected by FileHistoryProvider. To make the dump show
            # the FULL flowing prompt (past history + the new message), read the
            # persisted history and prepend it. First turn ("session start") is
            # judged by whether any prior assistant turn exists; only then is the
            # (frozen) system prompt written -- later turns dump the flowing
            # conversation only, since the system prompt is unchanged.
            _history_msgs: list[dict[str, Any]] = []
            if not temporary:
                from app.session.storage import read_session_json

                try:
                    _sess = read_session_json(thread_id)
                except (OSError, ValueError):
                    _sess = None
                if isinstance(_sess, dict):
                    _raw = _sess.get("messages")
                    if isinstance(_raw, list):
                        _history_msgs = _raw
            _dump_first_turn = not any(isinstance(m, dict) and m.get("role") == "assistant" for m in _history_msgs)
            _flowing = [*_history_msgs, *request_body.messages]
            # Tool surface (PRP-0119, CTR-0009 v18, UDR-0102). Read from the BUILT
            # agent -- the object about to run -- not from the authoring inventory
            # (CTR-0178), so a configuration-vs-build divergence is visible rather
            # than restated (UDR-0102 D1). Read-only and best-effort: returns None on
            # any failure and the dump proceeds without the section.
            from app.agent.tool_surface import describe_tool_surface

            _tool_surface = await describe_tool_surface(agent, spec=spec, model=effective_model)
            _dump_path = dump_prompt(
                thread_id=thread_id,
                run_id=run_id,
                model=effective_model,
                system_prompt=_effective_system_prompt,
                messages=_flowing,
                include_system_prompt=_dump_first_turn,
                tool_surface=_tool_surface,
                meta={
                    "first_turn": _dump_first_turn,
                    "temporary": temporary,
                    "user_profile_injected": profile_snapshot is not None,
                    "agent_memory_injected": memory_snapshot is not None,
                    "history_messages": len(_history_msgs),
                    "new_messages": len(request_body.messages),
                },
            )
            if _dump_path is not None:
                logger.info(
                    "Prompt dump written: %s (first_turn=%s, system_prompt_chars=%d, "
                    "history_messages=%d, new_messages=%d, tool_surface=%s)",
                    _dump_path,
                    _dump_first_turn,
                    len(_effective_system_prompt),
                    len(_history_msgs),
                    len(request_body.messages),
                    (
                        f"{_tool_surface.digest} {_tool_surface.counts()}"
                        if _tool_surface is not None
                        else "unavailable"
                    ),
                )

        # Set thread_id for image generation tools (CTR-0050, PRP-0027)
        _image_gen_thread_id.set(thread_id)
        # Per-session image output options (CTR-0120/CTR-0049, PRP-0085). The tools
        # read this as their default when the LLM omits a value.
        _image_gen_options.set(image_options)

        # PRP-0067 approval loop. The first iteration runs the original
        # messages. If MAF emits function_approval_request contents, the
        # inner loop collects them, parks on the asyncio.Event for each,
        # then appends a fresh "user" message bundling the resolved
        # responses and re-runs the agent. The loop terminates when an
        # iteration finishes without producing any approval request.
        iteration_messages: list[Any] = list(messages)
        # PRP-0144 / UDR-0125 D1/D2: does this agent's own history already hold
        # the turn, or does the re-run have to carry it? Read ONCE per run from
        # the agent's persistence configuration -- never from `harness_run`, so a
        # future lane that adopts per-service-call persistence inherits the
        # correct behaviour without an edit here (D2).
        self_persisting = history_is_self_persisting(agent)
        # PRP-0103 / UDR-0082 D1/D2: the approval re-run loop is bounded by two
        # operator-configurable counters. `interactive_rounds` only advances for
        # a round that required a human decision (source != "session-cache"), so
        # a blanket "approve for session" grant no longer burns the budget;
        # `total_rounds` advances for every round and is the runaway backstop.
        max_approval_iterations = settings.tool_approval_max_iterations
        # PRP-0146 / UDR-0082 D7/D8/D9: HOW the runaway backstop is budgeted depends
        # on whether this agent RE-RUNS ITSELF -- read once, from the agent, never
        # from `harness_run` (D8, following UDR-0125 D2). On a self-re-invoking
        # agent every gated tool call is an approval round, so a flat round ceiling
        # measures productivity: an observed harness turn died at total=201/200 with
        # interactive=0/33, without one human decision. That lane is bounded by LACK
        # OF PROGRESS instead, with a far higher round backstop behind it. An agent
        # that does not re-invoke itself keeps the historical budget byte-for-byte.
        autonomous = agent_self_reinvokes(agent)
        absolute_max_iterations = (
            settings.autonomous_loop_max_rounds if autonomous else settings.tool_approval_absolute_max_iterations
        )
        no_progress_limit = settings.autonomous_loop_no_progress_rounds if autonomous else None
        interactive_rounds = 0
        total_rounds = 0
        # Consecutive rounds that executed no tool and produced no text; reset by
        # every productive round (UDR-0082 D7). Only counted on the autonomous lane.
        no_progress_rounds = 0
        # Cumulative executed-tool count, so an exhausted budget can report the work
        # the turn actually performed instead of only naming a number (D10).
        tools_executed_total = 0
        approval_loop_exceeded = False
        loop_stop_reason: str | None = None
        logger.info(
            "AG-UI run start: thread=%s model=%s harness=%s input_msgs=%d self_persisting_history=%s "
            "self_reinvoking=%s round_budget=%s no_progress_limit=%s",
            thread_id,
            effective_model if harness_run else (selected_model or "<default>"),
            _harness_id or "-",
            len(iteration_messages),
            self_persisting,
            autonomous,
            absolute_max_iterations,
            no_progress_limit if no_progress_limit is not None else "-",
        )
        # PRP-0148 Section 4.3: did this turn START polluted? A harness conversation
        # keeps its history in a cached MAF session the operator cannot see, and an
        # aborted earlier run can leave orphaned tool calls in it (RES-0003 F3). An
        # operator reporting "it failed again" can now say whether the agent began the
        # turn already carrying debris, instead of that only being deducible from the
        # request dump after the fact.
        if harness_run:
            from app.agent.harness import history_debug as _history_debug

            _history_debug.log_run_start(
                thread_id=thread_id,
                harness_id=str(_harness_id or "-"),
                session=session,
                cached=True,
            )
        # Structured output marker (PRP-0082, CTR-0009 v14, UDR-0058 D5). Tell the
        # SPA early that this turn is structured so it renders the answer as a JSON
        # code block. DEMO_MODE resolves no structured shape (DemoChatClient is
        # selected before provider dispatch, UDR-0045 D7 / D10), so the marker is not
        # emitted there. Additive CUSTOM event; no new SSE event TYPE.
        if structured_active and not is_demo_mode():
            so_support = providers.structured_output_support(effective_model)
            yield encoder.encode(
                CustomEvent(
                    type=EventType.CUSTOM,
                    name="structured_output",
                    value={
                        "enabled": True,
                        "mode": output_format,
                        "strategy": "native" if so_support.get("native") else so_support.get("fallback", "none"),
                        "schema_present": output_schema is not None,
                    },
                )
            )
        while True:
            pending_approvals: list[tuple[ApprovalRecord, Content]] = []
            # PRP-0069 follow-up: capture this iteration's stream content so
            # the next iteration's agent.run input pairs the originating
            # function_call (assistant role) with the approval response (user
            # role). Without it, MAF's Anthropic connector emits an orphan
            # tool_result -> HTTP 400. See app.agent.approval_iteration.
            iter_accumulator = IterationContentAccumulator()
            # PRP-0141 tracing: the input this iteration hands to agent.run, and
            # what MAF holds on the session going in.
            approval_debug.logger.info(
                "[iter %d/%d start] thread=%s input_msgs=%d service_session_id=%s %s\n  input: %s",
                total_rounds,
                interactive_rounds,
                thread_id,
                len(iteration_messages),
                getattr(session, "service_session_id", None),
                approval_debug.describe_session_state(session),
                " | ".join(approval_debug.describe_messages(iteration_messages)),
            )

            async for update in _resilient_run(agent, iteration_messages, session, run_options, stats=run_stats):
                # v0.77.1: a transient upstream 5xx before any output triggers an
                # automatic retry. The helper yields a _RetryNotice so we can tell
                # the SPA a retry is happening (the run continues; no RUN_ERROR).
                if isinstance(update, _RetryNotice):
                    yield encoder.encode(
                        CustomEvent(
                            type=EventType.CUSTOM,
                            name="run_retry",
                            value={
                                "attempt": update.attempt,
                                "max_attempts": update.max_attempts,
                                "delay_ms": update.delay_ms,
                                "reason": "transient_upstream_error",
                            },
                        )
                    )
                    continue

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
                        # Accumulate visible assistant text for Auto Session
                        # Title (PRP-0077, CTR-0109).
                        assistant_text_parts.append(text)
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
                                    # PRP-0103 / UDR-0082 D3: the 1-based
                                    # interactive-round number this card belongs
                                    # to, and the configured budget. Frozen
                                    # during cached rounds (interactive_rounds
                                    # does not advance), so the SPA counter stops
                                    # while a blanket session grant is active.
                                    "iteration": interactive_rounds + 1,
                                    "max_iterations": max_approval_iterations,
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
                        # PRP-0144 / UDR-0125 D5: attribute usage to the offering
                        # that actually SERVED the turn. `effective_model` already
                        # resolves to the harness run-target's bound offering for a
                        # harness run and to the selected/default model otherwise;
                        # reading `selected_model` back here reported another
                        # model's context window, because a harness run
                        # deliberately clears it (the per-message controls must not
                        # leak into a run-target run).
                        model_name = effective_model
                        # Catalog context window (CTR-0069, PRP-0113): an offering's
                        # declared context_window wins, else DEFAULT_CONTEXT_WINDOW.
                        usage_value["max_context_tokens"] = providers.get_max_context_tokens(model_name)
                        usage_value["model"] = model_name
                        # Per-message generation options (PRP-0071 / PRP-0081,
                        # CTR-0030). The effort keeps its back-compat field name
                        # `reasoning`; every other advertised option (e.g.
                        # `verbosity`) is echoed under its own key. Emitted for
                        # every provider (incl. demo) so the SPA can label and
                        # persist the selections next to the model name. Anthropic
                        # advertises effort only, so no extra key appears there.
                        # None = the model advertises no effort axis (e.g. a
                        # non-OpenAI-family Foundry deployment, UDR-0085 A1);
                        # omit the key rather than echo null.
                        if resolved_reasoning is not None:
                            usage_value["reasoning"] = resolved_reasoning
                        usage_value.update({k: v for k, v in resolved_options.items() if k != "effort"})
                        # Structured output status (PRP-0082, CTR-0009 v14, UDR-0058
                        # D4/D9). Soft, non-blocking: parse the accumulated answer and
                        # surface a status; never reject / regenerate. Persisted by the
                        # SPA inside the usage object so the structured flag survives
                        # reload. Skipped in DEMO_MODE (no structured resolution).
                        if structured_active and not is_demo_mode():
                            usage_value["structured"] = True
                            usage_value["output_status"] = soft_validate("".join(assistant_text_parts))
                        # Provider-agnostic prompt-cache metrics (PRP-0080,
                        # CTR-0009 / FEAT-0038 / UDR-0056 D6). Normalize the
                        # provider-specific cache token keys to a stable
                        # cache_read_input_tokens / cache_write_input_tokens pair
                        # so the SPA / operator can see the savings regardless of
                        # provider. Anthropic reports anthropic.cache_read /
                        # cache_creation; OpenAI reports a *cached_tokens detail.
                        # Additive: absent when the provider reports no cache use.
                        cache_read = usage_value.get("anthropic.cache_read_input_tokens")
                        cache_write = usage_value.get("anthropic.cache_creation_input_tokens")
                        if cache_read is None:
                            cache_read = next(
                                (
                                    v
                                    for k, v in usage_value.items()
                                    if isinstance(k, str) and k.endswith("cached_tokens")
                                ),
                                None,
                            )
                        if cache_read is not None:
                            usage_value["cache_read_input_tokens"] = cache_read
                        if cache_write is not None:
                            usage_value["cache_write_input_tokens"] = cache_write
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
            approval_debug.logger.info(
                "[iter %d end] thread=%s pending_approvals=%d %s\n  %s",
                total_rounds,
                thread_id,
                len(pending_approvals),
                approval_debug.describe_session_state(session),
                iter_accumulator.observation_summary(),
            )
            if not pending_approvals:
                break

            approval_response_contents: list[Content] = []
            # PRP-0103 / UDR-0082 D2: record each resolution source so the round
            # can be classified as cached (all "session-cache") or interactive.
            round_sources: list[str] = []
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
                round_sources.append(source)
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

            # PRP-0144 / UDR-0125 D1: on a lane whose history has ALREADY
            # persisted this turn, the replay below is not merely unnecessary --
            # it is the defect. MAF's per-service-call persistence writes every
            # model call's inputs and outputs to the history provider as the run
            # proceeds, and `_split_service_call_messages` files our synthetic
            # messages as NEW INPUT (they carry no `_attribution`), so each
            # round's replay of rounds 1..N-1 is written into a history that
            # already holds it. MAF then re-streams those calls, the accumulator
            # observes them, and an even larger replay is appended: a closed
            # feedback loop that took a three-message conversation to 200
            # messages / 926 wire items in seven rounds and overran the model's
            # context window. Send this round's approval responses ALONE and let
            # MAF load the rest from the history it has been writing.
            #
            # Everything below this branch -- the provider-aware reconstruction,
            # the deferred-call replay gate, the dangling-result healer -- exists
            # to rebuild messages a lane cannot supply, so none of it applies
            # here. It is untouched on the lane that needs it.
            if self_persisting:
                iteration_messages = [Message(role="user", contents=approval_response_contents)]
                approval_debug.logger.info(
                    "[iter %d replay] thread=%s self-persisting history: sending %d approval "
                    "response(s) ALONE, no synthetic replay (UDR-0125 D1). sources=%s\n  responses: %s",
                    total_rounds,
                    thread_id,
                    len(approval_response_contents),
                    round_sources,
                    ", ".join(approval_debug.describe_content(c) for c in approval_response_contents),
                )
                # NOT `continue`: the shared tail below (service_session_id reset,
                # UDR-0082 round accounting and budget enforcement) applies to
                # every lane and must not be skipped.
                _build_replay = False
            else:
                _build_replay = True

            # PRP-0069 follow-up: append BOTH the iter-N synthetic assistant
            # message (carrying the originating function_call(s) plus the
            # accumulated text -- and, for Anthropic, the signed reasoning) AND
            # the user message bundling every approval response. Without the
            # synthetic assistant, MAF's Anthropic connector emits a tool_result
            # block with no preceding tool_use (HTTP 400 orphan tool_result).
            #
            # Reconstruction is provider-aware (defect fix): OpenAI reasoning
            # models (gpt-5.x Responses API) link each function_call server item
            # (fc_...) to the reasoning item (rs_...) emitted with it. We cannot
            # replay that exact rs_ item, so replaying the original function_call
            # (with its fc_ id) returns HTTP 400 "function_call ... provided
            # without its required 'reasoning' item". For OpenAI we therefore
            # drop the reasoning and rebuild function_calls without server item
            # ids (matched by call_id). Anthropic keeps the signed reasoning +
            # original tool_use Content the API requires.
            if _build_replay:
                # PRP-0154 / UDR-0132 D1. SETTLE BEFORE APPENDING. Everything already
                # in `iteration_messages` has been through at least one agent.run, so
                # every approval response it carries has been offered to MAF and its
                # ticket is SPENT -- MAF pops the pending entry on the first bind
                # (_tools._bind_approval_response_to_pending_request, consume=True) and
                # silently discards a later replay of the same response while leaving
                # OUR function_call in the request. From round 3 on, that reached the
                # provider as a call with no output (400 "No tool output found for
                # function call ..."), on every provider, because the consumption
                # happens inside MAF ahead of any connector.
                #
                # This runs BEFORE the append below, so the round being submitted --
                # `approval_response_contents`, not yet in the list -- is untouched by
                # construction. Spentness is established by POSITION in the loop, not
                # by a liveness heuristic, so no live approval can be damaged.
                iteration_messages, settled_ids, dropped_ids = approval_debug.settle_consumed_approvals(
                    iteration_messages
                )
                if settled_ids or dropped_ids:
                    approval_debug.logger.info(
                        "[iter %d settle] thread=%s settled=%s dropped-with-call=%s",
                        total_rounds,
                        thread_id,
                        sorted(settled_ids),
                        sorted(dropped_ids),
                    )
                is_anthropic = providers.provider_for(effective_model).name == "anthropic"
                iter_synthetic_messages = iter_accumulator.build_iteration_messages(
                    approval_response_contents,
                    include_reasoning=is_anthropic,
                    strip_function_call_ids=not is_anthropic,
                    # PRP-0141 / UDR-0119 D6: replay the calls MAF deferred ONLY when
                    # MAF will resume them, and never answer for them. Read BEFORE the
                    # re-run below, which is where MAF pops the group.
                    resumable_call_ids=(resumable_ids := maf_resumable_call_ids(session)),
                )
                approval_debug.logger.info(
                    "[iter %d replay] thread=%s provider=%s sources=%s resumable_call_ids=%s\n"
                    "  responses: %s\n  replay: %s",
                    total_rounds,
                    thread_id,
                    "anthropic" if is_anthropic else "openai-family",
                    round_sources,
                    sorted(resumable_ids),
                    ", ".join(approval_debug.describe_content(c) for c in approval_response_contents),
                    " | ".join(approval_debug.describe_messages(iter_synthetic_messages)),
                )
                iteration_messages = [*iteration_messages, *iter_synthetic_messages]
            # PRP-0141 multi-iteration follow-up: a call MAF DEFERRED in an earlier
            # round is resumed by MAF in the FOLLOWING iteration, which produces its
            # result on that iteration's request but never streams it back -- so the
            # accumulator cannot capture it and this replay leaves the function_call
            # bare. A LATER iteration then re-sends it with no matching output and the
            # provider rejects the turn (400 "No tool output found for function call
            # ..."). Heal by appending the result captured from the wire, so every
            # replayed function_call carries its output.
            # Only meaningful where a replay was built: the healer repairs
            # REPLAYED function_calls left without their output. A self-persisting
            # lane replays nothing, so there is nothing to heal (UDR-0125 D1).
            if _build_replay:
                # UDR-0132 D2: the calls answered by THIS round are the only approvals
                # whose ticket is unspent, so they are the only ones that predict an
                # output. Passed in explicitly -- the healer must never re-derive this
                # by scanning the messages, which is what let a spent response from an
                # earlier round pass as an answer.
                iteration_messages, healed_ids = approval_debug.heal_dangling_tool_calls(
                    iteration_messages,
                    live_approval_call_ids=[
                        cid
                        for cid in (approval_debug.approval_response_call_id(c) for c in approval_response_contents)
                        if cid
                    ],
                )
                if healed_ids:
                    approval_debug.logger.info(
                        "[iter %d heal] thread=%s appended captured results for deferred call(s) whose "
                        "output MAF produced but never streamed: %s",
                        total_rounds,
                        thread_id,
                        sorted(healed_ids),
                    )
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

            # PRP-0103 / UDR-0082 D2: account this round AFTER it resolved. A
            # round whose approvals were ALL resolved from the session cache is
            # free with respect to the human-interactive budget; every round
            # still counts toward the absolute backstop. Aborting when either
            # ceiling is crossed preserves the runaway "last stop".
            total_rounds += 1
            round_cached = bool(round_sources) and all(s == "session-cache" for s in round_sources)
            if not round_cached:
                interactive_rounds += 1
            # PRP-0146 / UDR-0082 D7: a runaway is characterised by not getting
            # anywhere, so the autonomous lane counts CONSECUTIVE unproductive
            # rounds -- no tool executed and no text emitted. Reasoning is not
            # progress. A gated flow alternates ("request approval" then "execute
            # it and request the next"), so healthy operation never exceeds one.
            productive = round_is_productive(iter_accumulator)
            if iter_accumulator.executed_any():
                tools_executed_total += len(iter_accumulator.executed_call_ids())
            no_progress_rounds = 0 if productive else no_progress_rounds + 1
            if interactive_rounds > max_approval_iterations:
                approval_loop_exceeded = True
                loop_stop_reason = "interactive"
                break
            if total_rounds > absolute_max_iterations:
                approval_loop_exceeded = True
                loop_stop_reason = "absolute"
                break
            if no_progress_limit is not None and no_progress_rounds >= no_progress_limit:
                approval_loop_exceeded = True
                loop_stop_reason = "no_progress"
                break

        if approval_loop_exceeded:
            # The budget was exhausted without the agent settling. Defensive:
            # emit a RUN_ERROR so the SPA surfaces the run as failed instead of
            # silently terminating.
            run_error = True
            # PRP-0146 / UDR-0082 D10: on a self-re-invoking lane the side effects
            # of the turn are ALREADY on disk -- todos completed, files edited,
            # commands run -- and only the account of them is being discarded. The
            # message therefore reports the work rather than only naming the number
            # that was crossed. The non-autonomous texts are UNCHANGED.
            _worked = f" {tools_executed_total} tool(s) executed in this turn; that work is not rolled back."
            if loop_stop_reason == "no_progress":
                error_message = (
                    f"This autonomous run was stopped because {no_progress_rounds} consecutive rounds "
                    f"executed no tool and produced no output -- the agent appears stuck."
                    f"{_worked} Start a new turn to continue, or narrow the request."
                )
            elif loop_stop_reason == "absolute" and autonomous:
                error_message = (
                    f"This autonomous run was stopped after {absolute_max_iterations} approval rounds."
                    f"{_worked} Start a new turn to continue, or narrow the request."
                )
            elif loop_stop_reason == "absolute":
                error_message = (
                    f"Tool approval loop exceeded the absolute ceiling of "
                    f"{absolute_max_iterations} rounds; aborting run."
                )
            else:
                error_message = (
                    f"Tool approval loop exceeded {max_approval_iterations} interactive rounds; aborting run."
                )
            if autonomous:
                # Additive CUSTOM event so the SPA can render the counts; no new
                # event TYPE and nothing changes on the non-autonomous path.
                yield encoder.encode(
                    CustomEvent(
                        type=EventType.CUSTOM,
                        name="autonomous_run_stopped",
                        value={
                            "reason": loop_stop_reason,
                            "rounds": total_rounds,
                            "tools_executed": tools_executed_total,
                            "no_progress_rounds": no_progress_rounds,
                        },
                    )
                )
            logger.warning(
                "approval loop exceeded for thread %s (reason=%s interactive=%d/%d total=%d/%d "
                "no_progress=%d/%s tools_executed=%d autonomous=%s)",
                thread_id,
                loop_stop_reason,
                interactive_rounds,
                max_approval_iterations,
                total_rounds,
                absolute_max_iterations,
                no_progress_rounds,
                no_progress_limit if no_progress_limit is not None else "-",
                tools_executed_total,
                autonomous,
            )

    except (OpenAINotFoundError, ChatClientException, TypeError) as exc:
        run_error = True
        error_message, known = _classify_run_error(
            exc, continuation_token=bool(continuation_token), harness_run=harness_run
        )
        if known:
            logger.warning("AG-UI run error for thread %s: %s", thread_id, exc)
        else:
            logger.exception("AG-UI stream error")
        # PRP-0141 tracing: which approval iteration died. The preceding
        # "[wire]" line from app.agent.approval_trace holds the request body shape.
        approval_debug.logger.error(
            "[run failed] thread=%s rounds total=%s interactive=%s error=%s "
            "attempt=%s elapsed=%.1fs updates=%d -- see the last [wire] line above",
            thread_id,
            locals().get("total_rounds"),
            locals().get("interactive_rounds"),
            type(exc).__name__,
            run_stats.attempt,
            run_stats.elapsed,
            run_stats.updates,
        )

    except Exception as exc:
        # A RAW provider error not wrapped in the typed exceptions above (e.g.
        # anthropic.BadRequestError for an out-of-credits 400) reaches here; run
        # it through the same classifier so billing / rate-limit / transient
        # causes still surface an actionable message instead of the generic one.
        run_error = True
        error_message, known = _classify_run_error(
            exc, continuation_token=bool(continuation_token), harness_run=harness_run
        )
        if known:
            logger.warning("AG-UI run error for thread %s: %s", thread_id, exc)
        else:
            logger.exception("AG-UI stream error")
    finally:
        # Abort / client disconnect (UDR-0053 D17). On a user stop or a dropped
        # connection the stream is cancelled (CancelledError / GeneratorExit, a
        # BaseException not caught above), so the post-try finalization below --
        # including the title dispatch -- is skipped. Detect a propagating exception
        # here and reconcile the Auto Session Title spinner so it never animates
        # forever. Normal and caught-error completions leave no propagating
        # exception (the except consumed it) and are handled below, so this does not
        # double-dispatch. Sync dispatch only; never yields.
        if sys.exc_info()[0] is not None:
            _dispatch_title_clear()

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
        # The generation task will not run on an errored turn, so clear the
        # auto_title_pending spinner instead (UDR-0053 D17).
        _dispatch_title_clear()
    else:
        logger.info("AG-UI run finished normally: thread=%s", thread_id)
        # Auto Session Title (PRP-0077, CTR-0109, UDR-0053). On the FIRST
        # completed assistant turn of a NON-temporary session, dispatch a
        # background task (CTR-0108) to summarize the opening exchange into a
        # concise title. Fire-and-forget and error-isolated: it never blocks or
        # fails the turn. Gated by SESSION_TITLE_MODE=llm; the default "truncate"
        # dispatches nothing (byte-for-byte pre-PRP-0077 behavior). The SSE
        # grammar is unchanged -- no event is emitted for the title.
        if settings.session_title_mode == "llm" and not temporary and is_first_turn:
            assistant_text = "".join(assistant_text_parts).strip()
            user_text = _first_user_text(request_body.messages)
            if user_text and assistant_text:
                from app.background import dispatch as _dispatch_background

                _dispatch_background(
                    "session-title",
                    dedup_key=thread_id,
                    ctx={
                        "thread_id": thread_id,
                        "user_text": user_text,
                        "assistant_text": assistant_text,
                        "model": effective_model,
                    },
                )
                title_dispatched = True
        # First turn completed but produced no text to summarize (e.g. a tool-only
        # turn): the generation task was not dispatched, so clear the pending
        # spinner instead (UDR-0053 D17). No-op on later turns / non-llm mode.
        if not title_dispatched:
            _dispatch_title_clear()
        # User Memory Background Extraction (PRP-0079, CTR-0117, UDR-0051 Phase 2,
        # resolving D5). On EVERY completed turn of a NON-temporary session, when
        # enabled, dispatch the extraction task (CTR-0108). The cadence cursor
        # inside the task throttles the actual LLM pass; below the threshold it is
        # a cheap no-op. Fire-and-forget and error-isolated, like the title task.
        # Default OFF (USER_MEMORY_EXTRACTION=false) dispatches nothing --
        # byte-for-byte Phase 1 behavior.
        if settings.user_profile_enabled and settings.user_memory_extraction and not temporary:
            from app.background import dispatch as _dispatch_background

            _dispatch_background(
                "user-memory-extract",
                dedup_key=thread_id,
                ctx={
                    "thread_id": thread_id,
                    "messages": request_body.messages,
                    "assistant_text": "".join(assistant_text_parts).strip(),
                    "model": effective_model,
                },
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
