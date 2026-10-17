"""Ontology searches in worker processes, with time limits (CTR-0170 v9, PRP-0211, UDR-0193 D3-D6).

pyoxigraph's ``Store.query()`` and owlrl's closure cannot be interrupted, and a Python
thread cannot be stopped from outside, so a search that runs too long can only be
stopped by ENDING the process it runs in. The search lanes -- ``POST /{id}/query``, the
schema summary and execution of ``POST /{id}/nl-query``, and the agent tool
``ontology_query`` -- therefore send their work to long-lived worker processes:

- A **supervisor** in the API process keeps ``ontology_query_workers`` workers (spawned
  on the first search). A job goes to an idle worker that served the same ontology last
  (its query cache is warm), else to any idle worker, else it waits (FIFO).
- Each job carries a **snapshot** of the ``ontology_*`` App Settings (the cache budget
  split between the workers), applied in the worker first, so run-time changes reach it.
- The worker reports its **phase** (``load``, ``entailment``, ``query``, ``explain``) and
  answers the result, or the exception with its type.
- **Time limits**: ``ontology_query_timeout_seconds`` for the Ontology screen and
  ``ontology_tool_timeout_seconds`` for the agent (0 = none), counted from the request's
  arrival, waiting included, the model's SPARQL generation excluded. A job still waiting
  is withdrawn; a running job's worker is ended and replaced (``QueryTimeout``).
- A caller that goes away (closed page, Stop, cancelled agent run) stops its job the
  same way (``QueryStopped``). A worker that dies answers ``WorkerFailed``.

``ontology_query_workers = 0`` runs the same work in the API process's threads, as
before PRP-0211, without time limits.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import logging
import multiprocessing
import threading
import time
from typing import TYPE_CHECKING, Any

from app.core.config import settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

SCREEN = "screen"
AGENT = "agent"
LANE_SETTINGS = {SCREEN: "ontology_query_timeout_seconds", AGENT: "ontology_tool_timeout_seconds"}
MAX_WORKERS = 8
POLL_SECONDS = 0.1
_RECENT = 8
PHASE_LABELS = {
    "queued": "waiting for a free search worker",
    "load": "loading the ontology",
    "entailment": "computing entailment",
    "query": "running the query",
    "explain": "explaining",
}


class QueryTimeout(Exception):
    """The search ran out of time; its worker (if it had started) was ended."""

    code = "query_timeout"

    def __init__(self, *, limit: float, phase: str, lane: str) -> None:
        setting = LANE_SETTINGS.get(lane, LANE_SETTINGS[SCREEN])
        doing = PHASE_LABELS.get(phase, phase)
        message = (
            f"The search did not finish within {limit:g} s (it was {doing}). Narrow the query, "
            f"or raise {setting} in App Settings."
        )
        super().__init__(message)
        self.detail = {
            "error": self.code,
            "message": message,
            "limit_seconds": limit,
            "phase": phase,
            "setting": setting,
        }


class QueryStopped(Exception):
    """The caller went away; the search was stopped."""


class WorkerFailed(RuntimeError):
    """A search worker exited unexpectedly; it was replaced."""


# ---- The work (runs in a worker process, or in a thread with 0 workers) --------------


def _op_summary(payload: dict[str, Any], phase: Callable[[str], None]) -> str:
    from app.ontology import nl, store

    phase("load")
    graph = store.load_store(payload["ontology_id"], payload.get("scope"), payload.get("imports"))
    return nl.cached_schema_summary(graph)


def _op_query(payload: dict[str, Any], phase: Callable[[str], None]) -> dict[str, Any]:
    from app.ontology import entailment, store

    phase("load")
    graph = store.load_store(payload["ontology_id"], payload.get("scope"), payload.get("imports"))
    mode = entailment.parse_mode(payload.get("mode"))
    if mode != entailment.SIMPLE:
        phase("entailment")
        store.ensure_entailment(graph, mode, compute=payload.get("compute", True))
    phase("query")
    return store.execute_query(
        graph,
        payload["sparql"],
        max_construct_triples=int(payload.get("max_construct_triples") or 0),
        entailment=mode,
    )


def _op_explain(payload: dict[str, Any], phase: Callable[[str], None]) -> dict[str, Any]:
    from app.ontology import entailment, explain, store

    phase("load")
    graph = store.load_store(payload["ontology_id"], payload.get("scope"), payload.get("imports"))
    mode = entailment.parse_mode(payload.get("mode"))
    phase("entailment")
    store.ensure_entailment(graph, mode, compute=payload.get("compute", True))
    phase("explain")
    return explain.explain(graph, mode, statement=payload.get("statement"), finding=payload.get("finding"))


def _op_statements(payload: dict[str, Any], phase: Callable[[str], None]) -> dict[str, Any]:
    from app.ontology import inferred_views

    return inferred_views.statements_op(payload, phase)


OPS: dict[str, Callable[[dict[str, Any], Callable[[str], None]], Any]] = {
    "summary": _op_summary,
    "query": _op_query,
    "explain": _op_explain,
    "statements": _op_statements,
}


def _no_phase(_name: str) -> None:
    return None


def _worker_main(conn: Any) -> None:
    """A worker process: answer jobs until the pipe closes."""
    from app.pipeline.process import apply_settings, encode_error

    while True:
        try:
            op, payload, snapshot = conn.recv()
        except (EOFError, OSError):
            return
        try:
            apply_settings(snapshot)
            result = OPS[op](payload, lambda name: conn.send(("phase", name)))
            conn.send(("ok", result))
        except BaseException as exc:  # everything goes back to the API process
            data, text = encode_error(exc)
            try:
                conn.send(("err", data, text))
            except (OSError, ValueError):
                return


# ---- The supervisor (API process) ---------------------------------------------------


@dataclass(eq=False)
class _Worker:
    process: Any
    conn: Any
    busy: bool = False
    recent: list[str] = field(default_factory=list)

    def served(self, ontology_id: str | None) -> None:
        if ontology_id:
            if ontology_id in self.recent:
                self.recent.remove(ontology_id)
            self.recent.insert(0, ontology_id)
            del self.recent[_RECENT:]


def _start_worker() -> _Worker:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=True)
    process = context.Process(target=_worker_main, args=(child,), daemon=True, name="ontology-search-worker")
    process.start()
    child.close()
    return _Worker(process=process, conn=parent)


def _end_worker(worker: _Worker) -> None:
    from app.pipeline.process import end_process

    with contextlib.suppress(Exception):
        worker.conn.close()
    with contextlib.suppress(Exception):
        end_process(worker.process)


def configured_workers() -> int:
    return max(0, min(MAX_WORKERS, int(settings.ontology_query_workers)))


class Supervisor:
    """The pool of search workers (thread-safe; blocking calls run in worker threads)."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._workers: list[_Worker] = []
        self.stats = {"jobs": 0, "timeouts": 0, "stopped": 0, "failed": 0, "replaced": 0}

    def _fit(self) -> None:
        """Start missing workers; end idle ones beyond the configured number (lock held)."""
        size = configured_workers()
        for worker in [w for w in self._workers if not w.busy and not w.process.is_alive()]:
            self._workers.remove(worker)
            self.stats["replaced"] += 1
        while len(self._workers) < size:
            self._workers.append(_start_worker())
        extra = len(self._workers) - size
        for worker in [w for w in self._workers if not w.busy][: max(0, extra)]:
            self._workers.remove(worker)
            threading.Thread(target=_end_worker, args=(worker,), daemon=True).start()

    def _acquire(self, affinity: str | None, deadline: float | None, stop: threading.Event) -> _Worker:
        with self._cond:
            while True:
                self._fit()
                idle = [w for w in self._workers if not w.busy]
                if idle:
                    chosen = next((w for w in idle if affinity and affinity in w.recent), idle[0])
                    chosen.busy = True
                    return chosen
                if stop.is_set():
                    raise QueryStopped
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError
                self._cond.wait(0.25)

    def _release(self, worker: _Worker, *, broken: bool) -> None:
        with self._cond:
            if broken:
                if worker in self._workers:
                    self._workers.remove(worker)
                threading.Thread(target=_end_worker, args=(worker,), daemon=True).start()
                if len(self._workers) < configured_workers():
                    self._workers.append(_start_worker())  # replaced at once
                    self.stats["replaced"] += 1
            else:
                worker.busy = False
            self._cond.notify_all()

    def run(
        self,
        op: str,
        payload: dict[str, Any],
        *,
        timeout: float | None,
        stop: threading.Event,
        lane: str,
        affinity: str | None = None,
    ) -> Any:
        """Run one job in a worker (blocking). See the module notes for the errors."""
        from app.pipeline.process import decode_error, settings_snapshot

        started = time.monotonic()
        deadline = started + timeout if timeout else None
        snapshot = settings_snapshot()
        snapshot["ontology_query_cache_mb"] = int(settings.ontology_query_cache_mb) // max(1, configured_workers())
        try:
            worker = self._acquire(affinity, deadline, stop)
        except TimeoutError:
            self.stats["timeouts"] += 1
            raise QueryTimeout(limit=float(timeout or 0), phase="queued", lane=lane) from None
        self.stats["jobs"] += 1
        phase = "load"
        broken = False
        try:
            worker.conn.send((op, payload, snapshot))
            while True:
                if worker.conn.poll(POLL_SECONDS):
                    message = worker.conn.recv()
                    if message[0] == "phase":
                        phase = message[1]
                        continue
                    if message[0] == "ok":
                        worker.served(affinity)
                        return message[1]
                    worker.served(affinity)
                    raise decode_error(message[1], message[2])
                if not worker.process.is_alive():
                    broken = True
                    self.stats["failed"] += 1
                    raise WorkerFailed(f"The search worker exited (code {worker.process.exitcode}).")
                if stop.is_set():
                    broken = True
                    self.stats["stopped"] += 1
                    logger.info("ontology search stopped (caller gone): %s %s, phase %s", lane, affinity, phase)
                    raise QueryStopped
                if deadline is not None and time.monotonic() > deadline:
                    broken = True
                    self.stats["timeouts"] += 1
                    logger.warning(
                        "ontology search timed out: %s %s, phase %s, %.1f s", lane, affinity, phase, timeout or 0
                    )
                    raise QueryTimeout(limit=float(timeout or 0), phase=phase, lane=lane)
        except (EOFError, OSError, BrokenPipeError) as exc:
            broken = True
            self.stats["failed"] += 1
            raise WorkerFailed(f"The search worker failed ({type(exc).__name__}).") from exc
        finally:
            self._release(worker, broken=broken)

    def shutdown(self) -> None:
        with self._cond:
            workers, self._workers = self._workers, []
        for worker in workers:
            _end_worker(worker)

    def processes(self) -> list[Any]:
        """The worker processes (tests and diagnostics)."""
        with self._cond:
            return [w.process for w in self._workers]


