"""Ontology Management REST API (CTR-0171, PRP-0105, UDR-0084).

    GET    /api/ontology/catalog            -- list the catalog (also the SPA probe)
    POST   /api/ontology/catalog            -- create an ontology (name + description)
    PATCH  /api/ontology/catalog/{id}       -- rename (name and/or description)
    DELETE /api/ontology/catalog/{id}       -- delete (backup-then-remove)
    POST   /api/ontology/import             -- import Turtle / TriG / N-Triples / N-Quads /
                                               RDF-XML / JSON-LD (UDR-0182 D6)
    GET    /api/ontology/{id}               -- the JSON projection (CTR-0169 v3)
    PUT    /api/ontology/{id}               -- save the projection (backup + atomic)
    POST   /api/ontology/{id}/statements    -- explicit statement operations (UDR-0182 D9)
    GET    /api/ontology/{id}/export        -- download (?format=; lossy formats refused, D7)
    POST   /api/ontology/{id}/query         -- read-only SPARQL (SELECT/CONSTRUCT/ASK), scoped
    POST   /api/ontology/{id}/nl-query      -- natural language -> SPARQL -> execute, scoped
    GET    /api/ontology/{id}/history       -- versions, newest first (UDR-0184 D2)
    GET    /api/ontology/{id}/history/{version}/diff     -- statement diff (D4)
    GET    /api/ontology/{id}/history/{version}/file     -- download a version
    POST   /api/ontology/{id}/history/{version}/restore  -- restore a version (D5)
    GET    /api/ontology/trash              -- deleted ontologies (D6)
    POST   /api/ontology/trash/{id}/restore -- restore a deleted ontology under its id

Every endpoint depends on ``verify_api_key`` (CTR-0083; loopback bypass) --
the mutations and the query POSTs are gated per invariant 7, the GETs follow
the read convention. The whole surface returns 404 unless ONTOLOGY_ENABLED so
the SPA can gate its launcher icon by probing the catalog (UDR-0084 D12).

Under DEMO_MODE the eight WRITE endpoints (POST/PATCH/DELETE /catalog, POST /import,
PUT /{id}, POST /{id}/statements, POST /{id}/history/{version}/restore, POST
/trash/{id}/restore) refuse with 409 ``demo_mode`` and the manager renders read-only
(PRP-0139 / UDR-0122). Reads -- including the SPARQL query lanes, which cannot
mutate -- are deliberately untouched.

The frontend never parses RDF: GET/PUT carry the CTR-0169 v2 statement-complete
projection and this module delegates the codec to ``app.ontology.vocabulary``
(UDR-0084 D6, UDR-0180 D2). Import is validate-then-commit (full pyoxigraph parse +
the ONTOLOGY_MAX_FILE_BYTES cap) and stores canonical Turtle WITH the source's
prefixes / base / VERSION (UDR-0084 D10, UDR-0180 D6). GET returns a ``revision``
(SHA-256 of the stored file); a PUT carrying a different one is refused with 409
``stale_revision`` before anything is written (UDR-0180 D8).

Parsing, loading, querying, serializing and writing run in worker threads, never on
the event loop; every "check revision -> read -> apply -> write" holds the
ontology's write lock (CTR-0170 v4 / CTR-0171 v6, UDR-0183 D4). ``POST /statements``
with ``on_stale: "rebase"`` applies a request made against an older revision to the
current file, refusing it with 409 ``stale_conflict`` when it touches statements
that are gone (D5); fresh blank-node labels are renamed on every save (D6).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from app.auth import verify_api_key
from app.core.config import settings
from app.ontology import formats, history, nl, store
from app.ontology.operations import StaleConflict, StatementNotFound, apply_operations
from app.ontology.vocabulary import (
    PROJECTION_VERSION,
    ProjectionError,
    RemoteContextError,
    base_iri_for,
    import_document,
    is_named,
    new_document_turtle,
    projection_to_text,
    read_dataset,
    turtle_to_projection,
    write_document,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ontology", tags=["Ontology"])

# CTR-0169 v1 body keys: refused by PUT (422 legacy_projection) rather than ignored.
_LEGACY_PROJECTION_KEYS = frozenset({"entities", "relationships", "extra_turtle"})


class _ImportRejected(ValueError):
    """No supported format could parse the upload (422; nothing is written)."""


def _require_enabled() -> None:
    if not settings.ontology_enabled:
        raise HTTPException(status_code=404, detail={"error": "ontology_disabled"})


def _guard_demo() -> None:
    """Refuse a WRITE under DEMO_MODE (PRP-0139 / UDR-0122; the CTR-0146 shape).

    `.ontologies/` is shared process-wide, and this surface hands its contents
    out directly: GET /catalog lists every ontology by name and
    GET /{id}/export downloads the whole Turtle. Left writable on a demo host,
    one visitor's uploaded model is readable -- and deletable -- by the next.
    Reads stay open (UDR-0122 D2).
    """
    if settings.demo_mode:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "demo_mode",
                "message": "Creating, editing, importing and deleting ontologies is disabled under DEMO_MODE.",
            },
        )


def _entry_or_404(ontology_id: str) -> dict[str, str]:
    entry = store.get_entry(ontology_id)
    if entry is None:
        raise HTTPException(status_code=404, detail={"error": "ontology_not_found"})
    return entry


class OntologyCreate(BaseModel):
    name: str = Field(default="", max_length=200)
    # The catalog description is the disambiguation key the agent tool uses to
    # pick the right ontology (UDR-0084 D3) -- encourage a meaningful one.
    description: str = Field(default="", max_length=2000)


class OntologyRename(BaseModel):
    # At least one of name/description must be present (validated in the handler).
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class QueryRequest(BaseModel):
    sparql: str = Field(min_length=1, max_length=20000)
    # "all" (default: the canvas default, PRP-0200 Q4), "default", or one graph as
    # Term JSON (iri / bnode) -- UDR-0182 D4.
    scope: Any = None


class NlQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    scope: Any = None


class StatementOperations(BaseModel):
    """``POST /{id}/statements`` (UDR-0182 D9): ordered operations against a revision."""

    revision: str = Field(min_length=1)
    operations: list[Any] = Field(default_factory=list)
    # "rebase": a request made against an older revision is applied to the current
    # file when it still fits (UDR-0183 D5); "refuse" (default) keeps 409 stale_revision.
    on_stale: Literal["refuse", "rebase"] = "refuse"
    # Blank-node labels this client created; renamed when the file uses them (D6).
    fresh_blank_nodes: list[str] = Field(default_factory=list)


class ProjectionSave(BaseModel):
    """The CTR-0169 v2 / v3 projection as saved by the editor (full state, UDR-0180 D8)."""

    # Unknown keys are kept so a v1-shaped body (entities / relationships /
    # extra_turtle) can be REFUSED instead of silently saving an empty ontology.
    model_config = ConfigDict(extra="allow")

    document: dict[str, Any] = Field(default_factory=dict)
    resources: list[dict[str, Any]] = Field(default_factory=list)
    # The revision the editor loaded; a mismatch refuses the save (409 stale_revision).
    revision: str | None = None
    # 3 = the client knows statement graphs (``g``); required on a dataset (UDR-0182 D2).
    projection_version: int | None = None


_STALE_REVISION = {
    "error": "stale_revision",
    "message": "The ontology was changed after you opened it. Reload it to see the latest version.",
}


def _check_revision(ontology_id: str, revision: str | None) -> None:
    """409 ``stale_revision`` when ``revision`` is not the stored file's (UDR-0180 D8).

    Callers hold ``store.write_lock(ontology_id)`` up to their write (UDR-0183 D4).
    """
    if revision is None:
        return
    current = store.revision_of(store.read_ontology_bytes(ontology_id) or b"")
    if revision != current:
        raise HTTPException(status_code=409, detail=_STALE_REVISION)


def _scope_or_422(value: Any) -> Any:
    try:
        return store.parse_scope(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": "invalid_scope", "message": str(exc)}) from exc


def _save(ontology_id: str, text: str, dataset: bool, entry: dict[str, Any] | None = None) -> str | None:
    """Write through the store (backup + atomic; ``.ttl`` / ``.trig`` switch, UDR-0182 D3).

    ``entry`` adds to the history log line (kind, counts; UDR-0184 D1).
    """
    try:
        return store.save_ontology_text(ontology_id, text, dataset=dataset, history=entry)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        logger.warning("ontology save failed: %s", ontology_id, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not save the ontology: {exc}") from exc


@router.get("/catalog", dependencies=[Depends(verify_api_key)])
async def list_catalog() -> dict:
    """List the ontology catalog. 404 when the feature is disabled (SPA probe).

    Carries ``demo_mode`` so the manager renders its read-only notice from the
    SAME payload it already fetches (UDR-0122 D4).
    """
    _require_enabled()
    return {"ontologies": store.read_catalog(), "demo_mode": bool(settings.demo_mode)}


@router.post("/catalog", dependencies=[Depends(verify_api_key)])
async def create_ontology(body: OntologyCreate) -> dict:
    """Create a new, empty ontology (consumes CTR-0083)."""
    _require_enabled()
    _guard_demo()
    entry = store.create_ontology(
        body.name, body.description, initial_turtle=lambda entry_id: new_document_turtle(base_iri_for(entry_id))
    )
    return {**entry, "base_iri": base_iri_for(entry["id"])}


@router.patch("/catalog/{ontology_id}", dependencies=[Depends(verify_api_key)])
async def rename_ontology(ontology_id: str, body: OntologyRename) -> dict:
    """Rename a catalog item's name and/or description (consumes CTR-0083).

    Additive catalog CRUD gap fill (PRP-0116): the id and the graph projection
    are UNCHANGED -- only the catalog entry's name/description move. It is a
    mutating endpoint and consumes CTR-0083 (invariant 7).
    """
    _require_enabled()
    _guard_demo()
    if body.name is None and body.description is None:
        raise HTTPException(status_code=422, detail="Provide name and/or description")
    _entry_or_404(ontology_id)
    entry = store.rename_ontology(ontology_id, name=body.name, description=body.description)
    if entry is None:  # raced with a delete
        raise HTTPException(status_code=404, detail={"error": "ontology_not_found"})
    return {**entry, "base_iri": base_iri_for(entry["id"])}


@router.delete("/catalog/{ontology_id}", dependencies=[Depends(verify_api_key)])
async def delete_ontology(ontology_id: str) -> dict:
    """Delete an ontology: backup-then-remove + catalog entry removal (UDR-0084 D10)."""
    _require_enabled()
    _guard_demo()
    _entry_or_404(ontology_id)
    await run_in_threadpool(store.delete_ontology, ontology_id)
    return {"deleted": True, "id": ontology_id}


@router.post("/import", dependencies=[Depends(verify_api_key)])
async def import_ontology(
    file: UploadFile = File(...),
    name: str = Form(default=""),
    description: str = Form(default=""),
) -> dict:
    """Import an RDF file in any supported format (validate-then-commit).

    The upload is fully parsed with pyoxigraph BEFORE anything is written, then
    stored as canonical Turtle -- or TriG when it holds a named graph -- under a NEW
    catalog id (UDR-0084 D10 / IMPORT-1, UDR-0182 D3 / D6), keeping the source's
    prefixes / base / VERSION (UDR-0180 D6). Parsing goes through
    ``pyoxigraph.parse`` (never a Store), so lexical forms stay exact. A remote
    JSON-LD ``@context`` is refused (422 ``remote_context_unsupported``), never fetched.
    """
    import pyoxigraph as ox

    _require_enabled()
    _guard_demo()
    data = await file.read()
    if len(data) > settings.ontology_max_file_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File is {len(data)} bytes but the limit is {settings.ontology_max_file_bytes} bytes",
        )

    candidates = formats.import_formats(file.filename or "")
    counted: list[int] = []
    datasets: list[bool] = []

    def convert(entry_id: str) -> str:
        # Relative IRIs of a source without a declared base resolve against the new
        # ontology's own base (UDR-0181 D6). Runs before anything is written: a
        # failure here leaves no file and no catalog entry.
        errors: list[str] = []
        for fmt in candidates:
            try:
                text, quad_count, dataset = import_document(
                    data, fmt, rdfxml=fmt == ox.RdfFormat.RDF_XML, base_iri=base_iri_for(entry_id)
                )
            except RemoteContextError:
                raise
            except Exception as exc:  # syntax error for this format -- try the next
                errors.append(f"{fmt}: {exc}")
                continue
            counted.append(quad_count)
            datasets.append(dataset)
            return text
        raise _ImportRejected(f"Not a valid RDF document. {' / '.join(errors[:1])}")

    display_name = name.strip() or (file.filename or "").rsplit(".", 1)[0] or "Imported ontology"
    try:
        entry = await run_in_threadpool(
            store.create_ontology,
            display_name,
            description,
            initial_turtle=convert,
            dataset=lambda: datasets[0],
            history_kind="import",
        )
    except _ImportRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RemoteContextError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "remote_context_unsupported", "url": exc.url, "message": str(exc)},
        ) from exc
    logger.info("ontology imported: %s (%d statements)", entry["id"], counted[0])
    return {**entry, "base_iri": base_iri_for(entry["id"]), "triple_count": counted[0]}


# ---- History and trash (CTR-0171 v7, UDR-0184) ---------------------------------


class RestoreRequest(BaseModel):
    # The revision the editor shows; a different current file refuses the restore (409).
    revision: str = Field(min_length=1)


def _version_or_404(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except history.VersionNotFound as exc:
        raise HTTPException(status_code=404, detail={"error": "version_not_found"}) from exc


def _parse_backup(data: bytes) -> tuple[str, bool]:
    """A version's text and whether it is a dataset; 422 when it is not valid RDF."""
    try:
        quads, _ = read_dataset(data) if data.strip() else ([], {})
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail={"error": "invalid_version", "message": f"Not a valid RDF document: {exc}"}
        ) from exc
    return data.decode("utf-8"), any(is_named(q) for q in quads)


