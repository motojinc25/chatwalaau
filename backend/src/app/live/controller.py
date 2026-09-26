"""GPT-Live sideband controller (CTR-0225, PRP-0188, UDR-0170 D3 / D7).

The sideband WebSocket is the ONLY controller of a Live session. Audio flows
browser <-> service on the WebRTC media track; everything else happens here:

- transcript fragments are grouped into turns (CTR-0227) and persisted;
- client delegations are executed, one at a time, by the selected Prompt agent
  (CTR-0226) and answered with ``session.commentary.append``;
- limits are enforced (session length, silence, subscriber gone) and the session is
  closed and recorded (``live_sessions[]``);
- everything the SPA shows is pushed to its CTR-0224 event stream.

The browser's data channel receives the same server events and is deliberately
ignored by the SPA, so no work is ever executed twice and the screen shows exactly
what is stored.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import json
import logging
import time
from typing import Any

from app.live import persist
from app.live.transcript import GroupingResult, TranscriptGrouper, live_meta, message_id

logger = logging.getLogger(__name__)

#: The ticker period: turn boundaries, provisional captions and limits.
TICK_SEC = 0.25
#: A session whose event-stream subscriber has been gone this long is closed.
SUBSCRIBER_GRACE_SEC = 15.0
#: How long ``close`` waits for ``session.closed`` before dropping the sideband.
CLOSE_WAIT_SEC = 10.0
#: The time warning is sent this long before the session limit.
TIME_WARNING_SEC = 60
#: ``context_window.usage_ratio`` at which a warning is pushed (CTR-0221's 80%).
CONTEXT_WARNING_RATIO = 0.8
#: Events kept for a subscriber that has not connected yet.
EARLY_EVENT_CAP = 500

FAILED_TIMEOUT_SPOKEN = "I could not finish that in time."
FAILED_ERROR_SPOKEN = "Something went wrong while working on that."

#: Step 3 (UDR-0170 D14): what a cancelled delegation leaves in the chat, and what
#: GPT-Live is told so it stops waiting for the result.
CANCELLED_TEXT = "(Cancelled by the user.)"
CANCELLED_CONTEXT = "The user cancelled this delegated task. Do not wait for its result."
#: A delegation still waiting in the queue when the session ended: it never ran.
NOT_RUN_TEXT = "(The Live conversation ended before this task started.)"
#: Step 3: progress of a running delegation is pushed at most this often.
PROGRESS_INTERVAL_SEC = 0.3
#: Only the tail of the streamed text travels in a progress event.
PROGRESS_TEXT_CAP = 4000
#: Step 3 (UDR-0170 D13): typed text reaches GPT-Live as quiet context, capped under
#: the service's 500-token append limit (S7). The full text is in the chat.
TYPED_TEXT_TOKEN_CAP = 400
TYPED_TEXT_PREFIX = "The user typed this in the chat instead of speaking. Reply to it: "


@dataclass(frozen=True)
class LiveLimits:
    max_session_seconds: int
    idle_timeout_seconds: int
    delegation_timeout_seconds: int

    def public(self) -> dict[str, int]:
        return {
            "max_session_seconds": self.max_session_seconds,
            "idle_timeout_seconds": self.idle_timeout_seconds,
        }


class LiveSession:
    """One running GPT-Live session and its sideband."""

    def __init__(
        self,
        *,
        live_session_id: str,
        thread_id: str,
        temporary: bool,
        agent_registry: Any,
        agent: dict[str, str],
        limits: LiveLimits,
        ws: Any,
        on_finished: Any = None,
    ) -> None:
        self.live_session_id = live_session_id
        self.thread_id = thread_id
        self.temporary = temporary
        self.agent_registry = agent_registry
        self.agent = agent
        self.limits = limits
        self._ws = ws
        self._on_finished = on_finished
        self.grouper = TranscriptGrouper(live_session_id)

        self._t0 = time.monotonic()
        self._last_activity = self._t0
        self.state = "live"
        self.muted = False
        self._close_reason: str | None = None
        self._service_reason: str | None = None
        self._seconds: int | None = None
        self._closed_event = asyncio.Event()
        self._finished = asyncio.Event()
        self._time_warned = False
        self._context_warned = False

        self._delegation_queue: asyncio.Queue[str] = asyncio.Queue()
        self._delegation_busy = False
        self._delegation_seq = 0
        self.delegations = 0
        # Step 3: queued + running delegation id -> its chat message id; the running
        # run's task (for cancel); ids the user cancelled.
        self._delegation_ids: dict[str, str] = {}
        self._running: tuple[str, asyncio.Task[Any]] | None = None
        self._cancel_requested: set[str] = set()
        self._typed_seq = 0

        # CTR-0224 subscriber (one at a time; a new one replaces the old).
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] | None = None
        self._subscriber_token = 0
        self._subscriber_left_at: float | None = self._t0
        self._early: list[tuple[str, dict[str, Any]]] = []

        self._tasks: list[asyncio.Task[Any]] = []
        # Fire-and-forget tasks (close / finalize) are referenced until they finish.
        self._aux: set[asyncio.Task[Any]] = set()

    # ---- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._reader(), name=f"live-reader-{self.live_session_id}"),
            asyncio.create_task(self._ticker(), name=f"live-ticker-{self.live_session_id}"),
            asyncio.create_task(self._delegation_worker(), name=f"live-delegation-{self.live_session_id}"),
        ]
        self._emit(
            "live.ready", {"live_session_id": self.live_session_id, "limits": self.limits.public(), "agent": self.agent}
        )

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._aux.add(task)
        task.add_done_callback(self._aux.discard)

    def now_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    async def close(self, reason: str) -> None:
        """Ask the service to close, wait for ``session.closed``, then finalize."""
        if self.state in ("closing", "closed"):
            return
        self.state = "closing"
        if self._close_reason is None:
            self._close_reason = reason
        await self._send({"type": "session.close", "event_id": f"close_{self.now_ms()}"})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._closed_event.wait(), timeout=CLOSE_WAIT_SEC)
        await self._finalize()

    async def wait_finished(self) -> None:
        await self._finished.wait()

    async def _finalize(self) -> None:
        if self.state == "closed":
            return
        self.state = "closed"
        for task in self._tasks:
            if task is not asyncio.current_task() and task.get_name().startswith(("live-reader", "live-ticker")):
                task.cancel()
        # Queued delegations are dropped; a running one finishes on its own (it still
        # persists its answer, but nothing is spoken on a closed session).
        while not self._delegation_queue.empty():
            dropped = self._delegation_queue.get_nowait()
            if dropped and dropped in self._delegation_ids:
                # It never ran, and the SPA must be told so rather than left spinning
                # (defect fix, v0.169.0).
                self._finish_delegation(dropped, text=NOT_RUN_TEXT, cancelled=True, speak=False)
            self._delegation_ids.pop(dropped, None)
        # Sentinel: the worker exits once any running delegation has finished.
        self._delegation_queue.put_nowait("")
        self._commit(self.grouper.force_close())
        with contextlib.suppress(Exception):
            await self._ws.close()
        reason = self._close_reason or self._service_reason or "connection_lost"
        try:
            persist.finish_live_record(
                self.thread_id,
                self.live_session_id,
                seconds=self._seconds,
                reason=reason,
                delegations=self.delegations,
            )
        except Exception:
            logger.warning("Live: could not record the end of %s", self.live_session_id, exc_info=True)
        # Voice seconds for the usage dashboard (step 3, UDR-0170 D10), kept beside the
        # token ledger so deleting the chat never deletes them.
        from app.usage.voice import append_voice_usage

        append_voice_usage(
            thread_id=self.thread_id,
            temporary=self.temporary,
            live_session_id=self.live_session_id,
            seconds=self._seconds,
            reason=reason,
            delegations=self.delegations,
        )
        self._emit("live.closed", {"reason": reason, "seconds": self._seconds})
        logger.info(
            "Live session %s closed: reason=%s seconds=%s delegations=%d",
            self.live_session_id,
            reason,
            self._seconds,
            self.delegations,
        )
        self._finished.set()
        if self._on_finished is not None:
            self._on_finished(self)

    # ---- commands ----------------------------------------------------------

    async def set_muted(self, muted: bool) -> None:
        """Mute or unmute caller input on the service (Q5)."""
        await self._send({"type": "session.input_audio.mute" if muted else "session.input_audio.unmute"})

    async def _send(self, event: dict[str, Any]) -> bool:
        try:
            await self._ws.send(json.dumps(event, ensure_ascii=False))
            return True
        except Exception:
            logger.debug("Live: send failed for %s (%s)", self.live_session_id, event.get("type"), exc_info=True)
            return False

    # ---- event stream (CTR-0224) -------------------------------------------

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._queue is not None:
            self._queue.put_nowait((event, data))
        elif len(self._early) < EARLY_EVENT_CAP:
            self._early.append((event, data))

    def subscribe(self) -> tuple[int, asyncio.Queue[tuple[str, dict[str, Any]]]]:
        """Attach a subscriber; replaces any previous one."""
        self._subscriber_token += 1
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        if self._queue is None:
            for item in self._early:
                queue.put_nowait(item)
            self._early.clear()
        else:
            # A re-attach: tell the new subscriber where things stand.
            queue.put_nowait(
                (
                    "live.ready",
                    {"live_session_id": self.live_session_id, "limits": self.limits.public(), "agent": self.agent},
                )
            )
            if self.state == "closed":
                queue.put_nowait(
                    ("live.closed", {"reason": self._close_reason or self._service_reason, "seconds": self._seconds})
                )
        self._queue = queue
        self._subscriber_left_at = None
        return self._subscriber_token, queue

    def is_current_subscriber(self, token: int) -> bool:
        return token == self._subscriber_token

    def request_close(self, reason: str) -> None:
        """Start ``close`` in the background (the stop request does not wait for it)."""
        if self.state == "live":
            self._spawn(self.close(reason))

    def unsubscribe(self, token: int) -> None:
        if token == self._subscriber_token:
            self._queue = None
            self._subscriber_left_at = time.monotonic()

    # ---- sideband reader ---------------------------------------------------

    async def _reader(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    event = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(event, dict):
                    await self._handle(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.info("Live: sideband for %s ended", self.live_session_id, exc_info=True)
        # The transport is gone: whatever caused it, the session is over.
        self._closed_event.set()
        if self.state != "closed":
            self._spawn(self._finalize())

    async def _handle(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "session.input_transcript.delta":
            self._fragment("user", event)
        elif etype == "session.output_transcript.delta":
            self._fragment("assistant", event)
        elif etype == "session.delegation.created":
            delegation = event.get("delegation") or {}
            if delegation.get("target") == "client" and delegation.get("id"):
                self._last_activity = time.monotonic()
                self._enqueue_delegation(str(delegation["id"]))
        elif etype == "session.usage.updated":
            self._usage(event)
        elif etype in ("session.input_audio.muted", "session.input_audio.unmuted"):
            self.muted = etype == "session.input_audio.muted"
            self._emit("live.mute", {"muted": self.muted})
        elif etype == "session.closed":
            self._usage(event)
            reason = event.get("reason")
            if isinstance(reason, str):
                self._service_reason = reason
            self._closed_event.set()
            if self.state != "closing":
                self.state = "closing"
                self._spawn(self._finalize())
        elif etype == "error":
            err = event.get("error") or {}
            logger.warning("Live %s error: %s %s", self.live_session_id, err.get("code"), err.get("message"))
            self._emit("live.error", {"code": err.get("code"), "message": err.get("message")})

    def _fragment(self, role: str, event: dict[str, Any]) -> None:
        delta = event.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        self._last_activity = time.monotonic()
        start = event.get("start_ms")
        end = event.get("end_ms")
        result = self.grouper.feed(
            role,  # type: ignore[arg-type]
            delta,
            self.now_ms(),
            start_ms=start if isinstance(start, int) else None,
            end_ms=end if isinstance(end, int) else None,
        )
        self._commit(result)

    def _usage(self, event: dict[str, Any]) -> None:
        usage = event.get("usage") or {}
        seconds = usage.get("seconds") if isinstance(usage, dict) else None
        if isinstance(seconds, (int, float)):
            self._seconds = int(seconds)
        ctx = event.get("context_window") or {}
        ratio = ctx.get("usage_ratio") if isinstance(ctx, dict) else None
        if isinstance(ratio, (int, float)) and ratio >= CONTEXT_WARNING_RATIO and not self._context_warned:
            self._context_warned = True
            self._emit("live.warning", {"kind": "context", "usage_ratio": ratio})

    # ---- ticker: turns and limits ------------------------------------------

    async def _ticker(self) -> None:
        while self.state == "live":
            await asyncio.sleep(TICK_SEC)
            if self.state != "live":
                break
            self._commit(self.grouper.tick(self.now_ms()))
            reason = self._limit_reached()
            if reason is not None:
                self._spawn(self.close(reason))
                break

    def _limit_reached(self) -> str | None:
        now = time.monotonic()
        elapsed = now - self._t0
        remaining = self.limits.max_session_seconds - elapsed
        if (
            not self._time_warned
            and self.limits.max_session_seconds > TIME_WARNING_SEC
            and remaining <= TIME_WARNING_SEC
        ):
            self._time_warned = True
            self._emit("live.warning", {"kind": "time", "remaining_seconds": max(0, int(remaining))})
        if remaining <= 0:
            return "max_duration"
        busy = self._delegation_busy or not self._delegation_queue.empty()
        if busy:
            self._last_activity = now
        elif not self.grouper.has_open_turns and now - self._last_activity >= self.limits.idle_timeout_seconds:
            return "idle_timeout"
        if self._subscriber_left_at is not None and now - self._subscriber_left_at >= SUBSCRIBER_GRACE_SEC:
            return "subscriber_gone"
        return None

    def _commit(self, result: GroupingResult) -> None:
        for update in result.provisional:
            self._emit("live.message", update)
        if not result.committed:
            return
        messages = [
            persist.build_message(
                message_id=turn.message_id,
                role=turn.role,
                text=turn.text.strip(),
                live=turn.to_update(self.live_session_id, final=True)["live"],
            )
            for turn in result.committed
        ]
        try:
            persist.append_messages(self.thread_id, messages)
        except Exception:
            logger.warning("Live: could not persist %d turn(s) for %s", len(messages), self.thread_id, exc_info=True)
        for turn in result.committed:
            self._emit("live.message", turn.to_update(self.live_session_id, final=True))

    # ---- typed text (step 3) ----------------------------------------------

    async def send_text(self, text: str) -> dict[str, Any]:
        """A message the user TYPED during Live (PRP-0188 step 3, UDR-0170 D13).

        It is committed like a spoken turn -- open turns are closed first so the chat
        keeps its order -- persisted as a user message with ``live.kind: typed``, and
        handed to GPT-Live as quiet context (``session.thinking.append``, at most
        ~400 tokens; the full text is in the chat and in every delegation's history).
        """
        from app.live.delegation import truncate_to_tokens

        clean = text.strip()
        if not clean:
            raise ValueError("empty text")
        self._commit(self.grouper.force_close())
        self._typed_seq += 1
        msg_id = message_id(self.live_session_id, "t", self._typed_seq)
        live = live_meta(self.live_session_id, kind="typed")
        try:
            persist.append_messages(
                self.thread_id, [persist.build_message(message_id=msg_id, role="user", text=clean, live=live)]
            )
        except Exception:
            logger.warning("Live: could not persist a typed message for %s", self.thread_id, exc_info=True)
        self._emit("live.message", {"message_id": msg_id, "role": "user", "text": clean, "final": True, "live": live})
        self._last_activity = time.monotonic()
        await self._send(
            {
                "type": "session.thinking.append",
                "event_id": f"typed_{msg_id}",
                "delegation_id": None,
                "content": TYPED_TEXT_PREFIX + truncate_to_tokens(clean, TYPED_TEXT_TOKEN_CAP),
            }
        )
        return {"message_id": msg_id}

    # ---- delegation (CTR-0226) ---------------------------------------------

    def _enqueue_delegation(self, delegation_id: str) -> None:
        """Queue a client delegation and give it its chat message id at once (step 3).

        The id is the one the final answer will carry, so the SPA's in-chat marker is
        replaced in place when the run ends.

        A REPEATED id is ignored (defect fix, v0.169.0). Minting a second message id for
        an id already queued or running orphaned the first marker -- it could never be
        replaced by an answer, and the second pass through the worker then raised
        KeyError and ended without a word. One delegation id is one unit of work.
        """
        if delegation_id in self._delegation_ids:
            logger.warning(
                "Live: delegation %s announced twice on %s; keeping the first",
                delegation_id,
                self.live_session_id,
            )
            return
        self._delegation_seq += 1
        msg_id = message_id(self.live_session_id, "d", self._delegation_seq)
        self._delegation_ids[delegation_id] = msg_id
        self._delegation_queue.put_nowait(delegation_id)
        self._emit("live.delegation", {"delegation_id": delegation_id, "message_id": msg_id, "state": "queued"})

    def pending_delegations(self) -> int:
        """Queued plus running delegations (the Working count, step 3)."""
        return len(self._delegation_ids)

    def cancel_delegation(self, delegation_id: str) -> bool:
        """Cancel a queued or running delegation (step 3). False when unknown / finished."""
        if delegation_id not in self._delegation_ids:
            return False
        running = self._running
        if running is not None and running[0] == delegation_id:
            self._cancel_requested.add(delegation_id)
            running[1].cancel()
        else:
            self._cancel_requested.add(delegation_id)
        self._emit(
            "live.delegation",
            {"delegation_id": delegation_id, "message_id": self._delegation_ids[delegation_id], "state": "cancelling"},
        )
        return True

    async def _delegation_worker(self) -> None:
        while True:
            delegation_id = await self._delegation_queue.get()
            if not delegation_id:
                return
            if self.state == "closed":
                self._delegation_ids.pop(delegation_id, None)
                continue
            self._delegation_busy = True
            try:
                await self._run_delegation(delegation_id)
            except Exception as exc:
                # Nothing may end silently (defect fix, v0.169.0): the marker in the chat
                # is resolved here even when the run itself could not report.
                logger.exception("Live: delegation %s crashed", delegation_id)
                self._finish_delegation(
                    delegation_id,
                    text=f"(The delegated task could not be run: {type(exc).__name__}.)",
                    failed=True,
                )
            finally:
                self._delegation_ids.pop(delegation_id, None)
                self._cancel_requested.discard(delegation_id)
                self._delegation_busy = False
                self._last_activity = time.monotonic()

    def _progress(self, delegation_id: str, msg_id: str) -> Any:
        """A throttled progress reporter for one run (step 3): accumulated text + tools."""
        last = [0.0]

        def report(text: str, tools: list[dict[str, Any]], *, force: bool = False) -> None:
            now = time.monotonic()
            if not force and now - last[0] < PROGRESS_INTERVAL_SEC:
                return
            last[0] = now
            self._emit(
                "live.progress",
                {
                    "delegation_id": delegation_id,
                    "message_id": msg_id,
                    "text": text[-PROGRESS_TEXT_CAP:],
                    "tools": [{"id": t["id"], "name": t["name"], "status": t["status"]} for t in tools],
                },
            )

        return report

    def _finish_delegation(
        self,
        delegation_id: str,
        *,
        text: str,
        failed: bool = False,
        cancelled: bool = False,
        tool_calls: list[dict[str, Any]] | None = None,
        activity: list[dict[str, Any]] | None = None,
        usage: dict[str, Any] | None = None,
        speak: bool = True,
    ) -> str:
        """Persist a delegation's outcome, push it, and emit its TERMINAL state.

        Every exit of a delegation goes through here (defect fix, v0.169.0) -- the
        answer, a timeout, an error, a cancel, a crash, and one dropped when the session
        closed -- so a marker in the chat is always replaced by something, and the
        Working count always comes back down. ``speak`` is the caller's business; this
        method never talks to GPT-Live.
        """
        msg_id = self._message_id_for(delegation_id)
        live = live_meta(
            self.live_session_id,
            kind="delegation",
            delegation_id=delegation_id,
            failed=failed,
            cancelled=cancelled,
        )
        message = persist.build_message(
            message_id=msg_id,
            role="assistant",
            text=text,
            live=live,
            tool_calls=tool_calls or [],
            activity_log=activity or [],
            usage=usage,
        )
        try:
            persist.append_messages(self.thread_id, [message])
        except Exception:
            logger.warning("Live: could not persist delegation %s", delegation_id, exc_info=True)
        self._emit(
            "live.message",
            {
                "message_id": msg_id,
                "role": "assistant",
                "text": text,
                "final": True,
                "live": live,
                "tool_calls": tool_calls or [],
                "activity_log": activity or [],
                "usage": usage,
            },
        )
        state = "cancelled" if cancelled else "failed" if failed else "completed"
        self._emit("live.delegation", {"delegation_id": delegation_id, "message_id": msg_id, "state": state})
        return msg_id

    def _message_id_for(self, delegation_id: str) -> str:
        """The chat message id of a delegation, minting one if it is somehow unknown."""
        msg_id = self._delegation_ids.get(delegation_id)
        if msg_id is None:
            self._delegation_seq += 1
            msg_id = message_id(self.live_session_id, "d", self._delegation_seq)
            self._delegation_ids[delegation_id] = msg_id
        return msg_id

    async def _run_delegation(self, delegation_id: str) -> None:
        from app.live.delegation import record_usage, run_delegated_agent, spoken_summary

        msg_id = self._message_id_for(delegation_id)
        self.delegations += 1
        partial: list[str] = []
        failed = False
        cancelled = False
        tool_calls: list[dict[str, Any]] = []
        activity: list[dict[str, Any]] = []
        usage: dict[str, Any] | None = None
        model = getattr(self.agent_registry, "default_model", "")
        spoken = ""

        if delegation_id in self._cancel_requested:
            # Cancelled while it was still waiting in the queue: never started.
            cancelled = True
            text = CANCELLED_TEXT
        else:
            self._emit("live.delegation", {"delegation_id": delegation_id, "message_id": msg_id, "state": "started"})
            # Settle: the request is whatever was said up to now (UDR-0170 D4).
            self._commit(self.grouper.force_close(started_at_or_before=self.now_ms()))
            run = asyncio.create_task(
                asyncio.wait_for(
                    run_delegated_agent(
                        thread_id=self.thread_id,
                        agent_registry=self.agent_registry,
                        temporary=self.temporary,
                        text_sink=partial,
                        progress=self._progress(delegation_id, msg_id),
                    ),
                    timeout=self.limits.delegation_timeout_seconds,
                ),
                name=f"live-run-{delegation_id}",
            )
            self._running = (delegation_id, run)
            try:
                outcome, turn_usage, model_calls, model = await run
                text = outcome.text
                tool_calls, activity = outcome.tool_calls, outcome.activity_log
                usage = record_usage(
                    turn_usage=turn_usage,
                    model_calls=model_calls,
                    model=model,
                    thread_id=self.thread_id,
                    temporary=self.temporary,
                    outcome="completed",
                )
                spoken = await spoken_summary(text, model=model, thread_id=self.thread_id) if text else ""
                if not text:
                    failed = True
                    text = "(The agent returned no answer.)"
                    spoken = FAILED_ERROR_SPOKEN
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise  # this worker itself is being cancelled -- not a user cancel
                cancelled = True
                done = "".join(partial).strip()
                text = f"{done}\n\n{CANCELLED_TEXT}".strip() if done else CANCELLED_TEXT
            except TimeoutError:
                failed = True
                done = "".join(partial).strip()
                note = f"(The delegated task did not finish within {self.limits.delegation_timeout_seconds} seconds.)"
                text = f"{done}\n\n{note}".strip() if done else note
                spoken = FAILED_TIMEOUT_SPOKEN
            except Exception as exc:
                failed = True
                logger.warning("Live: delegation %s failed", delegation_id, exc_info=True)
                text = f"(The delegated task failed: {type(exc).__name__}.)"
                spoken = FAILED_ERROR_SPOKEN
            finally:
                self._running = None

        if usage is None and model:
            usage = {"model": model}
        self._finish_delegation(
            delegation_id,
            text=text,
            failed=failed,
            cancelled=cancelled,
            tool_calls=tool_calls,
            activity=activity,
            usage=usage,
        )
        if self.state == "live":
            if cancelled:
                # Quiet: GPT-Live must stop waiting for it, but nothing is read out.
                await self._send(
                    {
                        "type": "session.thinking.append",
                        "event_id": f"cancel_{msg_id}",
                        "delegation_id": delegation_id,
                        "content": CANCELLED_CONTEXT,
                    }
                )
            elif spoken:
                await self._send(
                    {
                        "type": "session.commentary.append",
                        "event_id": f"result_{msg_id}",
                        "delegation_id": delegation_id,
                        "content": spoken,
                    }
                )


class LiveManager:
    """The registry of running Live sessions (one per thread, D7)."""

    def __init__(self) -> None:
        self._sessions: dict[str, LiveSession] = {}
        self._threads: dict[str, str] = {}
        self._reserved: set[str] = set()

    def get(self, live_session_id: str) -> LiveSession | None:
        return self._sessions.get(live_session_id)

    def busy(self, thread_id: str) -> bool:
        return thread_id in self._threads or thread_id in self._reserved

    def reserve(self, thread_id: str) -> bool:
        if self.busy(thread_id):
            return False
        self._reserved.add(thread_id)
        return True

    def release(self, thread_id: str) -> None:
        self._reserved.discard(thread_id)

    def add(self, session: LiveSession) -> None:
        self._reserved.discard(session.thread_id)
        self._sessions[session.live_session_id] = session
        self._threads[session.thread_id] = session.live_session_id

    def _finished(self, session: LiveSession) -> None:
        self._sessions.pop(session.live_session_id, None)
        if self._threads.get(session.thread_id) == session.live_session_id:
            self._threads.pop(session.thread_id, None)

    @property
    def on_finished(self) -> Any:
        return self._finished

    def running(self) -> list[LiveSession]:
        return list(self._sessions.values())

    async def shutdown(self) -> None:
        """Close every running session (lifespan shutdown, reason ``server_shutdown``)."""
        sessions = self.running()
        if not sessions:
            return
        await asyncio.gather(*(s.close("server_shutdown") for s in sessions), return_exceptions=True)


manager = LiveManager()
