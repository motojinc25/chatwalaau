"""Ontology meta-vocabulary and Turtle codec (CTR-0169, PRP-0105, UDR-0084 D4/D5/D6).

The canonical mapping between the editor's Entity/Relationship model and RDF,
under the fixed application namespace ``cw:`` (https://chatwalaau.com/ontology#):

- Entity          -> ``owl:Class``            (+ rdfs:label, rdfs:comment,
                                                 cw:emoji, cw:x, cw:y, cw:color)
- Entity Property -> ``owl:DatatypeProperty`` (+ rdfs:domain = the class,
                                                 rdfs:range = an XSD type,
                                                 optional cw:isKey = key attribute)
- Relationship    -> ``owl:ObjectProperty``   (+ rdfs:domain = source,
                                                 rdfs:range = target -- the single
                                                 direction; ONE property IRI per edge)
                     + ``cw:cardinality``     ("one-to-one" | "one-to-many" |
                                                "many-to-one" | "many-to-many")

Visual metadata (emoji / canvas x,y / color) is embedded as ``cw:`` annotation
triples IN THE SAME Turtle file (UDR-0084 D5) so one file is self-contained and
round-trips through export/import. Triples the projection does not consume
(e.g. RDF 1.2 triple terms, foreign annotations, instance data) are PRESERVED
verbatim: they ride the projection as the opaque ``extra_turtle`` field and are
re-emitted on save, so an editor round-trip never silently drops them.

This module is the ONE place that owns the Turtle <-> projection codec
(UDR-0084 D6); the frontend never parses RDF.
"""

from __future__ import annotations

from typing import Any

# ---- Namespaces (fixed; UDR-0084 D4) --------------------------------------

CW = "https://chatwalaau.com/ontology#"
OWL = "http://www.w3.org/2002/07/owl#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
XSD = "http://www.w3.org/2001/XMLSchema#"

RDF_TYPE = f"{RDF}type"
OWL_CLASS = f"{OWL}Class"
OWL_OBJECT_PROPERTY = f"{OWL}ObjectProperty"
OWL_DATATYPE_PROPERTY = f"{OWL}DatatypeProperty"
RDFS_LABEL = f"{RDFS}label"
RDFS_COMMENT = f"{RDFS}comment"
RDFS_DOMAIN = f"{RDFS}domain"
RDFS_RANGE = f"{RDFS}range"
CW_CARDINALITY = f"{CW}cardinality"
CW_EMOJI = f"{CW}emoji"
CW_X = f"{CW}x"
CW_Y = f"{CW}y"
CW_COLOR = f"{CW}color"
# Marks a datatype property as a KEY attribute of its entity (v0.99.1 amendment).
CW_IS_KEY = f"{CW}isKey"

XSD_STRING = f"{XSD}string"
XSD_DECIMAL = f"{XSD}decimal"
XSD_BOOLEAN = f"{XSD}boolean"

CARDINALITIES = ("one-to-one", "one-to-many", "many-to-one", "many-to-many")
DEFAULT_CARDINALITY = "one-to-many"

# Prefixes used by the canonical Turtle writer (readability only; not semantic).
TURTLE_PREFIXES = {"cw": CW, "owl": OWL, "rdf": RDF, "rdfs": RDFS, "xsd": XSD}
_PREFIXES = TURTLE_PREFIXES


def base_iri_for(ontology_id: str) -> str:
    """The ontology's own namespace for NEW terms (PRP-0105 IMPL-4)."""
    return f"https://chatwalaau.com/ontology/{ontology_id}#"


def local_name(iri: str) -> str:
    """Human-readable fallback label: the IRI fragment / last path segment."""
    for sep in ("#", "/", ":"):
        if sep in iri:
            tail = iri.rsplit(sep, 1)[1]
            if tail:
                return tail
    return iri


# ---- Turtle -> projection ---------------------------------------------------