@router.get("/trash", dependencies=[Depends(verify_api_key)])
async def list_trash() -> dict:
    """Deleted ontologies that can be restored (UDR-0184 D6); expired ones are removed."""
    _require_enabled()
    return {"deleted": await run_in_threadpool(history.trash)}


@router.post("/trash/{ontology_id}/restore", dependencies=[Depends(verify_api_key)])
async def restore_deleted(ontology_id: str) -> dict:
    """Recreate a deleted ontology under its SAME id from its newest backup (D6)."""
    _require_enabled()
    _guard_demo()

    def work() -> dict[str, Any]:
        backups = history.list_backups(ontology_id)
        if not backups or store.get_entry(ontology_id) is not None:
            if store.get_entry(ontology_id) is not None:
                raise HTTPException(status_code=409, detail={"error": "ontology_exists"})
            raise HTTPException(status_code=404, detail={"error": "ontology_not_found"})
        newest = backups[0]
        text, dataset = _parse_backup(history.read_version(ontology_id, newest.name))
        try:
            return store.restore_deleted_ontology(ontology_id, text, dataset=dataset, restored_from=newest.name)
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail={"error": "ontology_exists"}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    entry = await run_in_threadpool(work)
    return {**entry, "base_iri": base_iri_for(ontology_id)}