supervisor = Supervisor()


# ---- Async facade for the router and the agent tool ----------------------------------


class Budget:
    """The time a request may still spend (None = no limit); the model call is not spent."""

    def __init__(self, lane: str) -> None:
        self.lane = lane
        limit = int(getattr(settings, LANE_SETTINGS.get(lane, LANE_SETTINGS[SCREEN])))
        self.limit: float | None = float(limit) if limit > 0 and configured_workers() > 0 else None
        self.spent = 0.0

    def remaining(self) -> float | None:
        if self.limit is None:
            return None
        return max(0.01, self.limit - self.spent)

    def deadline(self) -> float | None:
        remaining = self.remaining()
        return None if remaining is None else time.monotonic() + remaining


async def run(
    op: str,
    payload: dict[str, Any],
    *,
    budget: Budget,
    is_gone: Callable[[], Awaitable[bool]] | None = None,
) -> Any:
    """Run ``op`` in a worker (or in a thread with 0 workers) within ``budget``."""
    from fastapi.concurrency import run_in_threadpool

    started = time.monotonic()
    try:
        if configured_workers() == 0:
            return await run_in_threadpool(OPS[op], payload, _no_phase)
        stop = threading.Event()
        future = asyncio.ensure_future(
            run_in_threadpool(
                supervisor.run,
                op,
                payload,
                timeout=budget.remaining(),
                stop=stop,
                lane=budget.lane,
                affinity=payload.get("ontology_id"),
            )
        )
        try:
            while True:
                done, _ = await asyncio.wait({future}, timeout=0.5)
                if future in done:
                    return future.result()
                if is_gone is not None and not stop.is_set():
                    with contextlib.suppress(Exception):
                        if await is_gone():
                            stop.set()
        except asyncio.CancelledError:
            stop.set()
            raise
    finally:
        budget.spent += time.monotonic() - started