def turtle_to_projection(data: bytes) -> dict[str, Any]:
    """Decode a Turtle document into the JSON graph projection (CTR-0171 GET).

    Only triples the meta-vocabulary fully understands are lifted into
    ``entities`` / ``relationships``; everything else is preserved verbatim in
    ``extra_turtle`` (UDR-0084 D4/D5). Raises ``ValueError`` on a syntax error.
    """
    import pyoxigraph as ox

    store = ox.Store()
    try:
        store.load(data, format=ox.RdfFormat.TURTLE)
    except Exception as exc:  # pyoxigraph raises SyntaxError/ValueError subclasses
        raise ValueError(f"Turtle parse error: {exc}") from exc

    named = ox.NamedNode

    def objects(subject: Any, predicate: str) -> list[Any]:
        return [q.object for q in store.quads_for_pattern(subject, named(predicate), None)]

    def first_str(subject: Any, predicate: str) -> str:
        for obj in objects(subject, predicate):
            value = getattr(obj, "value", None)
            if value is not None:
                return str(value)
        return ""

    def first_float(subject: Any, predicate: str) -> float:
        for obj in objects(subject, predicate):
            try:
                return float(getattr(obj, "value", ""))
            except (TypeError, ValueError):
                continue
        return 0.0

    consumed: set[tuple[str, str, str]] = set()

    def consume(subject: Any, predicate: str) -> None:
        for quad in store.quads_for_pattern(subject, named(predicate), None):
            consumed.add((str(quad.subject), str(quad.predicate), str(quad.object)))

    def typed_subjects(type_iri: str) -> list[Any]:
        return [q.subject for q in store.quads_for_pattern(None, named(RDF_TYPE), named(type_iri))]

    entity_nodes = [s for s in typed_subjects(OWL_CLASS) if isinstance(s, named)]
    entity_iris = {s.value for s in entity_nodes}

    # Datatype properties grouped under their (single, entity) domain.
    properties_by_entity: dict[str, list[dict[str, Any]]] = {}
    for node in typed_subjects(OWL_DATATYPE_PROPERTY):
        if not isinstance(node, named):
            continue
        domain = first_str(node, RDFS_DOMAIN)
        if domain not in entity_iris:
            continue  # not fully mapped -> stays in extra_turtle
        prop = {
            "iri": node.value,
            "label": first_str(node, RDFS_LABEL) or local_name(node.value),
            "range": first_str(node, RDFS_RANGE) or XSD_STRING,
            "comment": first_str(node, RDFS_COMMENT),
            "is_key": first_str(node, CW_IS_KEY).strip().lower() in {"true", "1"},
        }
        properties_by_entity.setdefault(domain, []).append(prop)
        for predicate in (RDF_TYPE, RDFS_LABEL, RDFS_COMMENT, RDFS_DOMAIN, RDFS_RANGE, CW_IS_KEY):
            consume(node, predicate)

    entities: list[dict[str, Any]] = []
    for node in sorted(entity_nodes, key=lambda n: n.value):
        iri = node.value
        entities.append(
            {
                "iri": iri,
                "label": first_str(node, RDFS_LABEL) or local_name(iri),
                "comment": first_str(node, RDFS_COMMENT),
                "emoji": first_str(node, CW_EMOJI),
                "color": first_str(node, CW_COLOR),
                "x": first_float(node, CW_X),
                "y": first_float(node, CW_Y),
                "properties": sorted(properties_by_entity.get(iri, []), key=lambda p: p["iri"]),
            }
        )
        for predicate in (RDF_TYPE, RDFS_LABEL, RDFS_COMMENT, CW_EMOJI, CW_COLOR, CW_X, CW_Y):
            consume(node, predicate)

    relationships: list[dict[str, Any]] = []
    for node in sorted(
        (s for s in typed_subjects(OWL_OBJECT_PROPERTY) if isinstance(s, named)),
        key=lambda n: n.value,
    ):
        source = first_str(node, RDFS_DOMAIN)
        target = first_str(node, RDFS_RANGE)
        if source not in entity_iris or target not in entity_iris:
            continue  # not fully mapped -> stays in extra_turtle
        cardinality = first_str(node, CW_CARDINALITY)
        relationships.append(
            {
                "iri": node.value,
                "label": first_str(node, RDFS_LABEL) or local_name(node.value),
                "comment": first_str(node, RDFS_COMMENT),
                "source": source,
                "target": target,
                "cardinality": cardinality if cardinality in CARDINALITIES else DEFAULT_CARDINALITY,
            }
        )
        for predicate in (RDF_TYPE, RDFS_LABEL, RDFS_COMMENT, RDFS_DOMAIN, RDFS_RANGE, CW_CARDINALITY):
            consume(node, predicate)

    # Everything not consumed round-trips verbatim (RDF 1.2 triple terms,
    # foreign annotations, instance data, partially-mapped terms).
    extras = [
        ox.Triple(quad.subject, quad.predicate, quad.object)
        for quad in store
        if (str(quad.subject), str(quad.predicate), str(quad.object)) not in consumed
    ]
    extra_turtle = ""
    if extras:
        extra_turtle = ox.serialize(extras, format=ox.RdfFormat.TURTLE, prefixes=_PREFIXES).decode("utf-8")

    return {
        "entities": entities,
        "relationships": relationships,
        "extra_turtle": extra_turtle,
        "triple_count": len(store),
    }


