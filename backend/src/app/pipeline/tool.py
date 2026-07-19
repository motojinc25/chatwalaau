"""manage_pipeline MAF Function Tool (CTR-0147, PRP-0096, UDR-0074 D9).

A single agent tool that lets the LLM submit / list / get / cancel / delete pipeline
jobs (today: rag-ingest, e.g. "ingest this PDF into the knowledge base"). It writes
through the SAME engine + store path as the REST API (CTR-0146), so the agent and the
portal never diverge -- mirroring the manage_cron / Cron API relationship.

Registered on the shared agent only when PIPELINE_ENABLED (agent_factory). Replaces the
former batch MCP tools (submit_job / list_jobs / get_job / cancel_job / delete_job). It
is NOT in the approval require-set; pipeline jobs run curated in-process job types only.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from pydantic import Field

from app.pipeline.engine import queue
from app.pipeline.registry import get_available_types, job_types_info

logger = logging.getLogger(__name__)

PIPELINE_TOOL_INSTRUCTION = (
    "\n\n## Pipeline Jobs\n"
    "You can run data-processing PIPELINE jobs via the manage_pipeline tool. The main "
    "job type is 'rag-ingest', which parses a PDF, chunks and embeds it, and stores it in "
    "the vector knowledge base so rag_search can cite it. Use action='submit' with "
    "job_type='rag-ingest' and file_path set to the uploaded PDF's filename (a bare "
    "filename is resolved against the uploads folder) or a full path. Use action='list' "
    "to see jobs, 'get' for one job's progress, 'cancel' to stop a running job, and "
    "'delete' to remove a finished job. Pipeline jobs run only curated in-process job "
    "types (no shell)."
)


def _summary(job: dict) -> dict:
    """A compact job view for tool results (avoid dumping the whole record)."""
    return {
        "id": job.get("id"),
        "type": job.get("type"),
        "status": job.get("status"),
        "progress": job.get("progress"),
        "progress_message": job.get("progress_message"),
        "error": job.get("error"),
    }


async def manage_pipeline(
    action: Annotated[str, Field(description="One of: submit, list, get, cancel, delete.")],
    job_type: Annotated[str, Field(description="For submit: a registered type, e.g. 'rag-ingest'.")] = "",
    file_path: Annotated[
        str,
        Field(
            description="For rag-ingest: the uploaded PDF's filename (resolved against the uploads folder) or a path."
        ),
    ] = "",
    collection: Annotated[str, Field(description="For rag-ingest: optional ChromaDB collection name.")] = "",
    chunk_size: Annotated[int, Field(description="For rag-ingest: optional characters per chunk.")] = 0,
    chunk_overlap: Annotated[int, Field(description="For rag-ingest: optional overlap characters.")] = 0,
    params_json: Annotated[str, Field(description="Optional JSON object of params for non-rag job types.")] = "",
    job_id: Annotated[str, Field(description="For get/cancel/delete: the target job id.")] = "",
) -> str:
    """Submit, list, get, cancel, or delete pipeline jobs (e.g. RAG ingest)."""
    act = (action or "").strip().lower()

    if act == "list":
        jobs = [_summary(j.model_dump()) for j in queue.list_jobs()]
        return json.dumps({"jobs": jobs, "types": get_available_types()}, ensure_ascii=False)

    if act == "get":
        if not job_id:
            return "Error: 'job_id' is required for get."
        job = queue.get_status(job_id)
        if job is None:
            return f"Error: no pipeline job with id {job_id}."
        return json.dumps({"job": _summary(job.model_dump())}, ensure_ascii=False)

    if act == "cancel":
        if not job_id:
            return "Error: 'job_id' is required for cancel."
        try:
            job = await queue.cancel(job_id)
        except ValueError as exc:
            return f"Error: {exc}"
        return json.dumps({"cancelled": _summary(job.model_dump())}, ensure_ascii=False)

    if act == "delete":
        if not job_id:
            return "Error: 'job_id' is required for delete."
        try:
            ok = queue.delete_job(job_id)
        except ValueError as exc:
            return f"Error: {exc}"
        return json.dumps({"deleted": ok, "id": job_id})

    if act == "submit":
        if not job_type:
            return f"Error: 'job_type' is required for submit. Available: {', '.join(get_available_types())}."
        params = _build_params(job_type, file_path, collection, chunk_size, chunk_overlap, params_json)
        if isinstance(params, str):
            return params  # an error message
        try:
            job = await queue.submit(job_type, params)
        except ValueError as exc:
            return f"Error: {exc}"
        logger.info("manage_pipeline submitted job %s (%s)", job.id, job.type)
        return json.dumps({"submitted": _summary(job.model_dump())}, ensure_ascii=False)

    return "Error: 'action' must be one of submit, list, get, cancel, delete."


def _build_params(
    job_type: str,
    file_path: str,
    collection: str,
    chunk_size: int,
    chunk_overlap: int,
    params_json: str,
) -> dict | str:
    """Assemble the job params dict from the flat tool args, or return an error string."""
    if params_json.strip():
        try:
            parsed = json.loads(params_json)
        except json.JSONDecodeError as exc:
            return f"Error: params_json is not valid JSON: {exc}"
        if not isinstance(parsed, dict):
            return "Error: params_json must be a JSON object."
        return parsed

    if job_type == "rag-ingest":
        if not file_path:
            return "Error: 'file_path' is required for rag-ingest."
        params: dict = {"file_path": file_path}
        if collection:
            params["collection"] = collection
        if chunk_size > 0:
            params["chunk_size"] = chunk_size
        if chunk_overlap > 0:
            params["chunk_overlap"] = chunk_overlap
        return params

    known = ", ".join(t["name"] for t in job_types_info())
    return f"Error: provide params_json for job_type '{job_type}'. Known types: {known}."


__all__ = ["PIPELINE_TOOL_INSTRUCTION", "manage_pipeline"]
