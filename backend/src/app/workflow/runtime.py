"""Declarative workflow run orchestration -- both lanes (CTR-0181, PRP-0118, UDR-0101).

Interactive lane: ``stream_workflow`` compiles the selected workflow (CTR-0180) and
runs it, translating MAF ``WorkflowEvent``s into AG-UI SSE events. The AG-UI endpoint
(CTR-0009) branches here on ``state.workflow_id``.

PRP-0123 / UDR-0106: the lane now speaks the STANDARD AG-UI vocabulary --
``STEP_STARTED`` / ``STEP_FINISHED`` per node plus one ``ACTIVITY_SNAPSHOT`` carrying
``{node, label, status, iteration, data|details}`` -- the same events the Microsoft
Agent Framework's own workflow AG-UI adapter emits. The legacy ChatWalaʻau-proprietary
``workflow_*`` CUSTOM events are emitted ALONGSIDE them for one minor line so a cached
browser bundle and saved sessions keep working (UDR-0106 D4). Only this branch changes;
the Prompt-agent path of CTR-0009 is untouched (UDR-0106 D1).

Human-in-the-loop is an AG-UI interrupt, not a held connection (UDR-0106 D5): a
``request_info`` ends the turn with ``RUN_FINISHED(outcome=interrupt)`` and the compiled
workflow is retained against the thread so the next request can resume it with
``workflow.run(responses=..., stream=True)``.

Asynchronous lane: ``run_workflow_job`` is a ``workflow`` Pipeline job type
(FEAT-0021 registry, the UDR-0074 D7 extension point) that runs the same compiled
graph to completion with run history / log (CTR-0145) and cooperative cancel.
"""

from __future__ import annotations

from collections import OrderedDict
import json
import logging
from typing import TYPE_CHECKING, Any

from app.workflow.loader import compile_for_run, node_labels_for
from app.workflow.spec import WorkflowError

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncGenerator

    from app.pipeline.models import Job
    from app.pipeline.store import PipelineStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Standard AG-UI activity vocabulary (UDR-0106 D2 / D7)
# ---------------------------------------------------------------------------
ACTIVITY_WORKFLOW_NODE = "workflow_node"
ACTIVITY_WORKFLOW_STATE = "workflow_state"

# Node statuses (UDR-0106 D3). ``skipped`` is NOT ``completed``: a conditional branch
# that was not taken must not be counted as a step or drawn as a success.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_AWAITING_INPUT = "awaiting_input"

# The per-node activity payload budget (UDR-0106 D9). A fixed constant rather than a
# configuration key: it protects the SSE transport (an agent node's whole answer, or an
# HTTP action's whole response body, would otherwise travel twice) rather than
# expressing an operator preference.
PAYLOAD_MAX_CHARS = 4000

# Compiled workflows retained while a human-in-the-loop request is pending (UDR-0106 D6).
# Bounded: a run that is never answered must not pin memory forever. Keyed by thread id
# because a conversation resumes its own workflow and nothing else.
_PENDING_RUNS: OrderedDict[str, dict[str, Any]] = OrderedDict()
_PENDING_RUNS_MAX = 32


def _retain_pending_run(thread_id: str, workflow_id: str, workflow: Any, interrupts: list[dict[str, Any]]) -> None:
    """Retain a paused workflow against its thread so the next request can resume it."""
    _PENDING_RUNS.pop(thread_id, None)
    _PENDING_RUNS[thread_id] = {"workflow_id": workflow_id, "workflow": workflow, "interrupts": interrupts}
    while len(_PENDING_RUNS) > _PENDING_RUNS_MAX:
        evicted, _ = _PENDING_RUNS.popitem(last=False)
        logger.info("Evicted a paused workflow run for thread %s (retention cap reached)", evicted)


def _take_pending_run(thread_id: str, workflow_id: str) -> Any | None:
    """Pop the retained workflow for ``thread_id`` when it matches ``workflow_id``."""
    entry = _PENDING_RUNS.get(thread_id)
    if not entry or entry.get("workflow_id") != workflow_id:
        return None
    _PENDING_RUNS.pop(thread_id, None)
    return entry.get("workflow")