@router.get("/{ontology_id}/history", dependencies=[Depends(verify_api_key)])
async def list_history(ontology_id: str) -> dict:
    """The versions of an ontology, newest first, labelled from the history log (D2)."""
    _require_enabled()
    _entry_or_404(ontology_id)
    rows = await run_in_threadpool(history.versions, ontology_id)
    return {
        "versions": rows,
        "retention": {
            "keep_recent": settings.ontology_history_keep_recent,
            "keep_days": settings.ontology_history_keep_days,
            "max_mb": settings.ontology_history_max_mb,
        },
    }


@router.get("/{ontology_id}/history/{version}/diff", dependencies=[Depends(verify_api_key)])
async def diff_version(
    ontology_id: str,
    version: str,
    against: str = "previous",
    offset: int = 0,
    limit: int = history.DIFF_DEFAULT_LIMIT,
) -> dict:
    """``added`` / ``removed`` statements of ``version`` against previous / current / a version (D4)."""
    _require_enabled()
    _entry_or_404(ontology_id)
    try:
        return await run_in_threadpool(
            _version_or_404, history.version_diff, ontology_id, version, against, offset=offset, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"error": "invalid_version", "message": str(exc)}) from exc


@router.get("/{ontology_id}/history/{version}/file", dependencies=[Depends(verify_api_key)])
async def download_version(ontology_id: str, version: str) -> Response:
    """One version as stored (Turtle or TriG)."""
    _require_enabled()
    entry = _entry_or_404(ontology_id)
    data = await run_in_threadpool(_version_or_404, history.read_version, ontology_id, version)
    is_trig = store.is_dataset(entry) if version == history.CURRENT else ".trig.bak-" in version
    fmt = formats.EXPORT_FORMATS["trig" if is_trig else "turtle"]
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in entry["name"]).strip() or ontology_id
    stamp = version.rsplit("-", 1)[-1] if version != history.CURRENT else "current"
    return Response(
        content=data,
        media_type=f"{fmt.media_type}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-{stamp}{fmt.extension}"'},
    )


