"""Streamed Content Merge Fold (CTR-0266, PRP-0229, UDR-0212 D3).

MAF 1.22.0 (#8886) gives every streamed ``Content`` an optional ``merge_mode``:

* ``append`` (or no mode) -- an incremental delta, the pre-1.22 meaning of every content;
* ``replace`` -- the FULL value of the same occurrence, sent after its deltas.

Producers measured in PRP-0229 (M8, M9): the OpenAI connector re-sends every finished
output text, refusal, reasoning text / summary and function-call argument string as a
``replace``; core rewrites the usage of every provider call after the first one of a
function-invocation loop into the loop's CUMULATIVE total with ``replace``. Upstream's
own fold (``ChatResponse.from_updates``) honours both. ChatWalaʻau's stream consumers
(CTR-0009 AG-UI, CTR-0057 OpenAI-compatible API, CTR-0140 Teams, CTR-0226 Live) emit
INCREMENTAL output instead, so they fold here -- and nowhere else (D3: no consumer
interprets ``merge_mode`` itself).

Everything in this module is a pure function of the update sequence: no I/O, no
provider knowledge, no MAF private API.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from agent_framework import Content, add_usage_details

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

logger = logging.getLogger(__name__)

REPLACE = "replace"


def merge_mode(content: Any) -> str | None:
    """The content's merge mode; ``None`` for a pre-1.22 content or a plain object."""
    mode = getattr(content, "merge_mode", None)
    return mode if isinstance(mode, str) else None