def _request_prompt(event: Any) -> str | None:
    """Best-effort prompt text from a HITL ``request_info`` event (Question / external input)."""
    data = getattr(event, "data", None)
    for attr in ("message", "prompt", "text", "question"):
        value = getattr(data, attr, None)
        if isinstance(value, str) and value.strip():
            return value
        inner = getattr(value, "text", None)
        if isinstance(inner, str) and inner.strip():
            return inner
    if isinstance(data, str) and data.strip():
        return data
    return None


# A MAF failure carries ``WorkflowErrorDetails(error_type=..., message=<python traceback>,
# executor_id=..., extra=...)``. Rendering that repr puts a raw traceback in the chat, so
# the message is extracted and -- for the failure modes with a known remedy -- replaced by
# an actionable sentence.
_POWERFX_UNAVAILABLE = "PowerFx is not available"


def _failure_message(details: Any) -> str:
    """Return a clean, operator-facing message for a workflow failure.

    Prefers the structured ``message`` field over the dataclass repr, keeps only its last
    line (the exception text, not the traceback that precedes it), and rewrites the
    missing-Power-Fx case into a remedy the operator can act on.
    """
    if details is None:
        return "Workflow failed."
    raw = getattr(details, "message", None)
    text = str(raw if isinstance(raw, str) and raw.strip() else details).strip()
    if _POWERFX_UNAVAILABLE in text:
        return (
            "This workflow uses a Power Fx expression ('=...') but the Power Fx engine is "
            "unavailable on this deployment. Install the .NET runtime (the container image "
            "ships it) or replace the expressions with literal values."
        )
    # Keep the exception line, drop any preceding traceback frames.
    last = [line for line in text.splitlines() if line.strip()]
    return last[-1].strip() if last else "Workflow failed."


def _event_text(event: Any) -> str | None:
    """Best-effort text payload from a workflow OUTPUT / DATA event."""
    data = getattr(event, "data", None)
    if data is None:
        return None
    text = getattr(data, "text", None)
    if isinstance(text, str) and text:
        return text
    if isinstance(data, str) and data.strip():
        return data
    return None