@router.post("/{ontology_id}/history/{version}/restore", dependencies=[Depends(verify_api_key)])
async def restore_version(ontology_id: str, version: str, body: RestoreRequest) -> dict:
    """Restore a whole version through the normal save path (UDR-0184 D5).

    Revision check (409 ``stale_revision``), parse check (422), backup of the current
    file, size cap, ``.ttl`` / ``.trig`` by content, cache dropped, history ``restore``
    line. The replaced state becomes the newest backup, so a restore can be undone.
    """
    _require_enabled()
    _guard_demo()
    _entry_or_404(ontology_id)
    if version == history.CURRENT:
        raise HTTPException(status_code=422, detail={"error": "invalid_version", "message": "Pick an earlier version."})

    def work() -> tuple[str, str | None]:
        with store.write_lock(ontology_id):
            _check_revision(ontology_id, body.revision)
            text, dataset = _parse_backup(_version_or_404(history.read_version, ontology_id, version))
            backup = _save(ontology_id, text, dataset, {"kind": "restore", "restored_from": version})
        return text, backup

    text, backup = await run_in_threadpool(work)
    logger.info("ontology restored: %s from %s (backup=%s)", ontology_id, version, backup)
    return {
        "saved": True,
        "id": ontology_id,
        "backup": backup,
        "revision": store.revision_of(text.encode("utf-8")),
    }


