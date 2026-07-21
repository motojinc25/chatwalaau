"""Declarative workflow run orchestration -- both lanes (CTR-0181, PRP-0118, UDR-0101).

Interactive lane: ``stream_workflow`` compiles the selected workflow (CTR-0180) and
runs it, translating MAF ``WorkflowEvent``s into AG-UI SSE events -- agent-node text
flows as ordinary TEXT_MESSAGE_* events, and the graph structure is surfaced via
ADDITIVE CUSTOM ``workflow_*`` events (UDR-0101 D8). The AG-UI endpoint (CTR-0009)
branches here on ``state.workflow_id``.

Asynchronous lane: ``run_workflow_job`` is a ``workflow`` Pipeline job type
(FEAT-0021 registry, the UDR-0074 D7 extension point) that runs the same compiled
graph to completion with run history / log (CTR-0145) and cooperative cancel.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.workflow.loader import compile_for_run
from app.workflow.spec import WorkflowError

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncGenerator

    from app.pipeline.models import Job
    from app.pipeline.store import PipelineStore

logger = logging.getLogger(__name__)


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


# ---------------------------------------------------------------------------
# Interactive lane (AG-UI SSE) -- CTR-0009 branch
# ---------------------------------------------------------------------------
async def stream_workflow(
    workflow_id: str,
    message: str,
    encoder: Any,
    *,
    thread_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Run ``workflow_id`` and yield encoded AG-UI SSE event strings (UDR-0101 D5/D8).

    Emits: RUN_STARTED -> workflow_started (CUSTOM) -> per-node workflow_node_* CUSTOM
    events + TEXT_MESSAGE_* for agent-node output -> workflow_completed (CUSTOM) ->
    RUN_FINISHED. A compile / run failure emits RUN_ERROR. The caller (endpoint.py)
    supplies the encoder and the AG-UI lifecycle wrappers are produced here so the
    workflow branch is self-contained.
    """
    from ag_ui.core import (
        CustomEvent,
        EventType,
        RunErrorEvent,
        RunFinishedEvent,
        RunStartedEvent,
        TextMessageContentEvent,
        TextMessageEndEvent,
        TextMessageStartEvent,
    )

    from app.agui.endpoint import _generate_id  # id helper (module-local)

    run_id = _generate_id()
    thread = thread_id or _generate_id()
    yield encoder.encode(RunStartedEvent(type=EventType.RUN_STARTED, thread_id=thread, run_id=run_id))

    try:
        workflow = compile_for_run(workflow_id)
    except WorkflowError as exc:
        yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, message=str(exc)))
        return

    yield encoder.encode(
        CustomEvent(type=EventType.CUSTOM, name="workflow_started", value={"workflow_id": workflow_id})
    )

    msg_id: str | None = None
    last_text_node: str | None = None
    try:
        async for event in workflow.run(message, stream=True):
            etype = str(getattr(event, "type", ""))
            executor_id = getattr(event, "executor_id", None)

            if etype in ("executor_invoked", "executor_started"):
                yield encoder.encode(
                    CustomEvent(
                        type=EventType.CUSTOM,
                        name="workflow_node_started",
                        value={"node": executor_id, "iteration": getattr(event, "iteration", None)},
                    )
                )
                continue
            if etype in ("executor_completed", "executor_bypassed"):
                yield encoder.encode(
                    CustomEvent(
                        type=EventType.CUSTOM,
                        name="workflow_node_completed",
                        value={"node": executor_id},
                    )
                )
                # fall through so any text payload is still surfaced
            if etype == "request_info":
                yield encoder.encode(
                    CustomEvent(
                        type=EventType.CUSTOM,
                        name="workflow_input_request",
                        value={"request_id": getattr(event, "request_id", None), "node": executor_id},
                    )
                )
                continue
            if etype in ("failed", "error"):
                details = getattr(event, "details", None)
                yield encoder.encode(
                    RunErrorEvent(type=EventType.RUN_ERROR, message=str(details or "Workflow failed"))
                )
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
                yield encoder.encode(
                    TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id=msg_id, delta=text)
                )

        if msg_id is not None:
            yield encoder.encode(TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id=msg_id))
        yield encoder.encode(
            CustomEvent(type=EventType.CUSTOM, name="workflow_completed", value={"workflow_id": workflow_id})
        )
        yield encoder.encode(RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id=thread, run_id=run_id))
    except Exception as exc:  # a node-agent / runtime failure ends the run
        logger.exception("Workflow run failed: %s", workflow_id)
        yield encoder.encode(RunErrorEvent(type=EventType.RUN_ERROR, message=f"Workflow run failed: {exc}"))


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
    try:
        async for event in workflow.run(message, stream=True):
            etype = str(getattr(event, "type", ""))
            if etype in ("executor_completed", "executor_bypassed"):
                nodes += 1
            if etype in ("failed", "error"):
                raise WorkflowError(str(getattr(event, "details", None) or "Workflow failed"))
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
        job.error = f"Workflow run failed: {exc}"
        job.completed_at = datetime.now(UTC).isoformat()
        storage.save(job)
        return

    job.status = JobStatus.completed
    job.progress = 100
    job.progress_message = "Workflow completed"
    job.result = {"workflow_id": workflow_id, "nodes_completed": nodes, "output": "".join(outputs)}
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


__all__ = ["register_workflow_job_type", "run_workflow_job", "stream_workflow"]
