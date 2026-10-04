"""Ontology Management REST API (CTR-0171, PRP-0105, UDR-0084).

    GET    /api/ontology/catalog            -- list the catalog (also the SPA probe)
    POST   /api/ontology/catalog            -- create an ontology (name + description)
    PATCH  /api/ontology/catalog/{id}       -- rename (name and/or description)
    DELETE /api/ontology/catalog/{id}       -- delete (backup-then-remove)
    POST   /api/ontology/import             -- import a Turtle / RDF-XML file
    GET    /api/ontology/{id}               -- the JSON graph projection (CTR-0169)
    PUT    /api/ontology/{id}               -- save the projection (backup + atomic)
    GET    /api/ontology/{id}/export        -- download the canonical Turtle
    POST   /api/ontology/{id}/query         -- read-only SPARQL (SELECT/CONSTRUCT/ASK)
    POST   /api/ontology/{id}/nl-query      -- natural language -> SPARQL -> execute

Every endpoint depends on ``verify_api_key`` (CTR-0083; loopback bypass) --
the mutations and the query POSTs are gated per invariant 7, the GETs follow
the read convention. The whole surface returns 404 unless ONTOLOGY_ENABLED so
the SPA can gate its launcher icon by probing the catalog (UDR-0084 D12).

Under DEMO_MODE the five WRITE endpoints (POST/PATCH/DELETE /catalog, POST /import,
PUT /{id}) refuse with 409 ``demo_mode`` and the manager renders read-only
(PRP-0139 / UDR-0122). Reads -- including the SPARQL query lanes, which cannot
mutate -- are deliberately untouched.

The frontend never parses RDF: GET/PUT carry the CTR-0169 v2 statement-complete
projection and this module delegates the codec to ``app.ontology.vocabulary``
(UDR-0084 D6, UDR-0180 D2). Import is validate-then-commit (full pyoxigraph parse +
the ONTOLOGY_MAX_FILE_BYTES cap) and stores canonical Turtle WITH the source's
prefixes / base / VERSION (UDR-0084 D10, UDR-0180 D6). GET returns a ``revision``
(SHA-256 of the stored file); a PUT carrying a different one is refused with 409
``stale_revision`` before anything is written (UDR-0180 D8).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.auth import verify_api_key
from app.core.config import settings
from app.ontology import nl, store
from app.ontology.vocabulary import (
    ProjectionError,
    base_iri_for,
    import_to_turtle,
    new_document_turtle,
    projection_to_turtle,
    turtle_to_projection,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ontology", tags=["Ontology"])

# CTR-0169 v1 body keys: refused by PUT (422 legacy_projection) rather than ignored.
_LEGACY_PROJECTION_KEYS = frozenset({"entities", "relationships", "extra_turtle"})


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


class NlQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)


class ProjectionSave(BaseModel):
    """The CTR-0169 v2 projection as saved by the editor (full state, UDR-0180 D8)."""

    # Unknown keys are kept so a v1-shaped body (entities / relationships /
    # extra_turtle) can be REFUSED instead of silently saving an empty ontology.
    model_config = ConfigDict(extra="allow")

    document: dict[str, Any] = Field(default_factory=dict)
    resources: list[dict[str, Any]] = Field(default_factory=list)
    # The revision the editor loaded; a mismatch refuses the save (409 stale_revision).
    revision: str | None = None


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
    store.delete_ontology(ontology_id)
    return {"deleted": True, "id": ontology_id}


@router.post("/import", dependencies=[Depends(verify_api_key)])
async def import_ontology(
    file: UploadFile = File(...),
    name: str = Form(default=""),
    description: str = Form(default=""),
) -> dict:
    """Import a Turtle (.ttl) or RDF/XML (.rdf/.owl) file (validate-then-commit).

    The upload is fully parsed with pyoxigraph BEFORE anything is written, then
    stored as canonical Turtle under a NEW catalog id (UDR-0084 D10 / IMPORT-1),
    keeping the source's prefixes / base / VERSION (UDR-0180 D6). Parsing goes
    through ``pyoxigraph.parse`` (never a Store), so lexical forms stay exact.
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

    filename = (file.filename or "").lower()
    if filename.endswith((".rdf", ".owl", ".xml")):
        formats = [ox.RdfFormat.RDF_XML, ox.RdfFormat.TURTLE]
    else:
        formats = [ox.RdfFormat.TURTLE, ox.RdfFormat.RDF_XML]

    converted: tuple[str, int] | None = None
    errors: list[str] = []
    for fmt in formats:
        try:
            converted = import_to_turtle(data, fmt, rdfxml=fmt == ox.RdfFormat.RDF_XML)
            break
        except Exception as exc:  # syntax error for this format -- try the next
            errors.append(f"{fmt}: {exc}")
    if converted is None:
        raise HTTPException(status_code=422, detail=f"Not a valid RDF document. {' / '.join(errors[:1])}")

    turtle, triple_count = converted
    display_name = name.strip() or (file.filename or "").rsplit(".", 1)[0] or "Imported ontology"
    entry = store.create_ontology(display_name, description, initial_turtle=turtle)
    logger.info("ontology imported: %s (%d triples)", entry["id"], triple_count)
    return {**entry, "base_iri": base_iri_for(entry["id"]), "triple_count": triple_count}


