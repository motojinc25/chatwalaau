"""Teams Meeting Pipeline Job (CTR-0156, PRP-0097, UDR-0076).

The ``teams-meeting`` pipeline job type. It is OWNED BY CAP-010 but REGISTERED INTO
the CAP-002 in-process Pipeline Job Engine (CTR-0073) via the UDR-0074 D7 extension
point, preserving the one-way CAP-010 -> CAP-002 dependency (UDR-0076 D2).

Lifecycle (UDR-0076 D3), recorded as progress + message in the run history (CTR-0145):

    received -> resolving_meeting -> fetching_transcript -> summarizing
             -> writing_output -> done | failed

It is triggered by a Graph webhook notification (CTR-0150 ``handle_notification``) or
manually by Fetch (CTR-0154 / CTR-0155 ``fetch``), so the pipeline is testable without
a live subscription (UDR-0076 D4). Meeting resolution + transcript fetch go through the
Graph app-only client (CTR-0153, UDR-0076 D5); the summary JSON is produced through the
registry chokepoint (CTR-0102/CTR-0070, DEMO_MODE honored, UDR-0076 D6); the output is
written into the CTR-0031 workspace jail (UDR-0076 D7).

PRP-0098 / UDR-0077: the job gains an additive ``auth_mode`` param. ``app_only`` (default)
is the original organizer-scoped app-only lane (byte-for-byte unchanged). ``delegated`` is
the user-delegated, user-scoped lane (FEAT-0054): a ``_MeetingAccess`` abstraction swaps
the Graph client (CTR-0158) and the path root (``me/onlineMeetings``), fed an in-memory
token addressed by an opaque ``token_ref`` (the token is never persisted, UDR-0077 D3).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import logging
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any

from app.core.config import settings
from app.pipeline.models import Job, JobStatus
from app.pipeline.registry import JobType, ParamSpec, register_job_type
from app.webhook import store as webhook_store
from app.webhook.msgraph import SOURCE_NAME, graph_client, graph_delegated_client

if TYPE_CHECKING:
    from app.pipeline.store import PipelineStore

logger = logging.getLogger(__name__)

JOB_TYPE = "teams-meeting"

# Lifecycle phase -> progress percentage (UDR-0076 D3).
PHASE_RECEIVED = 0
PHASE_RESOLVING = 10
PHASE_FETCHING = 40
PHASE_SUMMARIZING = 70
PHASE_WRITING = 90
PHASE_DONE = 100

_SUMMARY_SYSTEM_PROMPT = (
    "You summarize a Microsoft Teams meeting transcript into STRICT JSON. Return ONLY a "
    "JSON object with keys: title (string), summary (string), decisions (string array), "
    "action_items (array of objects with owner and task), participants (string array). "
    "Do not wrap the JSON in markdown fences."
)


class _MeetingAccess:
    """How this run talks to Graph for meeting + transcript reads (PRP-0098, UDR-0077 D1/D4).

    ``app_only`` (the default; CTR-0153) is organizer-scoped via
    ``users/{organizerId}/onlineMeetings`` and requires a Teams Application Access Policy.
    ``delegated`` (CTR-0158) is the user-delegated, user-scoped lane via
    ``me/onlineMeetings`` with an in-memory token (held for this run only, never persisted).
    The transcript-fetch retry logic is shared by both because both raise the same
    ``graph_client.GraphApiError``.
    """

    def __init__(self, *, mode: str, organizer_id: str = "", token: str = "") -> None:
        self.mode = mode
        self.organizer_id = organizer_id
        self.token = token

    @property
    def transcript_base(self) -> str:
        if self.mode == "delegated":
            return "me/onlineMeetings"
        return f"users/{self.organizer_id}/onlineMeetings"

    async def resolve(self, *, meeting_id: str, join_web_url: str) -> dict[str, Any]:
        if self.mode == "delegated":
            return await graph_delegated_client.resolve_online_meeting_me(
                self.token, meeting_id=meeting_id, join_web_url=join_web_url
            )
        return await graph_client.resolve_online_meeting(
            organizer_id=self.organizer_id, meeting_id=meeting_id, join_web_url=join_web_url
        )

    async def get(self, path: str) -> dict[str, Any]:
        if self.mode == "delegated":
            return await graph_delegated_client.get_me(self.token, path)
        return await graph_client.get(path)

    async def get_text(self, path: str) -> str:
        if self.mode == "delegated":
            return await graph_delegated_client.get_text_me(self.token, path)
        return await graph_client.get_text(path)


def _fail(job: Job, storage: PipelineStore, phase: str, error: str) -> None:
    job.status = JobStatus.failed
    job.progress_message = f"failed:{phase}"
    job.error = error
    job.completed_at = datetime.now(UTC).isoformat()
    storage.save(job)
    logger.warning("teams-meeting job %s failed at %s: %s", job.id, phase, error)


def _advance(job: Job, storage: PipelineStore, progress: int, message: str) -> None:
    job.progress = progress
    job.progress_message = message
    storage.save(job)


async def run_teams_meeting_job(job: Job, storage: PipelineStore, cancel_event: asyncio.Event) -> None:
    """Resolve a meeting, fetch its transcript, summarize it, and write the JSON output."""
    _advance(job, storage, PHASE_RECEIVED, "received")

    auth_mode = (str(job.params.get("auth_mode", "app_only")).strip() or "app_only").lower()
    organizer_id = str(job.params.get("organizer_id", "")).strip()
    meeting_id = str(job.params.get("meeting_id", "")).strip()
    join_web_url = str(job.params.get("join_web_url", "")).strip()
    transcript_id = str(job.params.get("transcript_id", "")).strip()

    if auth_mode == "delegated":
        # User-delegated ("Dedicated") lane (FEAT-0054, UDR-0077). The access token is held
        # in process memory only (UDR-0077 D3) and addressed by an opaque token_ref in the
        # persisted params; it is consumed here once. If absent (e.g. after a restart) the
        # job fails clearly rather than silently falling back to app-only.
        if not graph_delegated_client.is_configured():
            _fail(
                job,
                storage,
                "resolving_meeting",
                "Microsoft Graph delegated credentials are not configured "
                "(GRAPH_TENANT_ID / GRAPH_CLIENT_ID).",
            )
            return
        token_ref = str(job.params.get("token_ref", "")).strip()
        token = await graph_delegated_client.take_run_token(token_ref) if token_ref else None
        if not token:
            _fail(
                job,
                storage,
                "resolving_meeting",
                "User-delegated access token is unavailable. It is held in memory only and "
                "never persisted (UDR-0077 D3); re-run the Dedicated fetch to sign in again.",
            )
            return
        access = _MeetingAccess(mode="delegated", token=token)
    else:
        if not graph_client.is_configured():
            _fail(job, storage, "resolving_meeting", "Microsoft Graph credentials are not configured.")
            return
        if not organizer_id:
            # App-only Teams meeting/transcript reads are organizer-scoped (UDR-0076 D5):
            # without the organizer the Graph path /users/{organizer}/onlineMeetings cannot
            # be built. The webhook path supplies it from the notification; the manual Fetch
            # path requires the operator to enter it.
            _fail(
                job,
                storage,
                "resolving_meeting",
                "Missing organizer user id. App-only access is organizer-scoped: provide the "
                "meeting organizer's AAD object id or UPN.",
            )
            return
        access = _MeetingAccess(mode="app_only", organizer_id=organizer_id)

    # --- resolving_meeting ---
    _advance(job, storage, PHASE_RESOLVING, "resolving_meeting")
    if cancel_event.is_set():
        return _set_cancelled(job, storage)
    meeting: dict[str, Any] = {}
    if meeting_id or join_web_url:
        try:
            meeting = await access.resolve(meeting_id=meeting_id, join_web_url=join_web_url)
            meeting_id = meeting.get("id", meeting_id)
        except Exception as exc:
            # Resolution is best-effort metadata (subject); the transcript is the goal. If we
            # already have a meeting_id + transcript_id (the webhook path), continue and let
            # the fetch stage be authoritative. Otherwise (e.g. join URL only) we cannot
            # proceed without a meeting id.
            if not (meeting_id and transcript_id):
                _fail(job, storage, "resolving_meeting", f"Failed to resolve meeting: {exc}")
                return
            logger.warning("teams-meeting job %s: meeting resolve failed, continuing: %s", job.id, exc)

    # --- fetching_transcript ---
    # A transcript artifact is frequently NOT ready the instant a meeting ends; Graph can
    # take several minutes to produce it. Poll with backoff up to a configured deadline,
    # surfacing the wait in the progress message and honoring cancellation.
    _advance(job, storage, PHASE_FETCHING, "fetching_transcript")
    transcript_text = ""
    try:
        transcript_text = await _fetch_transcript_with_wait(
            access=access,
            meeting_id=meeting_id,
            transcript_id=transcript_id,
            job=job,
            storage=storage,
            cancel_event=cancel_event,
        )
    except _Cancelled:
        return _set_cancelled(job, storage)
    except Exception as exc:
        _fail(job, storage, "fetching_transcript", f"Failed to fetch transcript: {exc}")
        return
    if not transcript_text.strip():
        _fail(
            job,
            storage,
            "fetching_transcript",
            f"Transcript not available after waiting {settings.teams_meeting_transcript_max_wait_seconds}s "
            "(the artifact may still be generating; try again later).",
        )
        return

    # --- summarizing ---
    _advance(job, storage, PHASE_SUMMARIZING, "summarizing")
    if cancel_event.is_set():
        return _set_cancelled(job, storage)
    try:
        summary = await _summarize(transcript_text)
    except Exception as exc:
        _fail(job, storage, "summarizing", f"Failed to summarize transcript: {exc}")
        return

    # --- writing_output ---
    _advance(job, storage, PHASE_WRITING, "writing_output")
    try:
        out_path = _write_output(meeting_id or job.id, meeting, summary)
    except Exception as exc:
        _fail(job, storage, "writing_output", f"Failed to write output: {exc}")
        return

    # --- done ---
    job.status = JobStatus.completed
    job.progress = PHASE_DONE
    job.progress_message = "done"
    job.result = {
        "meeting_id": meeting_id,
        "subject": meeting.get("subject", ""),
        "output_path": out_path,
        "transcript_chars": len(transcript_text),
    }
    job.completed_at = datetime.now(UTC).isoformat()
    storage.save(job)
    logger.info("teams-meeting job %s done: %s", job.id, out_path)


class _Cancelled(Exception):
    """Raised internally when a cancel is requested during the transcript wait."""


async def _fetch_transcript_with_wait(
    *,
    access: _MeetingAccess,
    meeting_id: str,
    transcript_id: str,
    job: Job,
    storage: PipelineStore,
    cancel_event: asyncio.Event,
) -> str:
    """Poll for the transcript artifact until ready or the deadline, then return its text.

    "Not ready" (empty transcript list, or a 404 on the content) is retried with a fixed
    poll interval up to TEAMS_MEETING_TRANSCRIPT_MAX_WAIT_SECONDS. Hard errors (auth /
    403 missing Application Access Policy / 400) are raised immediately -- waiting would
    not help. Returns "" if still not ready at the deadline (the caller reports that).
    """
    if not meeting_id:
        msg = "A meeting id is required to fetch a transcript."
        raise graph_client.GraphApiError(400, msg)

    max_wait = max(0, int(settings.teams_meeting_transcript_max_wait_seconds))
    poll = max(5, int(settings.teams_meeting_transcript_poll_seconds))
    deadline = time.monotonic() + max_wait
    attempt = 0
    while True:
        if cancel_event.is_set():
            raise _Cancelled
        text = await _try_fetch_transcript(access, meeting_id, transcript_id)
        if text and text.strip():
            return text
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        attempt += 1
        waited = max_wait - max(0, int(remaining))
        _advance(
            job,
            storage,
            PHASE_FETCHING,
            f"fetching_transcript (waiting for the artifact, ~{waited}s/{max_wait}s)",
        )
        # Sleep in short slices so a cancel is honored promptly.
        slept = 0.0
        step = min(float(poll), max(0.0, remaining))
        while slept < step:
            if cancel_event.is_set():
                raise _Cancelled
            await asyncio.sleep(min(2.0, step - slept))
            slept += 2.0


async def _try_fetch_transcript(access: _MeetingAccess, meeting_id: str, transcript_id: str) -> str | None:
    """One transcript fetch attempt. Returns text, or None when not ready yet.

    A 404 (transcript / content not yet produced) maps to None (retry); any other Graph
    error propagates (it will not be fixed by waiting). The Graph path root differs by auth
    lane (``users/{organizerId}`` app-only vs ``me`` delegated) via ``access`` (UDR-0077 D4).
    """
    base = f"{access.transcript_base}/{meeting_id}/transcripts"
    tid = transcript_id
    if not tid:
        try:
            listing = await access.get(base)
        except graph_client.GraphApiError as exc:
            if exc.status_code == 404:
                return None
            raise
        items = listing.get("value") or []
        if not items:
            return None
        tid = items[0]["id"]
    try:
        return await access.get_text(f"{base}/{tid}/content?$format=text/vtt")
    except graph_client.GraphApiError as exc:
        if exc.status_code == 404:
            return None
        raise


async def _summarize(transcript_text: str) -> dict[str, Any]:
    """Summarize the transcript into a structured JSON via the registry chokepoint."""
    from agent_framework import Message

    from app.agui.agent_registry import _build_chat_client

    model = settings.teams_meeting_summary_model.strip() or _default_model()
    client = _build_chat_client(model)
    # Bound the transcript to a safe context size (defensive; long meetings).
    excerpt = transcript_text[:48_000]
    messages = [
        Message(role="system", contents=[_SUMMARY_SYSTEM_PROMPT]),
        Message(role="user", contents=[f"Summarize this meeting transcript as JSON:\n\n{excerpt}"]),
    ]
    response = await client.get_response(messages, stream=False)
    text = (getattr(response, "text", "") or "").strip()
    return _parse_summary(text)


def _parse_summary(text: str) -> dict[str, Any]:
    """Best-effort parse of the model's JSON answer; tolerant of stray fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if "\n" in cleaned:
            cleaned = cleaned.split("\n", 1)[1]
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return {"summary": text, "_unparsed": True}