def _render_export(data: bytes, fmt: Any) -> tuple[bytes, dict[str, Any]]:
    """Parse, check, write and verify one export (a worker thread; UDR-0183 D4)."""
    quads, document = read_dataset(data)
    return formats.serialize(quads, document, fmt), document


@router.get("/{ontology_id}/export", dependencies=[Depends(verify_api_key)])
async def export_ontology(ontology_id: str, format: str | None = None) -> Response:
    """Download the ontology (UDR-0182 D7).

    Without ``format`` the stored file as is: Turtle for a graph, TriG for a dataset.
    With ``format`` (turtle / trig / rdfxml / jsonld / ntriples / nquads) the content is
    checked against what the format can carry -- refused with 422
    ``export_unsupported_content`` (reasons + examples) rather than dropped -- then
    written, parsed back and compared (422 ``export_verification_failed`` on a
    mismatch). ``X-Ontology-Export-Notes`` names what the format drops that is not RDF
    content (prefixes, base).
    """
    _require_enabled()
    entry = _entry_or_404(ontology_id)
    data = await run_in_threadpool(store.read_ontology_bytes, ontology_id) or b""
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in entry["name"]).strip() or ontology_id
    if format is None:
        fmt = formats.EXPORT_FORMATS["trig" if store.is_dataset(entry) else "turtle"]
        return Response(
            content=data,
            media_type=f"{fmt.media_type}; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}{fmt.extension}"'},
        )
    fmt = formats.EXPORT_FORMATS.get(format)
    if fmt is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unknown_format",
                "message": f"format must be one of {', '.join(formats.EXPORT_FORMATS)}",
            },
        )
    try:
        content, document = await run_in_threadpool(_render_export, data, fmt)
    except formats.ExportRefused as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "export_unsupported_content",
                "format": fmt.name,
                "reasons": exc.reasons,
                "message": str(exc),
            },
        ) from exc
    except formats.ExportVerificationFailed as exc:
        logger.warning("ontology export verification failed: %s (%s): %s", ontology_id, fmt.name, exc)
        raise HTTPException(
            status_code=422,
            detail={"error": "export_verification_failed", "format": fmt.name, "message": str(exc)},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    headers = {"Content-Disposition": f'attachment; filename="{safe_name}{fmt.extension}"'}
    export_notes = formats.notes(fmt, document)
    if export_notes:
        headers["X-Ontology-Export-Notes"] = " ".join(export_notes)
    return Response(content=content, media_type=f"{fmt.media_type}; charset=utf-8", headers=headers)


def _run_sparql(ontology_id: str, scope: Any, sparql: str) -> dict[str, Any]:
    """Load (from the cache when possible) and query, in a worker thread (UDR-0183 D1 / D4)."""
    return store.execute_query(store.load_store(ontology_id, scope), sparql)


@router.post("/{ontology_id}/query", dependencies=[Depends(verify_api_key)])
async def run_query(ontology_id: str, body: QueryRequest) -> dict:
    """Run a READ-ONLY SPARQL query (SELECT / CONSTRUCT / ASK / DESCRIBE)."""
    _require_enabled()
    _entry_or_404(ontology_id)
    scope = _scope_or_422(body.scope)
    try:
        nl.ensure_read_only(body.sparql)
        result = await run_in_threadpool(_run_sparql, ontology_id, scope, body.sparql)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result


@router.post("/{ontology_id}/nl-query", dependencies=[Depends(verify_api_key)])
async def run_nl_query(ontology_id: str, body: NlQueryRequest) -> dict:
    """Natural language -> SPARQL (via the CTR-0102 chokepoint) -> execute.

    The generated SPARQL is returned to the caller (the UI places it into the
    SPARQL editor for refinement) and validated read-only BEFORE execution
    (UDR-0084 D8).
    """
    _require_enabled()
    _entry_or_404(ontology_id)
    graph = await run_in_threadpool(store.load_store, ontology_id, _scope_or_422(body.scope))
    try:
        sparql = await nl.generate_sparql(body.question, graph)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"NL to SPARQL failed: {exc}") from exc
    except Exception as exc:  # provider/network failure -- keep the message short
        logger.warning("nl-query completion failed for %s", ontology_id, exc_info=True)
        raise HTTPException(status_code=502, detail="The SPARQL generation model call failed.") from exc
    try:
        result = await run_in_threadpool(store.execute_query, graph, sparql)
    except ValueError as exc:
        # Surface the generated (broken) query so the user can fix it in the editor.
        return {"sparql": sparql, "kind": "error", "error": str(exc)}
    return {"sparql": sparql, **result}


