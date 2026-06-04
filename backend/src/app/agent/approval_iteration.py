"""PRP-0067 outer-loop iteration content accumulator (PRP-0069 follow-up).

The AG-UI endpoint (CTR-0009 v12) wraps MAF's ``agent.run()`` in an outer loop
that pauses on ``function_approval_request`` content and re-runs the agent
with the operator's decision appended. Originally only the
``function_approval_response`` was appended to ``iteration_messages``, so the
re-run started without the iter-N assistant context. MAF's Anthropic
connector (``agent_framework_anthropic._chat_client``) then emitted a
``tool_result`` block with no preceding ``tool_use`` block, which Anthropic
rejects with HTTP 400::

    messages.*.content.*: unexpected `tool_use_id` found in `tool_result`
    blocks: <id>. Each `tool_result` block must have a corresponding
    `tool_use` block in the previous message.

OpenAI tolerated the missing context; Anthropic does not.

This accumulator observes the iter-N stream content and, at the iteration
boundary, produces the iter-N+1 input pair::

    [..., iter_N_assistant_synthetic, iter_N_user_with_approval_responses]

so MAF + Anthropic see a properly paired conversation with FULL iter-N tool
history -- every ``tool_use`` from iter-N (gated and non-gated) is reflected
in the synthetic assistant, and every executed ``tool_result`` from iter-N
is in the synthetic user, alongside the operator's approval responses for
the gated tools that paused::

    assistant(reasoning_signed + text + tool_use_X + tool_use_Y + tool_use_W_gated)
    user(tool_result_X + tool_result_Y + tool_result_W_from_approval)

Capturing **both** gated and non-gated tool history is critical to avoid an
agent re-execution loop: without the non-gated calls in the synthetic
assistant, the iter-N+1 model call sees only "user asked X -> assistant
called the gated tool" and starts over (re-globbing, re-reading, re-writing),
which compounds across iterations and quickly trips Anthropic's per-minute
rate limit (429 Too Many Requests).

The synthetic assistant preserves Anthropic's ``thinking`` block signature
(stored on ``TextReasoningContent.protected_data``, round-tripped by the MAF
connector at serialization time).

Reconstruction is provider-aware (``build_iteration_messages`` flags), because
OpenAI reasoning models impose the OPPOSITE constraint from Anthropic:

- Anthropic needs the signed ``thinking`` block replayed and the original
  ``tool_use`` Content preserved (``include_reasoning=True``,
  ``strip_function_call_ids=False``).
- OpenAI reasoning models (gpt-5.x on the Responses API) link each
  ``function_call`` server item (``fc_...``) to the ``reasoning`` item
  (``rs_...``) emitted with it. We cannot replay that exact ``rs_`` item, so
  replaying the original function_call (with its ``fc_`` id) provokes HTTP 400
  ("function_call ... provided without its required 'reasoning' item"). OpenAI
  therefore drops the reasoning and rebuilds function_calls without server item
  ids (``include_reasoning=False``, ``strip_function_call_ids=True``), matched
  only by ``call_id`` -- the standard non-reasoning tool-replay shape.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_framework import Content, Message

logger = logging.getLogger(__name__)


class IterationContentAccumulator:
    """Capture an outer-loop iteration's streamed content for iter-N+1 input.

    Use one accumulator per outer-loop iteration. Call the ``observe_*``
    methods as content updates arrive on the AG-UI stream. After the inner
    stream completes with pending approvals, call ``build_iteration_messages``
    to produce ``[synthetic_assistant, synthetic_user_with_responses]`` to
    append to ``iteration_messages`` before the next ``agent.run`` call.
    """

    def __init__(self) -> None:
        self._reasoning_text_chunks: list[str] = []
        self._reasoning_signature: str | None = None
        self._text_chunks: list[str] = []
        # Function calls: ordered by first observation; per-call info accumulated.
        self._function_call_order: list[str] = []
        self._function_call_name: dict[str, str] = {}
        # args may arrive as either a streamed string (delta-concat) or a
        # complete structured value (dict-or-final). Track both paths and
        # prefer the structured value if present at build time.
        self._function_call_args_chunks: dict[str, list[str]] = {}
        self._function_call_args_structured: dict[str, Any] = {}
        # function_call Content objects taken from function_approval_request
        # (full args, no reconstruction needed). Preferred over the streamed
        # form when both exist for the same call_id.
        self._function_call_from_approval: dict[str, Content] = {}
        # function_result Content keyed by call_id (latest observation wins).
        self._function_results: dict[str, Content] = {}

    # ---- text-like content ----

    def observe_text_reasoning(self, content: Any) -> None:
        """Append a text_reasoning delta to the accumulated reasoning text.

        The Anthropic ``thinking`` block signature arrives on the final
        ``signature_delta``; we capture it from any update that has a non-empty
        ``protected_data`` and keep the latest. ``protected_data`` carries the
        signature in MAF's connector mapping.
        """
        text = getattr(content, "text", None)
        if text:
            self._reasoning_text_chunks.append(text)
        signature = getattr(content, "protected_data", None)
        if signature:
            self._reasoning_signature = signature

    def observe_text(self, content: Any) -> None:
        """Append a text delta to the accumulated assistant text."""
        text = getattr(content, "text", None)
        if text:
            self._text_chunks.append(text)

    # ---- function call / result content ----

    def _ensure_call_order(self, call_id: str) -> None:
        if call_id not in self._function_call_order:
            self._function_call_order.append(call_id)

    def observe_function_call(self, content: Any) -> None:
        """Observe a non-approval ``function_call`` content emitted in the stream.

        The Content is delta-streamed: ``arguments`` is a chunk of the final
        JSON string (or a complete structured value once the connector finishes
        parsing). We accumulate the string chunks per ``call_id`` and replace
        on a structured value, then reconstruct the canonical Content at build
        time.
        """
        call_id = getattr(content, "call_id", None)
        if not call_id:
            return
        self._ensure_call_order(call_id)
        name = getattr(content, "name", None)
        if name:
            self._function_call_name[call_id] = name
        args = getattr(content, "arguments", None)
        if args is None:
            return
        if isinstance(args, str):
            self._function_call_args_chunks.setdefault(call_id, []).append(args)
        else:
            # Mapping (dict) or other structured arguments -- this is the final
            # parsed form. Prefer it over the streamed-chunk reconstruction.
            self._function_call_args_structured[call_id] = args

    def observe_function_call_from_approval(self, function_call: Content | None) -> None:
        """Record the function_call extracted from a function_approval_request.

        The function_approval_request content wraps the original
        FunctionCallContent (with complete arguments) in ``.function_call``.
        Keeping this Content object lets the MAF Anthropic connector serialize
        it back into a ``tool_use`` block in iter N+1's assistant message.
        """
        if function_call is None:
            return
        call_id = getattr(function_call, "call_id", None)
        if not call_id:
            return
        self._ensure_call_order(call_id)
        self._function_call_from_approval[call_id] = function_call

    def observe_function_result(self, content: Any) -> None:
        """Record a ``function_result`` content keyed by ``call_id``.

        Non-approval-gated tools execute inline in iter-N; their results must
        be re-fed in iter-N+1 to prevent the model from re-running them and
        exhausting the rate limit (PRP-0067 follow-up rationale).
        """
        call_id = getattr(content, "call_id", None)
        if call_id:
            self._function_results[call_id] = content

    def has_pending_function_calls(self) -> bool:
        """True when at least one approval-gated function_call was observed."""
        return bool(self._function_call_from_approval)

    # ---- iteration boundary ----

    def _streamed_args(self, call_id: str) -> str | Any | None:
        """Best-available arguments for ``call_id`` from the streamed observation.

        A structured (parsed) value wins over the joined string chunks.
        """
        if call_id in self._function_call_args_structured:
            return self._function_call_args_structured[call_id]
        chunks = self._function_call_args_chunks.get(call_id)
        return "".join(chunks) if chunks else None

    def _resolved_function_call(self, call_id: str, *, strip_server_item_ids: bool = False) -> Content:
        """Build the canonical Content for ``call_id`` for the synthetic assistant.

        Priority order:
        1. The Content from a function_approval_request (complete by construction).
        2. A streamed function_call rebuilt from its captured name + args. When
           a structured args value was observed, it wins over the joined chunks.

        ``strip_server_item_ids`` (OpenAI reasoning models): NEVER return the
        approval's original Content. The original FunctionCallContent carries
        the provider's server item id (``fc_...``) in its raw_representation,
        which the OpenAI Responses API links to the reasoning item (``rs_...``)
        emitted alongside it in the original response. Replaying that
        function_call without its exact reasoning item triggers HTTP 400
        ("Item 'fc_...' of type 'function_call' was provided without its
        required 'reasoning' item: 'rs_...'"). A freshly built function_call
        (matched only by ``call_id``) carries no such linkage and is accepted.
        """
        from_approval = self._function_call_from_approval.get(call_id)
        if from_approval is not None and not strip_server_item_ids:
            return from_approval
        name = self._function_call_name.get(call_id, "")
        args: str | Any | None
        if from_approval is not None:
            # Rebuild from the approval Content's fields, dropping its
            # raw_representation / server item id.
            name = getattr(from_approval, "name", None) or name
            args = getattr(from_approval, "arguments", None)
            if args is None:
                args = self._streamed_args(call_id)
        else:
            args = self._streamed_args(call_id)
        return Content.from_function_call(call_id=call_id, name=name, arguments=args)

    def build_iteration_messages(
        self,
        approval_response_contents: list[Content],
        *,
        include_reasoning: bool = True,
        strip_function_call_ids: bool = False,
    ) -> list[Message]:
        """Build ``[synthetic_assistant, synthetic_user]`` for iter N+1's input.

        The synthetic assistant carries the accumulated reasoning (with the
        captured signature), the accumulated text, and EVERY function_call
        observed in this iteration (gated and non-gated), in observation order.
        The synthetic user carries every executed function_result (matching
        order) followed by the operator's function_approval_response contents
        for the gated tools that paused.

        Provider-aware reconstruction (defaults preserve the Anthropic path):

        - ``include_reasoning`` (Anthropic: True): replay the ``thinking`` block
          with its signature so the connector re-emits a valid signed reasoning
          block paired with its ``tool_use``. OpenAI passes False -- a replayed
          reasoning item without its original ``rs_`` id is useless and, paired
          with a server-id-bearing function_call, provokes the Responses API
          reasoning/function_call pairing 400.
        - ``strip_function_call_ids`` (OpenAI: True): rebuild every function_call
          without the provider server item id so the Responses API does not
          demand the matching reasoning item (see ``_resolved_function_call``).
          Anthropic passes False to keep the original ``tool_use`` Content.

        Empty assistant or user messages are skipped so a degenerate
        iteration (e.g., no observed content) does not inject a no-op Message
        into the conversation.
        """
        assistant_contents: list[Content] = []
        if include_reasoning:
            reasoning_text = "".join(self._reasoning_text_chunks)
            if reasoning_text:
                assistant_contents.append(
                    Content.from_text_reasoning(
                        text=reasoning_text,
                        protected_data=self._reasoning_signature,
                    )
                )
        text = "".join(self._text_chunks)
        if text:
            assistant_contents.append(Content.from_text(text=text))
        assistant_contents.extend(
            self._resolved_function_call(cid, strip_server_item_ids=strip_function_call_ids)
            for cid in self._function_call_order
        )

        user_contents: list[Content] = []
        for call_id in self._function_call_order:
            result = self._function_results.get(call_id)
            if result is not None:
                user_contents.append(result)
        user_contents.extend(approval_response_contents)

        # Defensive log: if there is at least one tool_use without a matching
        # result AND no approval response to substitute for it, the next
        # Anthropic request would be malformed. This should not happen in
        # practice (gated tools always produce an approval response, non-gated
        # ones always produce a function_result) but the log catches drift.
        if assistant_contents and self._function_call_order:
            pending_call_ids = {
                call_id
                for call_id in self._function_call_order
                if call_id not in self._function_results and call_id not in self._function_call_from_approval
            }
            if pending_call_ids:
                logger.warning(
                    "Outer-loop iteration produced function_call(s) without a function_result or approval response: %s",
                    sorted(pending_call_ids),
                )

        out: list[Message] = []
        if assistant_contents:
            out.append(Message(role="assistant", contents=assistant_contents))
        if user_contents:
            out.append(Message(role="user", contents=user_contents))
        return out


__all__ = ["IterationContentAccumulator"]
