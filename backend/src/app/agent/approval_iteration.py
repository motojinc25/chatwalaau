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

so MAF + Anthropic see a properly paired conversation with the iter-N tool
history -- every ``tool_use`` from iter-N that EXECUTED or asked for approval
(gated and non-gated alike) is reflected in the synthetic assistant, and every
executed ``tool_result`` from iter-N is in the synthetic user, alongside the
operator's approval responses for the gated tools that paused::

    assistant(reasoning_signed + text + tool_use_X + tool_use_Y + tool_use_W_gated)
    user(tool_result_X + tool_result_Y + tool_result_W_from_approval)

Capturing **both** gated and non-gated tool history is critical to avoid an
agent re-execution loop: without the non-gated calls in the synthetic
assistant, the iter-N+1 model call sees only "user asked X -> assistant
called the gated tool" and starts over (re-globbing, re-reading, re-writing),
which compounds across iterations and quickly trips Anthropic's per-minute
rate limit (429 Too Many Requests).

Deferral (PRP-0108 -> PRP-0141): a round that contains an approval-gated call
DEFERS every other tool call of that round -- non-gated tools do not execute
inline, their ``function_call`` streams bare (no ``function_result``, no
approval request), and on resume MAF executes only calls carrying an approval
response. Under MAF 1.10 the builder synthesized an APPROVED
``function_approval_response`` for each deferred call so it executed once on
resume. Since MAF 1.13 that synthesis is WRONG in every lane: MAF stores the
deferred siblings itself, on the invocation ``AgentSession``
(``state["tool_approval"]["already_approved_approval_request_groups"]``,
``_tools._store_already_approved_approval_requests``), and restores them as
approved responses the moment the genuinely gated response arrives. The
synthesized copy is then a SECOND response for the same call id with only one
execution result to consume it, so the survivor reaches the OpenAI Responses
connector as an ``mcp_approval_response`` naming a request that never existed::

    400 The following MCP approval requests have approval responses but
        weren't passed as input: call_...

A deferred call is therefore REPLAYED BUT NEVER ANSWERED -- and replayed only
when MAF will actually resume it (UDR-0119 D6 as amended). The two halves are
one rule with one criterion, ``maf_resumable_call_ids(session)``:

- MAF holds the call -> its ``function_result`` WILL appear in the resumed turn,
  so the ``function_call`` must be in the input beside it. Omitting it yields the
  mirror-image rejection, ``400 No tool call found for function call output with
  call_id call_...``, whenever the provider (not MAF) owns the history: with
  ``STORES_BY_DEFAULT=True`` and ``service_session_id`` cleared for the resume,
  this replay is the ONLY source of the assistant turn.
- MAF does not hold it (a session-less run) -> nothing will produce its output,
  so replaying it would strand the call. It is omitted and the model re-issues.

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
from typing import TYPE_CHECKING, Any

from agent_framework import Content, Message

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

logger = logging.getLogger(__name__)

# MAF's private session-state location for the calls it deferred in a gated
# round (agent_framework._tools._TOOL_APPROVAL_STATE_KEY /
# _ALREADY_APPROVED_APPROVAL_REQUEST_GROUPS_KEY). Read-only, defensively: the
# reader below returns an empty set on ANY shape it does not recognise, which
# degrades to "MAF resumes nothing" -- deferred calls are then omitted and the
# model re-issues them, rather than being stranded without an output.
_MAF_TOOL_APPROVAL_STATE_KEY = "tool_approval"
_MAF_DEFERRED_GROUPS_KEY = "already_approved_approval_request_groups"


def maf_resumable_call_ids(session: Any) -> frozenset[str]:
    """Return the call ids MAF will resume on its own for ``session``.

    Since MAF 1.13 a round containing an approval-gated call hides its non-gated
    siblings, stores them on the invocation ``AgentSession``, and restores them
    as APPROVED responses once the gated decision arrives
    (``_store_already_approved_approval_requests`` /
    ``_pop_already_approved_approval_responses``). Those calls therefore produce
    a ``function_result`` in the resumed turn, and their ``function_call`` must
    be replayed beside it -- while a deferred call MAF does NOT hold produces
    nothing and must not be replayed.

    MUST be read BEFORE the resume run: MAF pops the group during it.

    This reaches into upstream-private state deliberately. The alternative is to
    GUESS which of the two shapes applies, and both guesses have now shipped as
    a 400 (PRP-0135's fabricated response, PRP-0141's over-broad omission). The
    key is pinned by an upstream canary test, and every failure mode here is a
    quiet, correct empty set.
    """
    state = getattr(session, "state", None)
    if not isinstance(state, dict):
        return frozenset()
    approval_state = state.get(_MAF_TOOL_APPROVAL_STATE_KEY)
    # The harness ToolApprovalMiddleware may leave a typed state object here;
    # MAF itself normalizes it to a dict on first access.
    if not isinstance(approval_state, dict):
        to_dict = getattr(approval_state, "to_dict", None)
        approval_state = to_dict(exclude={"type"}) if callable(to_dict) else None
    if not isinstance(approval_state, dict):
        return frozenset()
    groups = approval_state.get(_MAF_DEFERRED_GROUPS_KEY)
    if not isinstance(groups, list):
        return frozenset()

    call_ids: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        requests = group.get("approval_requests")
        if not isinstance(requests, list):
            continue
        for request in requests:
            if not isinstance(request, dict):
                continue
            function_call = request.get("function_call")
            call_id = function_call.get("call_id") if isinstance(function_call, dict) else None
            call_id = call_id or request.get("id")
            if isinstance(call_id, str) and call_id:
                call_ids.add(call_id)
    return frozenset(call_ids)