@router.get("/{ontology_id}/export", dependencies=[Depends(verify_api_key)])
async def export_ontology(ontology_id: str) -> Response:
    """Download the ontology as canonical Turtle (Export emits Turtle; IMPORT-1)."""
    _require_enabled()
    entry = _entry_or_404(ontology_id)
    data = store.read_ontology_bytes(ontology_id) or b""
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in entry["name"]).strip() or ontology_id
    return Response(
        content=data,
        media_type="text/turtle; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.ttl"'},
    )


@router.post("/{ontology_id}/query", dependencies=[Depends(verify_api_key)])
async def run_query(ontology_id: str, body: QueryRequest) -> dict:
    """Run a READ-ONLY SPARQL query (SELECT / CONSTRUCT / ASK / DESCRIBE)."""
    _require_enabled()
    _entry_or_404(ontology_id)
    try:
        nl.ensure_read_only(body.sparql)
        result = store.execute_query(store.load_store(ontology_id), body.sparql)
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
    graph = store.load_store(ontology_id)
    try:
        sparql = await nl.generate_sparql(body.question, graph)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"NL to SPARQL failed: {exc}") from exc
    except Exception as exc:  # provider/network failure -- keep the message short
        logger.warning("nl-query completion failed for %s", ontology_id, exc_info=True)
        raise HTTPException(status_code=502, detail="The SPARQL generation model call failed.") from exc
    try:
        result = store.execute_query(graph, sparql)
    except ValueError as exc:
        # Surface the generated (broken) query so the user can fix it in the editor.
        return {"sparql": sparql, "kind": "error", "error": str(exc)}
    return {"sparql": sparql, **result}


@router.get("/{ontology_id}", dependencies=[Depends(verify_api_key)])
async def get_ontology(ontology_id: str) -> dict:
    """The CTR-0169 v2 statement-complete projection plus the file ``revision``."""
    _require_enabled()
    entry = _entry_or_404(ontology_id)
    data = store.read_ontology_bytes(ontology_id) or b""
    try:
        projection = turtle_to_projection(data)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {**entry, "base_iri": base_iri_for(ontology_id), "revision": store.revision_of(data), **projection}


@router.put("/{ontology_id}", dependencies=[Depends(verify_api_key)])
async def save_ontology(ontology_id: str, body: ProjectionSave) -> dict:
    """Save the projection: revision check -> validate -> Turtle -> backup -> atomic replace.

    The check and the write run without an ``await`` in between, so no other
    request can interleave on the event loop.
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
                "send {document, resources, revision} (CTR-0169 v2).",
            },
        )
    if body.revision is not None:
        current = store.revision_of(store.read_ontology_bytes(ontology_id) or b"")
        if body.revision != current:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "stale_revision",
                    "message": "The ontology was changed after you opened it. Reload it to see the latest version.",
                },
            )
    try:
        turtle = projection_to_turtle({"document": body.document, "resources": body.resources})
        backup = store.save_ontology_text(ontology_id, turtle)
    except ProjectionError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_projection", "pointer": exc.pointer, "message": exc.message},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        logger.warning("ontology save failed: %s", ontology_id, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not save the ontology: {exc}") from exc
    logger.info("ontology saved: %s (backup=%s)", ontology_id, backup)
    return {
        "saved": True,
        "id": ontology_id,
        "backup": backup,
        "revision": store.revision_of(turtle.encode("utf-8")),
    }


__all__ = ["router"]