def _json_safe(value: Any) -> Any:
    """Coerce an arbitrary payload into something JSON-serializable (never raises)."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    for attr in ("model_dump", "dict"):
        dumper = getattr(value, attr, None)
        if callable(dumper):
            try:
                return _json_safe(dumper())
            except Exception:
                break
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict) and data:
        return {str(k): _json_safe(v) for k, v in data.items() if not str(k).startswith("_")}
    try:
        return str(value)
    except Exception:
        return f"<{type(value).__name__}>"


def bounded_payload(value: Any, limit: int = PAYLOAD_MAX_CHARS) -> tuple[Any, bool]:
    """Return ``(payload, truncated)`` with the payload clipped to ``limit`` chars.

    The per-action log (UDR-0106 D9) must be bounded: an agent node's full answer or an
    HTTP action's full response body would otherwise be carried on the SSE channel twice.
    A clipped payload is replaced by its string prefix plus an explicit marker so the
    operator can see that something was dropped rather than reading a silent lie.
    """
    safe = _json_safe(value)
    if safe is None:
        return None, False
    try:
        rendered = json.dumps(safe, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(safe)
    if len(rendered) <= limit:
        return safe, False
    return {"truncated_preview": rendered[:limit], "original_length": len(rendered)}, True


def _hitl_payload(event: Any, node: str | None, label: str | None) -> dict[str, Any]:
    """Normalize a ``request_info`` event into the HITL descriptor the SPA renders.

    MAF's Question / RequestExternalInput executors carry an ``ExternalInputRequest``
    whose ``metadata`` holds the target variable, the choices, the free-text policy, and
    the default (``_executors_external_input.py``). Everything is best-effort so an
    unfamiliar request shape still produces a usable prompt.
    """
    data = getattr(event, "data", None)
    metadata = getattr(data, "metadata", None)
    meta: dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}
    request_id = getattr(event, "request_id", None) or getattr(data, "request_id", None)
    return {
        "request_id": str(request_id) if request_id else None,
        "node": node,
        "label": label,
        "prompt": _request_prompt(event),
        "request_type": getattr(data, "request_type", None),
        "variable": meta.get("output_property"),
        "choices": _json_safe(meta.get("choices")),
        "allow_free_text": bool(meta.get("allow_free_text", True)),
        "default": _json_safe(meta.get("default_value", meta.get("default"))),
        "required_fields": _json_safe(meta.get("required_fields")),
    }


# ---------------------------------------------------------------------------
# Interactive lane (AG-UI SSE) -- CTR-0009 branch
# ---------------------------------------------------------------------------
async def stream_workflow(
    workflow_id: str,
    message: str,
    encoder: Any,
    *,
    thread_id: str | None = None,
    result: dict[str, Any] | None = None,
    resume: dict[str, Any] | None = None,
) -> AsyncGenerator[str, None]:
    """Run ``workflow_id`` and yield encoded AG-UI SSE event strings (UDR-0106).

    Emits the STANDARD AG-UI vocabulary -- RUN_STARTED -> per-node STEP_STARTED /
    ACTIVITY_SNAPSHOT / STEP_FINISHED + TEXT_MESSAGE_* for agent-node output ->
    RUN_FINISHED -- plus the legacy ``workflow_*`` CUSTOM events for one transition line
    (UDR-0106 D4). A node that MAF bypassed is reported as ``skipped`` and is NOT counted
    as a completed step (UDR-0106 D3). A human-in-the-loop request ends the turn with
    RUN_FINISHED carrying an interrupt outcome and retains the compiled workflow so the
    next request can resume it (UDR-0106 D5/D6).

    ``resume`` maps a pending request id to the operator's answer; when present the run
    continues an existing workflow instead of compiling a new one.

    The caller passes a ``result`` dict that is filled with
    ``{"assistant_text": str, "steps": int, "produced_text": bool, "interrupted": bool}``
    so the endpoint can drive Auto Session Title (generate from output, else clear the
    pending spinner) -- otherwise a first-turn workflow run would spin forever.
    """
    from ag_ui.core import (
        ActivitySnapshotEvent,
        CustomEvent,
        EventType,
        Interrupt,
        RunErrorEvent,
        RunFinishedEvent,
        RunFinishedInterruptOutcome,
        RunFinishedSuccessOutcome,
        RunStartedEvent,
        StepFinishedEvent,
        StepStartedEvent,
        TextMessageContentEvent,
        TextMessageEndEvent,
        TextMessageStartEvent,
    )

    from app.agui.endpoint import _generate_id  # id helper (module-local)

    run_id = _generate_id()
    thread = thread_id or _generate_id()
    yield encoder.encode(RunStartedEvent(type=EventType.RUN_STARTED, thread_id=thread, run_id=run_id))

    workflow: Any = None
    if resume:
        workflow = _take_pending_run(thread, workflow_id)
        if workflow is None:
            # UDR-0106 D6: never silently start a fresh run -- the paused run's Local.
            # state is gone, so the answer would be applied to the wrong context.
            yield encoder.encode(
                RunErrorEvent(
                    type=EventType.RUN_ERROR,
                    message=(
                        "This workflow was waiting for your input, but its paused run is no longer "
                        "available on the server (it may have restarted). Send the workflow again to "
                        "start a new run."
                    ),
                )
            )
            return
    else:
        _PENDING_RUNS.pop(thread, None)
        try:
            workflow = compile_for_run(workflow_id)
        except WorkflowError as exc:
            yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, message=str(exc)))
            return

    yield encoder.encode(
        CustomEvent(type=EventType.CUSTOM, name="workflow_started", value={"workflow_id": workflow_id})
    )

    # Action id -> displayName, so a progress node reads in the author's own words
    # instead of the generated executor id (PRP-0122, UDR-0105 D7).
    node_labels = node_labels_for(workflow_id)

    def _label(node: Any) -> str:
        return node_labels.get(str(node)) or str(node)

    def _activity(node: Any, status: str, **extra: Any) -> Any:
        """One ACTIVITY_SNAPSHOT per node; message_id is stable so it REPLACES."""
        content: dict[str, Any] = {"node": node, "label": _label(node), "status": status}
        content.update({k: v for k, v in extra.items() if v is not None})
        return ActivitySnapshotEvent(
            type=EventType.ACTIVITY_SNAPSHOT,
            message_id=f"{ACTIVITY_WORKFLOW_NODE}:{node}",
            activity_type=ACTIVITY_WORKFLOW_NODE,
            content=content,
        )

    from app.workflow.inspector import snapshot_workflow_state

    msg_id: str | None = None
    last_text_node: str | None = None
    nodes_completed = 0
    assistant_parts: list[str] = []
    interrupts: list[dict[str, Any]] = []
    awaiting_nodes: set[str] = set()
    try:
        stream = workflow.run(responses=resume, stream=True) if resume else workflow.run(message, stream=True)
        async for event in stream:
            etype = str(getattr(event, "type", ""))
            executor_id = getattr(event, "executor_id", None)

            if etype in ("executor_invoked", "executor_started"):
                iteration = getattr(event, "iteration", None)
                yield encoder.encode(StepStartedEvent(type=EventType.STEP_STARTED, step_name=str(executor_id)))
                yield encoder.encode(_activity(executor_id, STATUS_RUNNING, iteration=iteration))
                yield encoder.encode(
                    CustomEvent(
                        type=EventType.CUSTOM,
                        name="workflow_node_started",
                        value={"node": executor_id, "label": _label(executor_id), "iteration": iteration},
                    )
                )
                continue

            if etype in ("executor_completed", "executor_bypassed"):
                # A Question / RequestExternalInput executor returns as soon as it has
                # RAISED its request, so MAF reports it "completed" while the workflow is
                # in fact paused on it. Keep the awaiting_input state (the snapshot would
                # otherwise REPLACE it) and do not count a paused node as a step; the real
                # completion arrives on the resume turn.
                if str(executor_id) in awaiting_nodes:
                    continue
                # UDR-0106 D3: a bypassed node was NOT executed. It gets its own status
                # and is excluded from the completed-step count (this corrects the count
                # for every workflow containing an untaken branch).
                skipped = etype == "executor_bypassed"
                if not skipped:
                    nodes_completed += 1
                payload, truncated = bounded_payload(getattr(event, "data", None))
                yield encoder.encode(StepFinishedEvent(type=EventType.STEP_FINISHED, step_name=str(executor_id)))
                yield encoder.encode(
                    _activity(
                        executor_id,
                        STATUS_SKIPPED if skipped else STATUS_COMPLETED,
                        data=payload,
                        truncated=truncated or None,
                    )
                )
                yield encoder.encode(
                    CustomEvent(
                        type=EventType.CUSTOM,
                        name="workflow_node_completed",
                        value={"node": executor_id, "label": _label(executor_id), "skipped": skipped},
                    )
                )
                # fall through so any text payload is still surfaced

            if etype == "request_info":
                # Human-in-the-Loop (Question / RequestExternalInput, UDR-0104 D4). The
                # turn does NOT park on the SSE connection: the request is collected and
                # surfaced as an AG-UI interrupt when the stream ends (UDR-0106 D5).
                node = executor_id or getattr(event, "source_executor_id", None)
                payload = _hitl_payload(event, node, _label(node) if node else None)
                if payload.get("request_id"):
                    interrupts.append(payload)
                if node is not None:
                    awaiting_nodes.add(str(node))
                yield encoder.encode(_activity(node, STATUS_AWAITING_INPUT, prompt=payload.get("prompt")))
                yield encoder.encode(CustomEvent(type=EventType.CUSTOM, name="workflow_input_request", value=payload))
                continue

            if etype == "executor_failed":
                failure = _failure_message(getattr(event, "details", None))
                details, truncated = bounded_payload(getattr(event, "details", None))
                yield encoder.encode(StepFinishedEvent(type=EventType.STEP_FINISHED, step_name=str(executor_id)))
                yield encoder.encode(
                    _activity(
                        executor_id,
                        STATUS_FAILED,
                        message=failure,
                        details=details,
                        truncated=truncated or None,
                    )
                )
                yield encoder.encode(
                    CustomEvent(
                        type=EventType.CUSTOM,
                        name="workflow_node_failed",
                        value={"node": executor_id, "label": _label(executor_id), "message": failure},
                    )
                )
                continue

            if etype == "superstep_completed":
                # Variable-namespace snapshot (CTR-0189, UDR-0106 D7). MAF commits state
                # at the superstep boundary, so this is the only point at which a reading
                # is guaranteed to reflect committed values. Off by default; returns None.
                snapshot = snapshot_workflow_state(workflow, getattr(event, "iteration", None))
                if snapshot is not None:
                    yield encoder.encode(
                        ActivitySnapshotEvent(
                            type=EventType.ACTIVITY_SNAPSHOT,
                            message_id=f"{ACTIVITY_WORKFLOW_STATE}:{snapshot.get('iteration')}",
                            activity_type=ACTIVITY_WORKFLOW_STATE,
                            content=snapshot,
                        )
                    )
                continue

            if etype in ("failed", "error"):
                failure = _failure_message(getattr(event, "details", None))
                yield encoder.encode(
                    CustomEvent(
                        type=EventType.CUSTOM,
                        name="workflow_failed",
                        value={"workflow_id": workflow_id, "steps": nodes_completed, "message": failure},
                    )
                )
                yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, message=failure))
                return

            text = _event_text(event)
            if text:
                if msg_id is None:
                    msg_id = _generate_id()
                    yield encoder.encode(
                        TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id=msg_id, role="assistant")
                    )
                # Separate each node's output with a blank line so the steps are
                # visually distinct in one message (UDR-0101 D8). The first output
                # gets no leading separator; a later node's output is prefixed with a
                # markdown paragraph break.
                elif executor_id is not None and executor_id != last_text_node:
                    yield encoder.encode(
                        TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id=msg_id, delta="\n\n")
                    )
                last_text_node = executor_id
                assistant_parts.append(text)
                yield encoder.encode(
                    TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id=msg_id, delta=text)
                )

        # Only close a message if node output actually produced one. A workflow with no
        # SendActivity / agent reply produces NO body bubble (v0.115.1); its completion
        # is shown by the run indicator (CTR-0185), not a chat message.
        if msg_id is not None:
            yield encoder.encode(TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=msg_id))
        if result is not None:
            result["assistant_text"] = "".join(assistant_parts).strip()
            result["steps"] = nodes_completed
            result["produced_text"] = msg_id is not None
            result["interrupted"] = bool(interrupts)

        if interrupts:
            # UDR-0106 D5: the run pauses as an AG-UI interrupt. Retain the compiled
            # workflow so the next request can resume it with the collected answers.
            _retain_pending_run(thread, workflow_id, workflow, interrupts)
            yield encoder.encode(
                CustomEvent(
                    type=EventType.CUSTOM,
                    name="workflow_input_pending",
                    value={"workflow_id": workflow_id, "steps": nodes_completed, "requests": interrupts},
                )
            )
            yield encoder.encode(
                RunFinishedEvent(
                    type=EventType.RUN_FINISHED,
                    thread_id=thread,
                    run_id=run_id,
                    outcome=RunFinishedInterruptOutcome(
                        interrupts=[
                            Interrupt(
                                id=str(req["request_id"]),
                                reason="workflow_input_request",
                                message=req.get("prompt"),
                                metadata=req,
                            )
                            for req in interrupts
                        ]
                    ),
                )
            )
            return

        yield encoder.encode(
            CustomEvent(
                type=EventType.CUSTOM,
                name="workflow_completed",
                value={"workflow_id": workflow_id, "steps": nodes_completed},
            )
        )
        yield encoder.encode(
            RunFinishedEvent(
                type=EventType.RUN_FINISHED,
                thread_id=thread,
                run_id=run_id,
                outcome=RunFinishedSuccessOutcome(),
            )
        )
    except Exception as exc:  # a node-agent / runtime failure ends the run
        logger.exception("Workflow run failed: %s", workflow_id)
        failure = _failure_message(exc)
        yield encoder.encode(
            CustomEvent(
                type=EventType.CUSTOM,
                name="workflow_failed",
                value={"workflow_id": workflow_id, "steps": nodes_completed, "message": failure},
            )
        )
        yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, message=failure))


# ---------------------------------------------------------------------------
# Asynchronous lane (Pipeline job) -- FEAT-0021 / UDR-0074 D7
# ---------------------------------------------------------------------------
async def run_workflow_job(job: Job, storage: PipelineStore, cancel_event: asyncio.Event) -> None:
    """Run a compiled workflow to completion as a Pipeline job (UDR-0101 D5).

    params: ``{workflow_id: str, input: str}``. Sets ``job.result`` to the collected
    output text and node count; a compile / run failure sets ``job.status = failed``
    with the error (never raises out of the runner). Cooperative cancel is checked
    around the run (fine-grained mid-graph cancel requires checkpointing, deferred).
    """
    from datetime import UTC, datetime

    from app.pipeline.models import JobStatus

    workflow_id = str(job.params.get("workflow_id", "")).strip()
    message = str(job.params.get("input", "")).strip()
    if not workflow_id:
        job.status = JobStatus.failed
        job.error = "workflow_id is required"
        job.completed_at = datetime.now(UTC).isoformat()
        storage.save(job)
        return

    job.progress = 5
    job.progress_message = f"Compiling workflow {workflow_id}..."
    storage.save(job)
    try:
        workflow = compile_for_run(workflow_id)
    except WorkflowError as exc:
        job.status = JobStatus.failed
        job.error = str(exc)
        job.completed_at = datetime.now(UTC).isoformat()
        storage.save(job)
        return

    if cancel_event.is_set():
        job.status = JobStatus.cancelled
        job.completed_at = datetime.now(UTC).isoformat()
        storage.save(job)
        return

    job.progress = 20
    job.progress_message = "Running workflow..."
    storage.save(job)

    outputs: list[str] = []
    nodes = 0
    skipped = 0
    try:
        async for event in workflow.run(message, stream=True):
            etype = str(getattr(event, "type", ""))
            if etype == "executor_completed":
                nodes += 1
            elif etype == "executor_bypassed":
                # UDR-0106 D3: reported separately, never as a completed step.
                skipped += 1
            if etype in ("failed", "error"):
                raise WorkflowError(_failure_message(getattr(event, "details", None)))
            text = _event_text(event)
            if text:
                outputs.append(text)
            if cancel_event.is_set():
                job.status = JobStatus.cancelled
                job.completed_at = datetime.now(UTC).isoformat()
                storage.save(job)
                return
    except Exception as exc:
        job.status = JobStatus.failed
        job.error = _failure_message(exc)
        job.completed_at = datetime.now(UTC).isoformat()
        storage.save(job)
        return

    job.status = JobStatus.completed
    job.progress = 100
    job.progress_message = "Workflow completed"
    job.result = {
        "workflow_id": workflow_id,
        "nodes_completed": nodes,
        "nodes_skipped": skipped,
        "output": "".join(outputs),
    }
    job.completed_at = datetime.now(UTC).isoformat()
    storage.save(job)


def register_workflow_job_type() -> None:
    """Register the ``workflow`` Pipeline job type (idempotent; UDR-0074 D7)."""
    from app.pipeline.registry import JobType, ParamSpec, register_job_type

    register_job_type(
        JobType(
            name="workflow",
            label="Declarative Workflow",
            description=(
                "Run a declarative workflow (kind: Workflow) to completion as a background "
                "job, with run history and a captured log. The workflow orchestrates "
                "declarative Prompt agents."
            ),
            runner=run_workflow_job,
            params=[
                ParamSpec(
                    name="workflow_id",
                    label="Workflow",
                    type="string",
                    required=True,
                    help="The declarative workflow id (from the Workflows inventory).",
                ),
                ParamSpec(
                    name="input",
                    label="Input",
                    type="string",
                    help="The initial input / instruction passed to the workflow's start node.",
                ),
            ],
        )
    )


__all__ = [
    "ACTIVITY_WORKFLOW_NODE",
    "ACTIVITY_WORKFLOW_STATE",
    "PAYLOAD_MAX_CHARS",
    "STATUS_AWAITING_INPUT",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_RUNNING",
    "STATUS_SKIPPED",
    "bounded_payload",
    "register_workflow_job_type",
    "run_workflow_job",
    "stream_workflow",
]