def settle_pending_tool_content(messages: list[Message], results: Mapping[str, Content]) -> tuple[list[str], list[str]]:
    """Write the results a resumed run produced back into the accumulated conversation.

    ``build_iteration_messages`` can only describe what was known when the round
    PAUSED: the operator's ``function_approval_response`` for the gated call, and
    a bare ``function_call`` for each deferred sibling MAF will resume. Both are
    promises about the run that is about to happen, and MAF keeps them -- but only
    for that one run, on its own copy of the messages. The accumulated
    ``iteration_messages`` this loop carries forward keeps the promises instead of
    the outcome, and from the SECOND approval round on, the provider sees them:

    - the deferred call's ``tool_use`` never gets its ``tool_result``::

          400 messages.N: `tool_use` ids were found without `tool_result` blocks
              immediately after: toolu_...

      (Anthropic states it; the OpenAI Responses API rejects the same transcript
      as a function_call with no output.)
    - the still-unconverted approval response is collected AGAIN by MAF, so the
      approved tool RE-EXECUTES once per subsequent round -- silently, since it
      succeeds. For ``bash_execute`` that is the operator's command running again
      without a second approval.

    So once the results are known -- they arrive on the very next stream, because
    a resumed run executes the approved and restored calls before it reaches the
    model -- they REPLACE the promises: each ``function_approval_response`` becomes
    its ``function_result``, and every unanswered ``function_call`` is paired with
    the result it produced. The conversation we keep then says what happened rather
    than what was expected to.

    Returns ``(settled_call_ids, still_unanswered_call_ids)``; the second list is
    an incomplete transcript this function could not repair and is worth logging.
    """
    if not messages:
        return [], []
    settled: list[str] = []

    # 1. Promise -> outcome: an approval response whose call has a result becomes
    #    that result. Anything still awaiting its result is left untouched.
    for message in messages:
        replaced: list[Content] = []
        for content in message.contents:
            if content.type != "function_approval_response":
                replaced.append(content)
                continue
            wrapped = getattr(content, "function_call", None)
            call_id = getattr(wrapped, "call_id", None) or getattr(content, "call_id", None)
            result = results.get(call_id) if call_id else None
            if result is None:
                replaced.append(content)
                continue
            replaced.append(result)
            settled.append(call_id)
        if len(replaced) == len(message.contents):
            message.contents = replaced

    # 2. Pair every function_call with a result in the message that FOLLOWS it,
    #    which is where both providers require it.
    unanswered: list[str] = []
    for index, message in enumerate(messages):
        call_ids = [c.call_id for c in message.contents if c.type == "function_call" and c.call_id]
        if not call_ids:
            continue
        follower = messages[index + 1] if index + 1 < len(messages) else None
        if follower is None or follower.role == "assistant":
            # No message to hold the results: only reachable for the round still
            # in flight, whose results do not exist yet.
            unanswered.extend(cid for cid in call_ids if cid not in results)
            continue
        answered = {
            getattr(c, "call_id", None) or getattr(getattr(c, "function_call", None), "call_id", None)
            for c in follower.contents
        }
        missing = [cid for cid in call_ids if cid not in answered]
        recovered = [results[cid] for cid in missing if cid in results]
        if recovered:
            follower.contents = [*follower.contents, *recovered]
            settled.extend(c.call_id for c in recovered if c.call_id)
        unanswered.extend(cid for cid in missing if cid not in results)

    return settled, unanswered


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

    def observed_results(self) -> dict[str, Content]:
        """Every ``function_result`` seen this iteration, keyed by call id.

        Includes results for calls issued in an EARLIER iteration: a resumed run
        executes the previously approved call and the deferred ones it restored
        before the model is called again, and those results are what
        ``settle_pending_tool_content`` writes back into the accumulated
        conversation.
        """
        return dict(self._function_results)

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
        resumable_call_ids: Collection[str] = (),
    ) -> list[Message]:
        """Build ``[synthetic_assistant, synthetic_user]`` for iter N+1's input.

        The synthetic assistant carries the accumulated reasoning (with the
        captured signature), the accumulated text, and every function_call
        observed in this iteration that either EXECUTED or asked for approval,
        in observation order. The synthetic user carries every executed
        function_result (matching order) followed by the operator's
        function_approval_response contents for the gated tools that paused.

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

        - ``resumable_call_ids`` (PRP-0141): the call ids MAF will resume by
          itself, from ``maf_resumable_call_ids(session)``, read BEFORE the resume
          run. A DEFERRED call is replayed iff it is in this set, and is NEVER
          answered either way. Both halves are load-bearing and each has shipped
          as a 400 when got wrong: fabricating an approval response for a call
          that issued no REQUEST collides with MAF's own restore (``400 ... MCP
          approval requests have approval responses but weren't passed as
          input``), while omitting a call MAF WILL resume leaves its result
          unpaired (``400 No tool call found for function call output with
          call_id ...``). The genuinely gated call always keeps its real
          approval response.

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

        # Calls that neither executed nor asked for approval: MAF deferred them.
        # A deferred call is replayed only when MAF will resume it (its result
        # then needs the matching call in the input) and is NEVER answered here.
        # See the module docstring and UDR-0119 D6 (amended by PRP-0141).
        deferred_call_ids = {
            call_id
            for call_id in self._function_call_order
            if call_id not in self._function_results and call_id not in self._function_call_from_approval
        }
        stranded_call_ids = deferred_call_ids - set(resumable_call_ids)
        replayed_call_ids = [cid for cid in self._function_call_order if cid not in stranded_call_ids]
        if deferred_call_ids:
            logger.info(
                "Outer-loop iteration deferred function_call(s): replayed unanswered (MAF resumes "
                "them) %s; omitted (nothing will produce their result, the model re-issues) %s",
                sorted(deferred_call_ids & set(resumable_call_ids)),
                sorted(stranded_call_ids),
            )
        assistant_contents.extend(
            self._resolved_function_call(cid, strip_server_item_ids=strip_function_call_ids)
            for cid in replayed_call_ids
        )

        user_contents: list[Content] = []
        for call_id in replayed_call_ids:
            result = self._function_results.get(call_id)
            if result is not None:
                user_contents.append(result)
        user_contents.extend(approval_response_contents)

        # PAIRING INVARIANT (PRP-0134 follow-up, UDR-0117). Anthropic rejects the whole
        # request when any tool_result names a tool_use that is not in the message before
        # it, and the two lists above are assembled from independent observations -- the
        # assistant side from streamed function_calls, the user side from results and
        # from the operator's approval responses. Every producer above is *believed* to
        # keep them in step; this enforces it, because the failure mode is a 400 that
        # aborts the turn and names only an opaque id.
        #
        # An Anthropic run that used Agent Skills reached exactly that 400, and the
        # producing path was not identifiable from the code alone -- so the unpaired
        # content is dropped and LOGGED with its call id and kind, which turns a silent
        # structural break into a named one the next occurrence can be traced from.
        paired_ids = {
            cid
            for cid in (getattr(c, "call_id", None) for c in assistant_contents)
            if cid  # text / reasoning contents carry no call_id
        }
        kept_user: list[Content] = []
        dropped: list[str] = []
        recovered: list[str] = []
        for content in user_contents:
            # The identifier that must pair is the TOOL CALL id, and only a
            # function_result carries it directly. A function_approval_response's own
            # ``id`` is the APPROVAL REQUEST id (``req_...``), a different namespace from
            # the call id (``toolu_...``) -- reading it here would compare unrelated
            # strings and discard perfectly valid approvals. Its call id lives on the
            # wrapped function_call.
            wrapped_call = getattr(content, "function_call", None)
            call_id = getattr(content, "call_id", None) or getattr(wrapped_call, "call_id", None)
            if call_id is None or call_id in paired_ids:
                kept_user.append(content)
                continue
            # REPAIR before dropping. A function_approval_response carries the originating
            # function_call, so an unpaired one is recoverable: adopt that call into the
            # assistant message and the pair is complete. Dropping here would discard the
            # operator's decision -- a worse outcome than the extra tool_use, and an
            # avoidable one whenever the information is right there.
            if wrapped_call is not None:
                assistant_contents.append(wrapped_call)
                paired_ids.add(call_id)
                recovered.append(call_id)
                kept_user.append(content)
                continue
            dropped.append(f"{getattr(content, 'type', type(content).__name__)}:{call_id}")
        if recovered:
            logger.info(
                "Outer-loop iteration adopted the originating function_call for approval "
                "response(s) whose call was not observed in this iteration's stream: %s",
                sorted(recovered),
            )
        if dropped:
            logger.warning(
                "Outer-loop iteration produced tool content with no matching function_call in the "
                "same assistant message; dropped to keep the request valid (Anthropic rejects an "
                "unpaired tool_result with HTTP 400). Unpaired: %s. Observed call ids: %s",
                sorted(dropped),
                sorted(paired_ids),
            )

        out: list[Message] = []
        if assistant_contents:
            out.append(Message(role="assistant", contents=assistant_contents))
        # NEVER emit the user message alone: its tool_results would have no preceding
        # tool_use by construction, which is the 400 this module exists to prevent.
        if kept_user and assistant_contents:
            out.append(Message(role="user", contents=kept_user))
        elif kept_user:
            logger.warning(
                "Outer-loop iteration had tool/approval content but no assistant content to pair "
                "it with; the user message was NOT emitted (%d content item(s) dropped).",
                len(kept_user),
            )
        return out


__all__ = [
    "IterationContentAccumulator",
    "maf_resumable_call_ids",
    "settle_pending_tool_content",
]