@router.get("/{ontology_id}", dependencies=[Depends(verify_api_key)])
async def get_ontology(ontology_id: str) -> dict:
    """The CTR-0169 v3 statement-complete projection plus the file ``revision``."""
    _require_enabled()
    entry = _entry_or_404(ontology_id)
    data = await run_in_threadpool(store.read_ontology_bytes, ontology_id) or b""
    try:
        projection = await run_in_threadpool(turtle_to_projection, data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**entry, "base_iri": base_iri_for(ontology_id), "revision": store.revision_of(data), **projection}


@router.put("/{ontology_id}", dependencies=[Depends(verify_api_key)])
async def save_ontology(ontology_id: str, body: ProjectionSave) -> dict:
    """Save the projection: validate -> Turtle -> revision check -> backup -> atomic replace.

    The conversion runs in a worker thread; the revision check and the write run
    under the ontology's write lock, so no other save can interleave (UDR-0183 D4).
    """
    _require_enabled()
    _guard_demo()
    _entry_or_404(ontology_id)
    legacy = sorted(set(body.model_extra or {}) & _LEGACY_PROJECTION_KEYS)
    if legacy:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "legacy_projection",
                "message": f"The v1 projection fields {', '.join(legacy)} are no longer accepted; "
                "send {document, resources, revision} (CTR-0169 v2 / v3).",
            },
        )
    try:
        text, dataset = await run_in_threadpool(
            projection_to_text, {"document": body.document, "resources": body.resources}
        )
    except ProjectionError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_projection", "pointer": exc.pointer, "message": exc.message},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    def write() -> str | None:
        with store.write_lock(ontology_id):
            entry = _entry_or_404(ontology_id)
            if store.is_dataset(entry) and body.projection_version != PROJECTION_VERSION:
                # A client that does not know statement graphs would move every
                # named-graph statement into the default graph (UDR-0182 D2).
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "graph_unaware_client",
                        "message": "This ontology has named graphs. Save with projection_version 3 "
                        "(statements carry their graph as g), or reload the editor.",
                    },
                )
            _check_revision(ontology_id, body.revision)
            return _save(ontology_id, text, dataset)

    backup = await run_in_threadpool(write)
    logger.info("ontology saved: %s (backup=%s)", ontology_id, backup)
    return {
        "saved": True,
        "id": ontology_id,
        "backup": backup,
        "revision": store.revision_of(text.encode("utf-8")),
    }