def arguments_text(arguments: Any) -> str:
    """A function call's arguments as the string the wire carries (dicts JSON-encoded)."""
    if arguments is None:
        return ""
    if isinstance(arguments, str):
        return arguments
    try:
        return json.dumps(arguments, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(arguments)


class TextFold:
    """One logical run of streamed text, emitted APPEND-ONLY (D3.1, D3.2).

    ``feed`` returns the delta to emit. A ``replace`` equal to what was shown emits
    nothing; one that extends it emits only the missing suffix; anything else emits
    nothing, keeps the shown text (an append-only stream cannot retract) and logs one
    WARNING -- the operator decision PRP-0229 Q3.
    """

    def __init__(self, label: str = "text") -> None:
        self.label = label
        self.shown = ""
        self.diverged = 0

    def reset(self) -> None:
        self.shown = ""

    def feed(self, text: str | None, mode: str | None) -> str:
        if not text:
            return ""
        if mode != REPLACE:
            self.shown += text
            return text
        if text == self.shown:
            return ""
        if text.startswith(self.shown):
            delta = text[len(self.shown) :]
            self.shown = text
            return delta
        self.diverged += 1
        logger.warning(
            "[stream merge] a %s replace does not extend the streamed %s (shown %d chars, "
            "replacement %d chars); the shown text is kept (UDR-0212 D3.1)",
            self.label,
            self.label,
            len(self.shown),
            len(text),
        )
        return ""


class ArgumentFold:
    """Function-call arguments per call id, under the TextFold rule (D3.2)."""

    def __init__(self) -> None:
        self._runs: dict[str, TextFold] = {}

    def feed(self, call_id: str, arguments: Any, mode: str | None) -> str:
        run = self._runs.setdefault(call_id, TextFold("function-call arguments"))
        return run.feed(arguments_text(arguments), mode)

    def shown(self, call_id: str) -> str:
        run = self._runs.get(call_id)
        return run.shown if run is not None else ""


def _int_keys_minus(total: Mapping[str, Any] | None, start: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``total - start`` on integer keys (never below zero); other keys keep ``total``."""
    if not total:
        return None
    out: dict[str, Any] = {}
    for key, value in total.items():
        before = (start or {}).get(key)
        if isinstance(value, int) and not isinstance(value, bool) and isinstance(before, int):
            out[key] = max(value - before, 0)
        else:
            out[key] = value
    return out


class UsageFold:
    """Usage of one turn: billing axis, context axis and call count (D3.3-D3.5).

    * Within a RUN SEGMENT (one function-invocation loop) a ``replace`` SETS the segment
      total and an ``append`` / no mode ADDS to it -- upstream ``from_updates`` semantics.
    * ``new_segment`` closes a segment: the harness loop's injected nudge starts a new
      function-invocation loop whose cumulative restarts, and so does a retry.
    * ``call_boundary`` marks the end of a model call (a ``function_result`` after usage).
    * ``turn`` is the sum of segment totals; ``last_call`` the segment total minus the
      total at the last boundary; ``calls`` the model calls that reported usage.
    """

    def __init__(self) -> None:
        self._closed: dict[str, Any] | None = None
        self._segment: dict[str, Any] | None = None
        self._call_start: dict[str, Any] | None = None
        self._in_call = False
        self.calls = 0

    def observe(self, details: Mapping[str, Any] | None, mode: str | None) -> None:
        if not details:
            return
        if not self._in_call:
            self._in_call = True
            self.calls += 1
        if mode == REPLACE:
            self._segment = dict(details)
        else:
            self._segment = dict(add_usage_details(self._segment, dict(details)))  # type: ignore[arg-type]

    def call_boundary(self) -> None:
        if self._in_call:
            self._in_call = False
            self._call_start = dict(self._segment) if self._segment else None

    def new_segment(self) -> None:
        if self._segment:
            self._closed = dict(add_usage_details(self._closed, self._segment))  # type: ignore[arg-type]
        self._segment = None
        self._call_start = None
        self._in_call = False

    @property
    def turn(self) -> dict[str, Any] | None:
        if not self._segment:
            return dict(self._closed) if self._closed else None
        return dict(add_usage_details(self._closed, self._segment))  # type: ignore[arg-type]

    @property
    def last_call(self) -> dict[str, Any] | None:
        return _int_keys_minus(self._segment, self._call_start)


class FoldResult:
    """What a NON-streaming collector needs from a whole content list (D3.6)."""

    def __init__(self, contents: list[Any], usage: UsageFold) -> None:
        self.contents = contents
        self.usage = usage

    @property
    def text(self) -> str:
        return "".join(c.text for c in self.contents if getattr(c, "type", None) == "text" and c.text)


def fold_contents(contents: Iterable[Any]) -> FoldResult:
    """Fold a streamed content list into deltas-only contents plus the turn's usage.

    Text and reasoning become their append-only deltas (a ``replace`` contributes only a
    missing suffix); function-call fragments coalesce into ONE content per call id at
    the position of the first fragment, carrying the full arguments; usage contents are
    consumed into the returned ``UsageFold``; every other content passes through.
    """
    out: list[Any] = []
    usage = UsageFold()
    text = TextFold("text")
    reasoning = TextFold("reasoning")
    args = ArgumentFold()
    calls: dict[str, int] = {}
    names: dict[str, str] = {}

    for content in contents:
        kind = getattr(content, "type", None)
        mode = merge_mode(content)
        if kind == "text":
            reasoning.reset()
            delta = text.feed(getattr(content, "text", None), mode)
            if delta:
                out.append(Content.from_text(text=delta))
            elif getattr(content, "annotations", None) and not getattr(content, "text", None):
                out.append(content)
        elif kind == "text_reasoning":
            delta = reasoning.feed(getattr(content, "text", None), mode)
            if delta:
                out.append(Content.from_text_reasoning(text=delta))
        elif kind == "function_call":
            text.reset()
            reasoning.reset()
            call_id = getattr(content, "call_id", None) or ""
            if getattr(content, "name", None):
                names.setdefault(call_id, content.name)
            args.feed(call_id, getattr(content, "arguments", None), mode)
            if call_id not in calls:
                calls[call_id] = len(out)
                out.append(None)  # placeholder, filled below
        elif kind == "usage":
            usage.observe(getattr(content, "usage_details", None), mode)
        else:
            if kind == "function_result":
                usage.call_boundary()
            text.reset()
            reasoning.reset()
            out.append(content)

    for call_id, index in calls.items():
        out[index] = Content.from_function_call(
            call_id=call_id,
            name=names.get(call_id, ""),
            arguments=args.shown(call_id),
        )
    return FoldResult([c for c in out if c is not None], usage)
