"""Import / export formats: the capability table, lossy-export refusal, verification (CTR-0171 v5, UDR-0182).

Import (D6): the upload's extension picks the parser, with a fixed fallback order.
JSON-LD never fetches a remote ``@context`` (``RemoteContextError`` -> 422).

Export (D7): every format DECLARES what it can carry. Before writing, the content is
checked against the declaration; what a format cannot carry is REFUSED with reason
codes, counts and examples (``ExportRefused``), never dropped. After writing, the
output is parsed back with the same format and compared with the stored dataset
(exact quads; blank nodes canonically relabelled when a writer renamed them). RDF/XML
is also run through a strict XML parser, because pyoxigraph's writer emits XML that
is not well-formed for a predicate with no splittable local name and its own parser
reads that back anyway. A mismatch raises ``ExportVerificationFailed``; the stored
file is never touched.

RDF/XML specifics, found by the W3C round-trip gate: a predicate, and a class a node
is typed with, become XML element names; each is checked by writing it once and
strict-parsing the result (``rdfxml_name``); a literal with
characters XML 1.0 cannot hold is refused (``xml_characters``); prefix names and
blank-node labels the XML parser would reject are narrowed / relabelled, since
neither is RDF content.

Prefixes and base are not RDF content: a format that has none (N-Triples, N-Quads,
JSON-LD in expanded form) states that in ``notes`` instead of refusing.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import re
from typing import Any

from app.ontology.vocabulary import RDF_TYPE, is_named, term_to_json, write_document

# How many example statements a refusal lists per reason.
EXAMPLES_PER_REASON = 5


@dataclass(frozen=True)
class ExportFormat:
    """One export format and what it can carry (UDR-0182 D7)."""

    name: str
    label: str
    media_type: str
    extension: str
    named_graphs: bool
    triple_terms: bool
    directional_literals: bool
    prefixes: bool
    base: bool

    def rdf_format(self) -> Any:
        import pyoxigraph as ox

        return {
            "turtle": ox.RdfFormat.TURTLE,
            "trig": ox.RdfFormat.TRIG,
            "rdfxml": ox.RdfFormat.RDF_XML,
            "jsonld": ox.RdfFormat.JSON_LD,
            "ntriples": ox.RdfFormat.N_TRIPLES,
            "nquads": ox.RdfFormat.N_QUADS,
        }[self.name]


EXPORT_FORMATS: dict[str, ExportFormat] = {
    f.name: f
    for f in (
        ExportFormat("turtle", "Turtle", "text/turtle", ".ttl", False, True, True, True, True),
        ExportFormat("trig", "TriG", "application/trig", ".trig", True, True, True, True, True),
        ExportFormat("rdfxml", "RDF/XML", "application/rdf+xml", ".rdf", False, True, True, True, False),
        ExportFormat("jsonld", "JSON-LD", "application/ld+json", ".jsonld", True, False, True, False, False),
        ExportFormat("ntriples", "N-Triples", "application/n-triples", ".nt", False, True, True, False, False),
        ExportFormat("nquads", "N-Quads", "application/n-quads", ".nq", True, True, True, False, False),
    )
}


def import_formats(filename: str) -> list[Any]:
    """The parsers to try for an upload, in order, from its extension (UDR-0182 D6)."""
    import pyoxigraph as ox

    f = ox.RdfFormat
    name = filename.lower()
    table: tuple[tuple[tuple[str, ...], list[Any]], ...] = (
        ((".ttl", ".turtle"), [f.TURTLE, f.RDF_XML]),
        ((".trig",), [f.TRIG]),
        ((".nt",), [f.N_TRIPLES, f.TURTLE]),
        ((".nq",), [f.N_QUADS, f.TRIG]),
        ((".rdf", ".owl", ".xml"), [f.RDF_XML, f.TURTLE]),
        ((".jsonld", ".json"), [f.JSON_LD]),
    )
    for suffixes, formats in table:
        if name.endswith(suffixes):
            return formats
    return [f.TRIG, f.RDF_XML]


IMPORT_EXTENSIONS = (".ttl", ".turtle", ".trig", ".nt", ".nq", ".rdf", ".owl", ".xml", ".jsonld", ".json")


class ExportRefused(ValueError):
    """The format cannot carry the content (422 ``export_unsupported_content``)."""

    def __init__(self, fmt: ExportFormat, reasons: list[dict[str, Any]]) -> None:
        names = ", ".join(f"{r['count']} {r['code'].replace('_', ' ')}" for r in reasons)
        super().__init__(f"{fmt.label} cannot carry this ontology without loss ({names}).")
        self.format = fmt
        self.reasons = reasons


class ExportVerificationFailed(ValueError):
    """The written file does not read back as the stored dataset (422 ``export_verification_failed``)."""


# Characters XML 1.0 cannot carry at all, not even as character references.
_XML_FORBIDDEN_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")
# Names passed to the RDF/XML writer (prefixes, blank-node ids): plain ASCII NCNames,
# which every XML parser accepts. Neither is RDF content, so narrowing them loses nothing.
_ASCII_NCNAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")


@lru_cache(maxsize=4096)
def rdfxml_name_ok(iri: str) -> bool:
    """Whether RDF/XML can write ``iri`` as an element name a strict XML parser accepts.

    Predicates and the classes nodes are typed with are written as element names.
    Exact rather than a name-grammar guess: one triple with ``iri`` as its predicate is
    written by the same writer and parsed by the same strict parser the verification
    uses (the writer splits a class IRI the same way).
    """
    import pyoxigraph as ox

    triple = ox.Triple(ox.NamedNode("urn:x-chatwalaau:s"), ox.NamedNode(iri), ox.NamedNode("urn:x-chatwalaau:o"))
    try:
        _strict_xml(ox.serialize([triple], format=ox.RdfFormat.RDF_XML))
    except (ExportVerificationFailed, ValueError):
        return False
    return True


def _xml_unsafe_literal(term: Any) -> bool:
    """A literal (also inside a triple term) whose text XML 1.0 cannot represent."""
    import pyoxigraph as ox

    if isinstance(term, ox.Literal):
        return bool(_XML_FORBIDDEN_RE.search(term.value))
    if isinstance(term, ox.Triple):
        return _xml_unsafe_literal(term.subject) or _xml_unsafe_literal(term.object)
    return False


def _blank_nodes(quad: Any) -> list[Any]:
    import pyoxigraph as ox

    out: list[Any] = []

    def visit(term: Any) -> None:
        if isinstance(term, ox.BlankNode):
            out.append(term)
        elif isinstance(term, ox.Triple):
            visit(term.subject)
            visit(term.object)

    visit(quad.subject)
    visit(quad.object)
    return out


def _rdfxml_blank_ids(quads: list[Any]) -> list[Any]:
    """``quads`` as triples with every blank-node label a valid ``rdf:nodeID`` (labels are not content)."""
    import pyoxigraph as ox

    used = {t.value for q in quads for t in _blank_nodes(q)}
    renamed: dict[str, Any] = {}

    def fix(term: Any) -> Any:
        if isinstance(term, ox.BlankNode):
            if _ASCII_NCNAME_RE.fullmatch(term.value):
                return term
            if term.value not in renamed:
                base = "b" + re.sub(r"[^A-Za-z0-9_.\-]", "_", term.value)
                candidate, n = base, 1
                while candidate in used:
                    n += 1
                    candidate = f"{base}_{n}"
                used.add(candidate)
                renamed[term.value] = ox.BlankNode(candidate)
            return renamed[term.value]
        if isinstance(term, ox.Triple):
            return ox.Triple(fix(term.subject), term.predicate, fix(term.object))
        return term

    return [ox.Triple(fix(q.subject), q.predicate, fix(q.object)) for q in quads]


def _has_triple_term(term: Any) -> bool:
    import pyoxigraph as ox

    return isinstance(term, ox.Triple)


def _example(quad: Any) -> dict[str, Any]:
    out = {"s": term_to_json(quad.subject), "p": quad.predicate.value, "o": term_to_json(quad.object)}
    if is_named(quad):
        out["g"] = term_to_json(quad.graph_name)
    return out


def unsupported_content(quads: list[Any], fmt: ExportFormat) -> list[dict[str, Any]]:
    """What ``fmt`` cannot carry, as reasons ``{code, count, examples}`` (empty = exportable)."""
    import pyoxigraph as ox

    buckets: dict[str, list[Any]] = {}
    for quad in quads:
        if not fmt.named_graphs and is_named(quad):
            buckets.setdefault("named_graphs", []).append(quad)
        if not fmt.triple_terms and (_has_triple_term(quad.object) or _has_triple_term(quad.subject)):
            buckets.setdefault("triple_terms", []).append(quad)
        if fmt.name == "rdfxml":
            typed_class = quad.predicate.value == RDF_TYPE and isinstance(quad.object, ox.NamedNode)
            if not rdfxml_name_ok(quad.predicate.value) or (typed_class and not rdfxml_name_ok(quad.object.value)):
                buckets.setdefault("rdfxml_name", []).append(quad)
            if _xml_unsafe_literal(quad.object):
                buckets.setdefault("xml_characters", []).append(quad)
    return [
        {"code": code, "count": len(items), "examples": [_example(q) for q in items[:EXAMPLES_PER_REASON]]}
        for code, items in buckets.items()
    ]


def notes(fmt: ExportFormat, document: dict[str, Any]) -> list[str]:
    """What the format drops that is not RDF content (prefixes, base)."""
    out: list[str] = []
    if not fmt.prefixes and document.get("prefixes"):
        out.append(f"{fmt.label} has no prefix declarations; IRIs are written in full.")
    if not fmt.base and document.get("base"):
        out.append(f"{fmt.label} keeps no base IRI; relative IRIs were already resolved.")
    if document.get("version") and fmt.name not in ("turtle", "trig"):
        out.append(f'{fmt.label} has no VERSION directive; the "{document["version"]}" declaration is not written.')
    return out


def _canonical(quads: list[Any]) -> set[str]:
    import pyoxigraph as ox

    dataset = ox.Dataset(quads)
    dataset.canonicalize(ox.CanonicalizationAlgorithm.UNSTABLE)
    return {str(q) for q in dataset}


def _strict_xml(data: bytes) -> None:
    """Parse ``data`` with a strict XML parser; raise on malformed XML.

    Namespace processing is on (RDF/XML needs namespace-well-formed XML: a QName such
    as ``oxprefix:`` with an empty local part is an error) and external entities are off.
    """
    import io
    import xml.sax
    from xml.sax.handler import ContentHandler, feature_external_ges, feature_external_pes, feature_namespaces
    from xml.sax.xmlreader import InputSource

    parser = xml.sax.make_parser()
    parser.setFeature(feature_namespaces, True)
    parser.setFeature(feature_external_ges, False)
    parser.setFeature(feature_external_pes, False)
    parser.setContentHandler(ContentHandler())
    source = InputSource()
    source.setByteStream(io.BytesIO(data))
    try:
        parser.parse(source)
    except xml.sax.SAXException as exc:
        raise ExportVerificationFailed(f"The RDF/XML output is not well-formed XML: {exc}") from exc


def serialize(quads: list[Any], document: dict[str, Any], fmt: ExportFormat) -> bytes:
    """Write ``quads`` in ``fmt`` (refusing what it cannot carry) and verify the result."""
    import pyoxigraph as ox

    reasons = unsupported_content(quads, fmt)
    if reasons:
        raise ExportRefused(fmt, reasons)
    prefixes = {p["prefix"]: p["iri"] for p in document.get("prefixes") or []}
    if fmt.name in ("turtle", "trig"):
        data = write_document(quads, document, trig=fmt.name == "trig").encode("utf-8")
    elif fmt.named_graphs:
        data = ox.serialize(quads, format=fmt.rdf_format())
    elif fmt.name == "rdfxml":
        # Only plain ASCII prefix names become xmlns declarations (the default namespace
        # "" cannot hold rdf:Description); blank-node labels become valid rdf:nodeIDs.
        named = {k: v for k, v in prefixes.items() if k and _ASCII_NCNAME_RE.fullmatch(k)}
        data = ox.serialize(_rdfxml_blank_ids(quads), format=fmt.rdf_format(), prefixes=named)
    else:
        data = ox.serialize([q.triple for q in quads], format=fmt.rdf_format())
    verify(quads, data, fmt)
    return data


def verify(quads: list[Any], data: bytes, fmt: ExportFormat) -> None:
    """Parse ``data`` back and compare it with ``quads`` (D7); strict XML for RDF/XML."""
    import pyoxigraph as ox

    if fmt.name == "rdfxml":
        _strict_xml(data)
    try:
        back = list(ox.parse(data, format=fmt.rdf_format()))
    except (SyntaxError, ValueError) as exc:
        raise ExportVerificationFailed(f"The {fmt.label} output does not parse back: {exc}") from exc
    expected = {str(q) for q in quads}
    got = {str(q) for q in back}
    if got == expected:
        return
    if _canonical(back) != _canonical(list(quads)):
        missing = len(expected - got)
        extra = len(got - expected)
        raise ExportVerificationFailed(
            f"The {fmt.label} output does not read back as the stored ontology "
            f"({missing} statement(s) missing, {extra} unexpected)."
        )


__all__ = [
    "EXPORT_FORMATS",
    "IMPORT_EXTENSIONS",
    "ExportFormat",
    "ExportRefused",
    "ExportVerificationFailed",
    "import_formats",
    "notes",
    "rdfxml_name_ok",
    "serialize",
    "unsupported_content",
    "verify",
]