def _apply_and_save(ontology_id: str, body: StatementOperations) -> tuple[str, str | None, dict[str, Any]]:
    """Read -> (rebase) apply -> write under the write lock, in a worker thread (UDR-0183 D4-D6)."""
    with store.write_lock(ontology_id):
        data = store.read_ontology_bytes(ontology_id)
        if data is None:
            raise HTTPException(status_code=404, detail={"error": "ontology_not_found"})
        current = store.revision_of(data)
        stale = body.revision != current
        if stale and body.on_stale != "rebase":
            raise HTTPException(status_code=409, detail=_STALE_REVISION)
        quads, document = read_dataset(data)
        before = set(quads)
        try:
            quads, report = apply_operations(
                quads,
                body.operations,
                ontology_base=base_iri_for(ontology_id),
                rebase=stale,
                fresh_blank_nodes=body.fresh_blank_nodes,
            )
        except StaleConflict as exc:
            exc.revision = current
            raise
        dataset = any(is_named(q) for q in quads)
        text = write_document(quads, document, trig=dataset)
        after = set(quads)
        backup = _save(
            ontology_id,
            text,
            dataset,
            {"kind": "statements", "added": len(after - before), "removed": len(before - after)},
        )
    report["rebased"] = stale
    return text, backup, report


@router.post("/{ontology_id}/statements", dependencies=[Depends(verify_api_key)])
async def apply_statement_operations(ontology_id: str, body: StatementOperations) -> dict:
    """Apply explicit statement operations atomically (UDR-0182 D9).

    ``add`` / ``remove`` / ``replace`` (reifiers follow by default) / ``annotate``
    (a reifier with an IRI, minted as ``<base>r-<8 hex>`` when absent). Same guards
    and lossless writer as ``PUT``: DEMO_MODE 409, a required revision (409
    ``stale_revision``), 422 with a pointer for a malformed operation, 422
    ``statement_not_found`` for a statement the file does not hold; nothing is
    written unless every operation applies.

    ``on_stale: "rebase"`` (UDR-0183 D5) applies a request made against an older
    revision to the current file: adding a present statement is skipped, touching a
    statement that is gone is 409 ``stale_conflict`` (listed, nothing written), and
    layout positions are last-writer-wins. ``fresh_blank_nodes`` are renamed when the
    file uses them (D6). The whole read -> apply -> write runs in a worker thread
    under the ontology's write lock (D4).
    """
    _require_enabled()
    _guard_demo()
    _entry_or_404(ontology_id)
    try:
        text, backup, report = await run_in_threadpool(_apply_and_save, ontology_id, body)
    except StaleConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "stale_conflict",
                "revision": exc.revision,
                "conflicts": exc.conflicts,
                "count": exc.count,
                "message": (
                    f"{exc.count} change(s) touch statements that were removed or changed "
                    "after you opened the ontology. Nothing was saved."
                ),
            },
        ) from exc
    except StatementNotFound as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "statement_not_found", "pointer": exc.pointer, "message": str(exc)},
        ) from exc
    except ProjectionError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_operation", "pointer": exc.pointer, "message": exc.message},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info(
        "ontology statements applied: %s (%d ops, %d skipped, rebased=%s, backup=%s)",
        ontology_id,
        report["applied"],
        report["skipped"],
        report["rebased"],
        backup,
    )
    return {
        "saved": True,
        "id": ontology_id,
        "backup": backup,
        "revision": store.revision_of(text.encode("utf-8")),
        **report,
    }


__all__ = ["router"]