def _default_model() -> str:
    """Resolve the fallback summarization model (honors DEMO_MODE).

    Mirrors app.background.session_title._default_model: the configured default model
    comes from the provider registry, NOT from a module-level agent_registry singleton
    (there is none -- the registry is built at startup and held by app.main).
    """
    from app.demo import is_demo_mode

    if is_demo_mode():
        from app.demo import resolve_demo_models

        models = resolve_demo_models()
        return models[0] if models else "chatwalaau-demo"
    from app import providers

    resolved = providers.resolve_models()
    return resolved[0][0] if resolved else ""


def _write_output(name: str, meeting: dict[str, Any], summary: dict[str, Any]) -> str:
    """Write the summary JSON into the coding-workspace jail (CTR-0031, UDR-0076 D7)."""
    from app.coding.security import resolve_safe_path

    workspace = (settings.coding_workspace_dir or "").strip()
    if not workspace:
        msg = "CODING_WORKSPACE_DIR is not configured; cannot write meeting output."
        raise RuntimeError(msg)
    subdir = settings.teams_meeting_output_dir.strip() or "meeting-summaries"
    safe_id = "".join(c for c in name if c.isalnum() or c in "-_") or "meeting"
    rel = f"{subdir}/{safe_id}.json"
    safe_path = resolve_safe_path(workspace, rel)
    payload = {
        "meeting_id": meeting.get("id", name),
        "subject": meeting.get("subject", ""),
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": summary,
    }
    out = Path(safe_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return rel


def _set_cancelled(job: Job, storage: PipelineStore) -> None:
    job.status = JobStatus.cancelled
    job.completed_at = datetime.now(UTC).isoformat()
    storage.save(job)


# ---------------------------------------------------------------------------
# Triggers: webhook notification handoff + manual fetch
# ---------------------------------------------------------------------------


def _dedupe_key(params: dict[str, Any]) -> str:
    raw = "|".join(str(params.get(k, "")) for k in ("organizer_id", "meeting_id", "transcript_id", "join_web_url"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _parse_resource_ids(resource: str) -> dict[str, str]:
    """Extract ids from a Graph transcript notification resource path.

    The getAllTranscripts change-notification resource looks like
    ``users/{organizerId}/onlineMeetings/{meetingId}/transcripts/{transcriptId}`` (ids may
    be wrapped in quotes/parentheses, e.g. ``users('...')/onlineMeetings('...')``). Returns
    whatever of {organizer_id, meeting_id, transcript_id} can be parsed.
    """
    import re

    out: dict[str, str] = {}
    for key, segment in (("organizer_id", "users"), ("meeting_id", "onlineMeetings"), ("transcript_id", "transcripts")):
        m = re.search(rf"{segment}[(/']+([^)'/]+)", resource)
        if m:
            out[key] = m.group(1)
    return out


async def submit_meeting_job(params: dict[str, Any], *, idempotent: bool = True) -> dict[str, Any]:
    """Submit a teams-meeting pipeline job. Returns {job_id, deduped}.

    With ``idempotent`` (the webhook path), a stable key derived from the meeting /
    transcript identity is claimed first so a redelivered or restarted notification
    maps to the same work (UDR-0076 D8).
    """
    from app.pipeline.engine import queue

    if idempotent:
        key = _dedupe_key(params)
        # Reserve a tentative job, then claim the key; if not newly claimed, skip.
        job = await queue.submit(JOB_TYPE, params)
        if not webhook_store.claim_dedupe_key(SOURCE_NAME, key, job_id=job.id):
            # Already processed: delete the just-created duplicate, report deduped.
            import contextlib

            from app.pipeline.store import store as pstore

            with contextlib.suppress(Exception):
                pstore.delete(job.id)
            return {"job_id": None, "deduped": True}
        return {"job_id": job.id, "deduped": False}

    job = await queue.submit(JOB_TYPE, params)
    return {"job_id": job.id, "deduped": False}


async def handle_notification(notification: dict[str, Any]) -> dict[str, Any] | None:
    """Map a single accepted Graph notification to a teams-meeting job (CTR-0150 handoff).

    Returns {job_id, deduped} or None when the notification is not actionable.

    The callTranscript resourceData carries ``meetingId`` and ``meetingOrganizerId`` plus
    the transcript ``id``; anything missing is recovered from the resource path. App-only
    transcript reads need the organizer (UDR-0076 D5), so a notification without it is
    skipped rather than spawning a job that will fail.
    """
    resource = str(notification.get("resource", ""))
    rd = notification.get("resourceData") or {}
    rd = rd if isinstance(rd, dict) else {}
    from_path = _parse_resource_ids(resource)
    organizer_id = str(rd.get("meetingOrganizerId") or from_path.get("organizer_id", ""))
    meeting_id = str(rd.get("meetingId") or from_path.get("meeting_id", ""))
    transcript_id = str(rd.get("id") or from_path.get("transcript_id", ""))
    if not (organizer_id and meeting_id):
        logger.warning("teams-meeting notification not actionable (organizer/meeting id missing): %s", resource)
        return None
    params = {
        "organizer_id": organizer_id,
        "meeting_id": meeting_id,
        "transcript_id": transcript_id,
        "resource": resource,
        "source": "webhook",
    }
    return await submit_meeting_job(params, idempotent=True)


async def fetch(*, organizer_id: str = "", meeting_id: str = "", join_web_url: str = "") -> dict[str, Any]:
    """Manually trigger the pipeline for a meeting (CTR-0154/0155 fetch; UDR-0076 D4).

    App-only access is organizer-scoped, so ``organizer_id`` (the meeting organizer's AAD
    object id or UPN) is required alongside a meeting id or join URL.
    """
    if not organizer_id:
        msg = "fetch requires organizer_id (the meeting organizer's user id or UPN)."
        raise ValueError(msg)
    if not (meeting_id or join_web_url):
        msg = "fetch requires meeting_id or join_web_url."
        raise ValueError(msg)
    # The numeric Teams "Meeting ID" (dial-in conference id, e.g. "233 618 417 845 803")
    # is NOT the Graph onlineMeeting id and cannot be resolved by Graph. Catch it early
    # with a clear message rather than letting Graph 400.
    if meeting_id and meeting_id.replace(" ", "").isdigit():
        msg = (
            "That looks like the numeric Teams 'Meeting ID' (the dial-in id), which "
            "Microsoft Graph cannot resolve directly. Use the meeting Join URL instead, or "
            "the Graph onlineMeeting id (a long opaque string)."
        )
        raise ValueError(msg)
    params = {
        "organizer_id": organizer_id,
        "meeting_id": meeting_id,
        "join_web_url": join_web_url,
        "source": "manual",
    }
    return await submit_meeting_job(params, idempotent=False)


async def submit_dedicated_job(token: str, *, meeting_id: str = "", join_web_url: str = "") -> dict[str, Any]:
    """Submit a user-delegated ("Dedicated") teams-meeting job (FEAT-0054, UDR-0077).

    Used by the Dedicated Fetch API (CTR-0159) once the device-code login has produced a
    user-delegated access token. The token is stashed in the process-local run-token map
    under an opaque ref; the persisted job params carry ONLY that ref (UDR-0077 D3), so the
    token never lands in the CTR-0145 store. ``organizer_id`` is NOT required -- delegated
    access is user-scoped (``/me/onlineMeetings``, UDR-0077 D4).
    """
    import uuid

    if not (meeting_id or join_web_url):
        msg = "A meeting id or join URL is required."
        raise ValueError(msg)
    if meeting_id and meeting_id.replace(" ", "").isdigit():
        msg = (
            "That looks like the numeric Teams 'Meeting ID' (the dial-in id), which "
            "Microsoft Graph cannot resolve directly. Use the meeting Join URL instead, or "
            "the Graph onlineMeeting id (a long opaque string)."
        )
        raise ValueError(msg)
    token_ref = uuid.uuid4().hex
    await graph_delegated_client.put_run_token(token_ref, token)
    params = {
        "auth_mode": "delegated",
        "token_ref": token_ref,
        "meeting_id": meeting_id,
        "join_web_url": join_web_url,
        "source": "dedicated",
    }
    try:
        return await submit_meeting_job(params, idempotent=False)
    except Exception:
        # Reclaim the stashed token if the job could not be enqueued.
        await graph_delegated_client.take_run_token(token_ref)
        raise


def register_meeting_job_type() -> None:
    """Register the teams-meeting job type into the pipeline engine (UDR-0074 D7)."""
    register_job_type(
        JobType(
            name=JOB_TYPE,
            label="Teams Meeting Summary",
            description=(
                "Resolve a Teams online meeting via Microsoft Graph, fetch its transcript, "
                "summarize it into a structured JSON, and write the result into the coding "
                "workspace."
            ),
            runner=run_teams_meeting_job,
            params=[
                ParamSpec(
                    name="organizer_id",
                    label="Organizer (user id / UPN)",
                    type="string",
                    required=True,
                    help="Meeting organizer's AAD object id or UPN (app-only access is organizer-scoped).",
                ),
                ParamSpec(
                    name="meeting_id",
                    label="Meeting ID",
                    type="string",
                    help="Graph onlineMeeting id (opaque string), NOT the numeric Teams 'Meeting ID'.",
                ),
                ParamSpec(
                    name="join_web_url",
                    label="Join URL",
                    type="string",
                    help="Meeting joinWebUrl (recommended; works directly).",
                ),
            ],
        )
    )


__all__ = [
    "JOB_TYPE",
    "fetch",
    "handle_notification",
    "register_meeting_job_type",
    "run_teams_meeting_job",
    "submit_dedicated_job",
    "submit_meeting_job",
]
