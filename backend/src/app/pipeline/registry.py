"""Pipeline job-type registry (CTR-0073, PRP-0096, UDR-0074 D7).

The single extension point for pipeline job types. A new job type is added by:

  1. implementing an async runner ``run(job, store, cancel_event)`` under jobs/,
  2. registering it here in JOB_TYPES with a ParamSpec list + display metadata.

The engine (CTR-0073) dispatches by ``job.type``; the REST API (CTR-0146) exposes
JOB_TYPES via GET /api/pipeline/types so the SPA (CTR-0148) can render a job-type
submission form with zero bespoke frontend code per type.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.pipeline.jobs.rag_ingest import run_rag_ingest_job

if TYPE_CHECKING:
    import asyncio

    from app.pipeline.models import Job
    from app.pipeline.store import PipelineStore

JobRunner = Callable[["Job", "PipelineStore", "asyncio.Event"], Awaitable[None]]


@dataclass(frozen=True)
class ParamSpec:
    """A single job-type parameter descriptor (drives the SPA submission form)."""

    name: str
    label: str
    type: str = "string"  # string | int | number
    required: bool = False
    default: Any = None
    help: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "required": self.required,
            "default": self.default,
            "help": self.help,
        }


@dataclass(frozen=True)
class JobType:
    """A registered pipeline job type."""

    name: str
    label: str
    description: str
    runner: JobRunner
    params: list[ParamSpec] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "params": [p.to_dict() for p in self.params],
        }


# --- Registered job types. rag-ingest is the first (FEAT-0022, CTR-0076). ---
JOB_TYPES: dict[str, JobType] = {
    "rag-ingest": JobType(
        name="rag-ingest",
        label="RAG Ingest (PDF)",
        description=(
            "Parse a PDF, chunk it, embed the chunks, and store them in the ChromaDB "
            "vector collection so the agent's rag_search tool can cite them."
        ),
        runner=run_rag_ingest_job,
        params=[
            ParamSpec(
                name="file_path",
                label="PDF path",
                type="string",
                required=True,
                help="Path to the PDF (e.g. .uploads/<thread>/file.pdf).",
            ),
            ParamSpec(name="collection", label="Collection", type="string", help="ChromaDB collection name."),
            ParamSpec(name="chunk_size", label="Chunk size", type="int", help="Characters per chunk."),
            ParamSpec(name="chunk_overlap", label="Chunk overlap", type="int", help="Overlap characters."),
            ParamSpec(name="chunk_min_size", label="Min chunk size", type="int", help="Trailing-chunk merge floor."),
        ],
    ),
}


def get_runner(job_type: str) -> JobRunner | None:
    """Return the runner for a job type, or None if unknown."""
    jt = JOB_TYPES.get(job_type)
    return jt.runner if jt else None


def get_available_types() -> list[str]:
    """Return the list of registered job-type names."""
    return list(JOB_TYPES.keys())


def job_types_info() -> list[dict[str, Any]]:
    """Return serializable descriptors for every job type (for GET /types)."""
    return [jt.to_dict() for jt in JOB_TYPES.values()]


__all__ = [
    "JOB_TYPES",
    "JobType",
    "ParamSpec",
    "get_available_types",
    "get_runner",
    "job_types_info",
]