async def query(
    ontology_id: str,
    *,
    scope: Any,
    imports: bool | None,
    sparql: str,
    mode: str,
    budget: Budget,
    max_construct_triples: int = 0,
    is_gone: Callable[[], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """One search: wait for the stored entailment when needed, then query (UDR-0193 D9)."""
    compute = await _entailment_ready(ontology_id, scope, imports, mode, budget)
    payload = {
        "ontology_id": ontology_id,
        "scope": scope,
        "imports": imports,
        "sparql": sparql,
        "mode": mode,
        "compute": compute,
        "max_construct_triples": max_construct_triples,
    }
    return await run("query", payload, budget=budget, is_gone=is_gone)


async def summary(
    ontology_id: str,
    *,
    scope: Any,
    imports: bool | None,
    budget: Budget,
    is_gone: Callable[[], Awaitable[bool]] | None = None,
) -> str:
    """The NL schema summary of the ontology (for SPARQL generation)."""
    payload = {"ontology_id": ontology_id, "scope": scope, "imports": imports}
    return await run("summary", payload, budget=budget, is_gone=is_gone)


async def explain(
    ontology_id: str,
    *,
    scope: Any,
    imports: bool | None,
    mode: str,
    statement: Any = None,
    finding: Any = None,
    budget: Budget,
    is_gone: Callable[[], Awaitable[bool]] | None = None,
) -> dict[str, Any]:
    """Explain one inferred statement or inconsistency (UDR-0193 D14)."""
    compute = await _entailment_ready(ontology_id, scope, imports, mode, budget)
    payload = {
        "ontology_id": ontology_id,
        "scope": scope,
        "imports": imports,
        "mode": mode,
        "compute": compute,
        "statement": statement,
        "finding": finding,
    }
    return await run("explain", payload, budget=budget, is_gone=is_gone)


async def _entailment_ready(ontology_id: str, scope: Any, imports: bool | None, mode: str, budget: Budget) -> bool:
    """Wait for the stored result when builds are Pipeline Jobs; True = the worker may compute."""
    from app.ontology import entailment, entailment_jobs

    if entailment.parse_mode(mode) == entailment.SIMPLE:
        return True
    if not entailment_jobs.active() or configured_workers() == 0:
        return True
    started = time.monotonic()
    try:
        await entailment_jobs.wait_ready(ontology_id, scope, imports, mode, budget.deadline())
    finally:
        budget.spent += time.monotonic() - started
    return False


def shutdown() -> None:
    """End the workers (app shutdown)."""
    supervisor.shutdown()


__all__ = [
    "AGENT",
    "LANE_SETTINGS",
    "SCREEN",
    "Budget",
    "QueryStopped",
    "QueryTimeout",
    "Supervisor",
    "WorkerFailed",
    "configured_workers",
    "explain",
    "query",
    "run",
    "shutdown",
    "summary",
    "supervisor",
]