# ---- projection -> Turtle ---------------------------------------------------


def projection_to_turtle(projection: dict[str, Any], *, base_iri: str = "") -> str:
    """Encode the JSON graph projection into canonical Turtle (CTR-0171 PUT).

    Validates the projection shape (IRIs present, cardinality in the enum) and
    re-emits ``extra_turtle`` verbatim so unknown triples survive an editor
    save. Raises ``ValueError`` on an invalid projection.
    """
    import pyoxigraph as ox

    named = ox.NamedNode
    literal = ox.Literal
    store = ox.Store()

    def add(subject: str, predicate: str, obj: Any) -> None:
        store.add(ox.Quad(named(subject), named(predicate), obj))

    def require_iri(value: Any, what: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{what} must carry a non-empty IRI")
        return value.strip()

    entity_iris: set[str] = set()
    for entity in projection.get("entities") or []:
        iri = require_iri(entity.get("iri"), "entity")
        entity_iris.add(iri)
        add(iri, RDF_TYPE, named(OWL_CLASS))
        add(iri, RDFS_LABEL, literal(str(entity.get("label") or local_name(iri))))
        if entity.get("comment"):
            add(iri, RDFS_COMMENT, literal(str(entity["comment"])))
        if entity.get("emoji"):
            add(iri, CW_EMOJI, literal(str(entity["emoji"])))
        if entity.get("color"):
            add(iri, CW_COLOR, literal(str(entity["color"])))
        for axis, predicate in (("x", CW_X), ("y", CW_Y)):
            value = entity.get(axis)
            if value is not None:
                try:
                    number = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"entity {iri}: {axis} must be a number") from exc
                add(iri, predicate, literal(f"{number:g}", datatype=named(XSD_DECIMAL)))
        for prop in entity.get("properties") or []:
            prop_iri = require_iri(prop.get("iri"), f"property of entity {iri}")
            add(prop_iri, RDF_TYPE, named(OWL_DATATYPE_PROPERTY))
            add(prop_iri, RDFS_LABEL, literal(str(prop.get("label") or local_name(prop_iri))))
            add(prop_iri, RDFS_DOMAIN, named(iri))
            add(prop_iri, RDFS_RANGE, named(str(prop.get("range") or XSD_STRING)))
            if prop.get("comment"):
                add(prop_iri, RDFS_COMMENT, literal(str(prop["comment"])))
            if prop.get("is_key"):
                add(prop_iri, CW_IS_KEY, literal("true", datatype=named(XSD_BOOLEAN)))

    for rel in projection.get("relationships") or []:
        iri = require_iri(rel.get("iri"), "relationship")
        source = require_iri(rel.get("source"), f"relationship {iri}: source")
        target = require_iri(rel.get("target"), f"relationship {iri}: target")
        if source not in entity_iris or target not in entity_iris:
            raise ValueError(f"relationship {iri}: source/target must reference entities in this ontology")
        cardinality = str(rel.get("cardinality") or DEFAULT_CARDINALITY)
        if cardinality not in CARDINALITIES:
            raise ValueError(f"relationship {iri}: cardinality must be one of {', '.join(CARDINALITIES)}")
        add(iri, RDF_TYPE, named(OWL_OBJECT_PROPERTY))
        add(iri, RDFS_LABEL, literal(str(rel.get("label") or local_name(iri))))
        add(iri, RDFS_DOMAIN, named(source))
        add(iri, RDFS_RANGE, named(target))
        add(iri, CW_CARDINALITY, literal(cardinality))
        if rel.get("comment"):
            add(iri, RDFS_COMMENT, literal(str(rel["comment"])))

    extra = (projection.get("extra_turtle") or "").strip()
    if extra:
        try:
            store.load(extra.encode("utf-8"), format=ox.RdfFormat.TURTLE)
        except Exception as exc:
            raise ValueError(f"extra_turtle parse error: {exc}") from exc

    prefixes = dict(_PREFIXES)
    if base_iri:
        prefixes[""] = base_iri
    return store.dump(format=ox.RdfFormat.TURTLE, from_graph=ox.DefaultGraph(), prefixes=prefixes).decode("utf-8")


__all__ = [
    "CARDINALITIES",
    "CW",
    "DEFAULT_CARDINALITY",
    "OWL",
    "RDFS",
    "XSD",
    "base_iri_for",
    "local_name",
    "projection_to_turtle",
    "turtle_to_projection",
]
