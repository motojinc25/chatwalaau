"""Ontology meta-vocabulary and the lossless Turtle codec (CTR-0169 v2, PRP-0198, UDR-0180).

The ``cw:`` application namespace (https://chatwalaau.com/ontology#) and the
OWL-minimal meta-vocabulary the canvas draws (UDR-0084 D4); the RDFS forms
``rdfs:Class`` / ``rdf:Property`` (kind by range) are drawn too (UDR-0185 D1):

- Entity          -> ``owl:Class``            (+ rdfs:label, rdfs:comment,
                                                 cw:emoji, cw:x, cw:y, cw:color)
- Entity Property -> ``owl:DatatypeProperty`` (+ rdfs:domain = the class(es),
                                                 rdfs:range = an XSD type or a class
                                                 expression, optional cw:isKey)
- Relationship    -> ``owl:ObjectProperty``   (+ rdfs:domain = source(s),
                                                 rdfs:range = target(s))
                     + ``cw:cardinality``     ("one-to-one" | "one-to-many" |
                                                "many-to-one" | "many-to-many")

CTR-0169 v2 (UDR-0180): the projection is STATEMENT-COMPLETE and resource-centric.
Every triple of the default graph appears exactly once, as a statement ``{p, o}``
of the resource for its subject, with every term typed (RDF 1.2 Term JSON:
iri / bnode / literal / triple). Nothing is lifted, consumed, defaulted, coerced or
synthesized, so GET followed by an unchanged PUT writes back the same graph and the
same declarations (UDR-0180 D1). The kept declarations are prefixes, base and
VERSION (UDR-0180 D6); comments, statement order and abbreviated syntax are not
part of the abstract syntax and are written in the canonical form.

The codec never goes through ``pyoxigraph.Store``: the store re-encodes typed
literals (``"01"^^xsd:integer`` reads back as ``"1"``), so ``parse`` /
``serialize`` are used directly to keep lexical forms exact.

This module owns the ONE Turtle <-> projection codec (UDR-0084 D6); the frontend
never parses or serializes RDF text, it edits the typed statement JSON.
"""

from __future__ import annotations

from datetime import date
import re
from typing import Any
from xml.sax.saxutils import unescape as xml_unescape

# ---- Namespaces (fixed; UDR-0084 D4) --------------------------------------

CW = "https://chatwalaau.com/ontology#"
OWL = "http://www.w3.org/2002/07/owl#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
XSD = "http://www.w3.org/2001/XMLSchema#"

RDF_TYPE = f"{RDF}type"
RDF_LANG_STRING = f"{RDF}langString"
RDF_DIR_LANG_STRING = f"{RDF}dirLangString"
OWL_CLASS = f"{OWL}Class"
OWL_OBJECT_PROPERTY = f"{OWL}ObjectProperty"
OWL_DATATYPE_PROPERTY = f"{OWL}DatatypeProperty"
RDFS_LABEL = f"{RDFS}label"
RDFS_COMMENT = f"{RDFS}comment"
RDFS_DOMAIN = f"{RDFS}domain"
RDFS_RANGE = f"{RDFS}range"
RDFS_CLASS = f"{RDFS}Class"
RDFS_DATATYPE = f"{RDFS}Datatype"
RDFS_SUB_CLASS_OF = f"{RDFS}subClassOf"
RDFS_SUB_PROPERTY_OF = f"{RDFS}subPropertyOf"
RDF_PROPERTY = f"{RDF}Property"
OWL_ON_DATATYPE = f"{OWL}onDatatype"
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

# Built-in prefixes: written into a NEW document and added at import for names the
# source does not bind. A save writes exactly ``document.prefixes`` (UDR-0180 D6),
# so a user can remove one of these and it stays removed.
TURTLE_PREFIXES = {"cw": CW, "owl": OWL, "rdf": RDF, "rdfs": RDFS, "xsd": XSD}

PROJECTION_VERSION = 3

# Role precedence when a subject carries several of the drawn types (punning).
ROLES = ("entity", "object_property", "datatype_property", "other")

# Ranges that make an rdf:Property an attribute (UDR-0185 D1), besides the xsd:
# namespace and terms the model declares as datatypes.
LITERAL_RANGES = frozenset(
    {
        f"{RDFS}Literal",
        RDF_LANG_STRING,
        RDF_DIR_LANG_STRING,
        f"{RDF}JSON",
        f"{RDF}HTML",
        f"{RDF}XMLLiteral",
        f"{RDF}PlainLiteral",
    }
)


def term_key(term: dict[str, Any]) -> str:
    """The editor's term key (`<iri>` / `_:label`), as the shared roles fixture writes it."""
    return f"<{term['value']}>" if term["type"] == "iri" else f"_:{term['value']}"


def declares_datatype(statements: list[dict[str, Any]]) -> bool:
    """True for a term typed rdfs:Datatype or carrying owl:onDatatype (a datatype restriction)."""
    for statement in statements:
        if statement["p"] == OWL_ON_DATATYPE:
            return True
        o = statement["o"]
        if statement["p"] == RDF_TYPE and o["type"] == "iri" and o["value"] == RDFS_DATATYPE:
            return True
    return False


def is_literal_range(term: dict[str, Any], datatypes: set[str]) -> bool:
    if term["type"] == "iri":
        value = term["value"]
        if value in LITERAL_RANGES or value.startswith(XSD):
            return True
    return term["type"] in ("iri", "bnode") and term_key(term) in datatypes


def classify_role(term: dict[str, Any], statements: list[dict[str, Any]], datatypes: set[str]) -> str:
    """The display role of a resource (UDR-0185 D1). Never stored, never written.

    ``datatypes`` holds the keys of the terms the model declares as datatypes
    (``declares_datatype``); the frontend ``roleOf`` implements the same table and
    ``tests/fixtures/ontology_roles/cases.json`` pins both (D2).
    """
    if term["type"] != "iri":
        return "other"  # a blank-node owl:Class is a class expression: shown nested
    types = {s["o"]["value"] for s in statements if s["p"] == RDF_TYPE and s["o"]["type"] == "iri"}
    if OWL_CLASS in types or (RDFS_CLASS in types and RDFS_DATATYPE not in types):
        return "entity"
    if OWL_OBJECT_PROPERTY in types:
        return "object_property"
    if OWL_DATATYPE_PROPERTY in types:
        return "datatype_property"
    if RDF_PROPERTY in types:
        ranges = [s["o"] for s in statements if s["p"] == RDFS_RANGE]
        if not ranges:
            return "other"
        if all(is_literal_range(r, datatypes) for r in ranges):
            return "datatype_property"
        return "object_property"
    return "other"


def assign_roles(resources: list[dict[str, Any]]) -> None:
    """Set ``role`` on every resource of a projection (in place)."""
    datatypes = {term_key(r["term"]) for r in resources if declares_datatype(r["statements"])}
    for resource in resources:
        resource["role"] = classify_role(resource["term"], resource["statements"], datatypes)


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


class ProjectionError(ValueError):
    """An invalid projection, with a JSON pointer to the offending field (422)."""

    def __init__(self, pointer: str, message: str) -> None:
        super().__init__(f"{pointer}: {message}")
        self.pointer = pointer
        self.message = message


# ---- Declarations (prefixes / base / VERSION; UDR-0180 D6) -------------------

# A Turtle 1.2 version directive: ``VERSION "1.2"`` (keyword, case-insensitive) or
# ``@version "1.2" .``. Only line-leading positions are scanned; a false hit can only
# set ``version`` and never changes a triple.
_TURTLE_VERSION_RE = re.compile(
    r"""^[ \t]*(?:@version|(?i:VERSION))[ \t]+(?:"([^"\r\n]*)"|'([^'\r\n]*)')""",
    re.MULTILINE,
)
_XML_ROOT_RE = re.compile(r"<([A-Za-z_][\w.\-]*(?::[A-Za-z_][\w.\-]*)?)(\s[^<>]*?)?/?>", re.DOTALL)
_XML_ATTR_RE = re.compile(r"""([A-Za-z_][\w.\-]*(?::[A-Za-z_][\w.\-]*)?)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
_XML_SKIP_RE = re.compile(r"<\?.*?\?>|<!--.*?-->|<!DOCTYPE(?:[^\[>]|\[.*?\])*>", re.DOTALL | re.IGNORECASE)


def _empty_document() -> dict[str, Any]:
    return {"prefixes": [], "base": None, "version": None}


def turtle_version(text: str) -> str | None:
    match = _TURTLE_VERSION_RE.search(text)
    if match is None:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2)


def rdfxml_declarations(data: bytes) -> dict[str, Any]:
    """Prefixes / base / version from the RDF/XML root start tag.

    A non-expanding scan of the FIRST element's start tag only (no DTD, no entity
    expansion); the document itself was already validated by pyoxigraph, which
    exposes none of these declarations for RDF/XML.
    """
    doc = _empty_document()
    text = _XML_SKIP_RE.sub("", data.decode("utf-8", errors="replace"))
    root = _XML_ROOT_RE.search(text)
    if root is None:
        return doc
    rdf_prefix = None
    attrs: list[tuple[str, str]] = []
    for match in _XML_ATTR_RE.finditer(root.group(2) or ""):
        value = xml_unescape(
            match.group(2) if match.group(2) is not None else match.group(3), {"&quot;": '"', "&apos;": "'"}
        )
        attrs.append((match.group(1), value))
    for name, value in attrs:
        if name.startswith("xmlns:"):
            prefix = name[len("xmlns:") :]
            if prefix == "xml":
                continue
            doc["prefixes"].append({"prefix": prefix, "iri": value})
            if value == RDF:
                rdf_prefix = prefix
        elif name == "xmlns":
            doc["prefixes"].append({"prefix": "", "iri": value})
        elif name == "xml:base":
            doc["base"] = value
    if rdf_prefix is not None:
        for name, value in attrs:
            if name == f"{rdf_prefix}:version":
                doc["version"] = value
    return doc


def _with_builtin_prefixes(prefixes: list[dict[str, str]]) -> list[dict[str, str]]:
    names = {p["prefix"] for p in prefixes}
    iris = {p["iri"] for p in prefixes}
    out = list(prefixes)
    for name, iri in TURTLE_PREFIXES.items():
        if name not in names and iri not in iris:
            out.append({"prefix": name, "iri": iri})
    return out


# ---- Terms (RDF 1.2 Term JSON; UDR-0180 D3) ----------------------------------


def term_to_json(term: Any) -> dict[str, Any]:
    import pyoxigraph as ox

    if isinstance(term, ox.NamedNode):
        return {"type": "iri", "value": term.value}
    if isinstance(term, ox.BlankNode):
        return {"type": "bnode", "value": term.value}
    if isinstance(term, ox.Literal):
        out: dict[str, Any] = {"type": "literal", "value": term.value, "datatype": term.datatype.value}
        if term.language:
            out["language"] = term.language
        if term.direction is not None:
            out["direction"] = str(term.direction)
        return out
    if isinstance(term, ox.Triple):
        return {
            "type": "triple",
            "s": term_to_json(term.subject),
            "p": term.predicate.value,
            "o": term_to_json(term.object),
        }
    raise ValueError(f"unsupported RDF term: {term!r}")


_LANG_RE = re.compile(r"^[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*$")


def _iri(value: Any, pointer: str) -> Any:
    import pyoxigraph as ox

    if not isinstance(value, str) or not value:
        raise ProjectionError(pointer, "must be a non-empty absolute IRI")
    try:
        return ox.NamedNode(value)
    except ValueError as exc:
        raise ProjectionError(pointer, f"invalid IRI: {exc}") from exc


def term_from_json(obj: Any, pointer: str, *, position: str = "object") -> Any:
    """Build a pyoxigraph term from Term JSON, validating its shape (D8)."""
    import pyoxigraph as ox

    if not isinstance(obj, dict):
        raise ProjectionError(pointer, "must be a term object")
    kind = obj.get("type")
    if kind == "iri":
        return _iri(obj.get("value"), f"{pointer}.value")
    if kind == "bnode":
        value = obj.get("value")
        if not isinstance(value, str) or not value:
            raise ProjectionError(f"{pointer}.value", "must be a blank node label")
        try:
            return ox.BlankNode(value)
        except ValueError as exc:
            raise ProjectionError(f"{pointer}.value", f"invalid blank node label: {exc}") from exc
    if position == "subject":
        raise ProjectionError(f"{pointer}.type", "a subject must be an IRI or a blank node")
    if kind == "literal":
        value = obj.get("value")
        if not isinstance(value, str):
            raise ProjectionError(f"{pointer}.value", "must be a string (the lexical form)")
        language = obj.get("language") or None
        direction = obj.get("direction") or None
        datatype = obj.get("datatype") or None
        if direction is not None:
            if direction not in ("ltr", "rtl"):
                raise ProjectionError(f"{pointer}.direction", 'must be "ltr" or "rtl"')
            if language is None:
                raise ProjectionError(f"{pointer}.direction", "requires a language")
        if language is not None:
            if not isinstance(language, str) or not _LANG_RE.match(language):
                raise ProjectionError(f"{pointer}.language", "must be a well-formed language tag")
            expected = RDF_DIR_LANG_STRING if direction else RDF_LANG_STRING
            if datatype not in (None, expected):
                raise ProjectionError(
                    f"{pointer}.datatype", f"a literal with this language must have datatype {expected}"
                )
            try:
                if direction:
                    base = ox.BaseDirection.RTL if direction == "rtl" else ox.BaseDirection.LTR
                    return ox.Literal(value, language=language, direction=base)
                return ox.Literal(value, language=language)
            except ValueError as exc:
                raise ProjectionError(f"{pointer}.language", str(exc)) from exc
        if datatype in (RDF_LANG_STRING, RDF_DIR_LANG_STRING):
            raise ProjectionError(f"{pointer}.datatype", "rdf:langString / rdf:dirLangString require a language")
        if datatype is None:
            return ox.Literal(value)
        return ox.Literal(value, datatype=_iri(datatype, f"{pointer}.datatype"))
    if kind == "triple":
        return ox.Triple(
            term_from_json(obj.get("s"), f"{pointer}.s", position="subject"),
            _iri(obj.get("p"), f"{pointer}.p"),
            term_from_json(obj.get("o"), f"{pointer}.o"),
        )
    raise ProjectionError(f"{pointer}.type", 'must be "iri", "bnode", "literal" or "triple"')


# ---- Diagnostics (display-only; never change a literal) ----------------------

_INT_RE = re.compile(r"^[+-]?\d+$")
_DECIMAL_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_DOUBLE_RE = re.compile(r"^(?:[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[+-]?INF|NaN)$")
_TZ = r"(?:Z|[+-]\d{2}:\d{2})?"
_DATE_RE = re.compile(rf"^(-?\d{{4,}})-(\d{{2}})-(\d{{2}}){_TZ}$")
_TIME_RE = re.compile(rf"^(\d{{2}}):(\d{{2}}):(\d{{2}})(?:\.\d+)?{_TZ}$")
_DATETIME_RE = re.compile(rf"^(-?\d{{4,}})-(\d{{2}})-(\d{{2}})T(\d{{2}}):(\d{{2}}):(\d{{2}})(?:\.\d+)?{_TZ}$")

_INTEGER_TYPES = {
    "integer": None,
    "int": None,
    "long": None,
    "short": None,
    "byte": None,
    "nonNegativeInteger": lambda n: n >= 0,
    "positiveInteger": lambda n: n > 0,
    "nonPositiveInteger": lambda n: n <= 0,
    "negativeInteger": lambda n: n < 0,
    "unsignedLong": lambda n: n >= 0,
    "unsignedInt": lambda n: n >= 0,
    "unsignedShort": lambda n: n >= 0,
    "unsignedByte": lambda n: n >= 0,
}


def _valid_date(y: str, m: str, d: str) -> bool:
    try:
        date(max(1, min(9999, abs(int(y)))), int(m), int(d))
        return True
    except ValueError:
        return False


def literal_is_ill_typed(value: str, datatype: str) -> bool:
    """True when ``value`` is not a valid lexical form of a common XSD datatype."""
    if not datatype.startswith(XSD):
        return False
    name = datatype[len(XSD) :]
    if name in _INTEGER_TYPES:
        if not _INT_RE.match(value):
            return True
        check = _INTEGER_TYPES[name]
        return check is not None and not check(int(value))
    if name == "decimal":
        return not _DECIMAL_RE.match(value)
    if name in ("double", "float"):
        return not _DOUBLE_RE.match(value)
    if name == "boolean":
        return value not in ("true", "false", "1", "0")
    if name == "date":
        m = _DATE_RE.match(value)
        return m is None or not _valid_date(*m.groups())
    if name == "time":
        m = _TIME_RE.match(value)
        return m is None or not _valid_time(*m.groups())
    if name == "dateTime":
        m = _DATETIME_RE.match(value)
        if m is None:
            return True
        y, mo, d, h, mi, s = m.groups()
        return not (_valid_date(y, mo, d) and _valid_time(h, mi, s))
    return False


def _valid_time(h: str, mi: str, s: str) -> bool:
    """XSD allows 24:00:00 as end of day; otherwise the usual clock ranges."""
    hour, minute, second = int(h), int(mi), int(s)
    if hour == 24:
        return minute == 0 and second == 0
    return hour <= 23 and minute <= 59 and second <= 59


def _diagnose(term: dict[str, Any], subject: dict[str, Any], predicate: str, out: list[dict[str, Any]]) -> None:
    if term["type"] == "literal" and literal_is_ill_typed(term["value"], term["datatype"]):
        out.append({"kind": "ill_typed_literal", "s": subject, "p": predicate, "o": term})
    elif term["type"] == "triple":
        _diagnose(term["o"], subject, predicate, out)


# ---- Dataset -> projection (GET) ---------------------------------------------


def _parse(data: bytes, fmt: Any, base_iri: str | None = None) -> tuple[list[Any], Any]:
    """Parse into quads WITHOUT a Store (exact lexical forms); returns (quads, parser).

    ``base_iri`` resolves relative IRIs when the source declares no base of its own
    (UDR-0181 D6); a base the source declares still wins.
    """
    import pyoxigraph as ox

    parser = ox.parse(data, format=fmt, base_iri=base_iri) if base_iri else ox.parse(data, format=fmt)
    return list(parser), parser


def is_named(quad: Any) -> bool:
    """True when the quad is in a named graph (not the default graph)."""
    import pyoxigraph as ox

    return not isinstance(quad.graph_name, ox.DefaultGraph)


def read_dataset(data: bytes) -> tuple[list[Any], dict[str, Any]]:
    """Parse a stored file (Turtle or TriG) into (quads, document); ValueError on a syntax error.

    The TriG parser reads both: Turtle is a subset of TriG (UDR-0182 D3).
    """
    import pyoxigraph as ox

    if not data.strip():
        return [], _empty_document()
    try:
        quads, parser = _parse(data, ox.RdfFormat.TRIG)
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"RDF parse error: {exc}") from exc
    return quads, document_of(parser, data)


def document_of(parser: Any, data: bytes) -> dict[str, Any]:
    """The declarations of a FULLY ITERATED Turtle / TriG parser (prefixes / base / VERSION)."""
    return {
        "prefixes": [{"prefix": k, "iri": v} for k, v in (parser.prefixes or {}).items()],
        "base": parser.base_iri or None,
        "version": turtle_version(data.decode("utf-8", errors="replace")),
    }


def _key(term: dict[str, Any]) -> tuple[str, str]:
    return (term["type"], term["value"])


def quads_to_projection(quads: list[Any], document: dict[str, Any]) -> dict[str, Any]:
    """Group quads by subject into the statement-complete projection v3 (UDR-0182 D1).

    One resource per subject across every graph; a statement in a named graph
    carries ``g``; ``graphs`` lists the named graphs in file order.
    """
    resources: dict[tuple[str, str], dict[str, Any]] = {}
    diagnostics: list[dict[str, Any]] = []
    graphs: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    triples: set[str] = set()
    for quad in quads:
        identity = str(quad)
        if identity in seen:  # a dataset is a set of quads
            continue
        seen.add(identity)
        triples.add(str(quad.triple))
        subject = term_to_json(quad.subject)
        key = _key(subject)
        resource = resources.get(key)
        if resource is None:
            resource = resources[key] = {"term": subject, "role": "other", "statements": []}
        obj = term_to_json(quad.object)
        predicate = quad.predicate.value
        statement: dict[str, Any] = {"p": predicate, "o": obj}
        if is_named(quad):
            graph = term_to_json(quad.graph_name)
            graphs.setdefault(str(quad.graph_name), graph)
            statement["g"] = graph
        resource["statements"].append(statement)
        before = len(diagnostics)
        _diagnose(obj, subject, predicate, diagnostics)
        if "g" in statement:
            for item in diagnostics[before:]:
                item["g"] = statement["g"]

    assign_roles(list(resources.values()))

    ordered = sorted(
        resources.values(),
        key=lambda r: (ROLES.index(r["role"]), r["term"]["type"] != "iri", r["term"]["value"]),
    )
    return {
        "projection_version": PROJECTION_VERSION,
        "document": document,
        "graphs": list(graphs.values()),
        "resources": ordered,
        "diagnostics": diagnostics,
        "triple_count": len(triples),
        "quad_count": len(seen),
    }


def turtle_to_projection(data: bytes) -> dict[str, Any]:
    """Decode a stored Turtle / TriG document into the CTR-0169 v3 projection (GET).

    Lossless by construction: every quad becomes exactly one statement of its
    subject; nothing is lifted, defaulted or coerced. Raises ``ValueError`` on a
    syntax error.
    """
    quads, document = read_dataset(data)
    return quads_to_projection(quads, document)


# ---- projection -> Turtle / TriG (PUT) ----------------------------------------


# Turtle PN_PREFIX (RDF 1.1 / 1.2 grammar), so every prefix a valid file declares
# is accepted back on save (found by the W3C gate, UDR-0181 D7).
_PN_CHARS_BASE = "A-Za-zÀ-ÖØ-öø-˿Ͱ-ͽͿ-῿‌-‍⁰-↏Ⰰ-⿯、-퟿豈-﷏ﷰ-�\U00010000-\U000effff"
_PN_CHARS = _PN_CHARS_BASE + "_\\-0-9·̀-ͯ‿-⁀"
_PN_PREFIX_RE = re.compile(f"[{_PN_CHARS_BASE}](?:[{_PN_CHARS}.]*[{_PN_CHARS}])?")


def _validate_document(document: Any) -> dict[str, Any]:
    if document is None:
        return _empty_document()
    if not isinstance(document, dict):
        raise ProjectionError("document", "must be an object")
    prefixes_in = document.get("prefixes") or []
    if not isinstance(prefixes_in, list):
        raise ProjectionError("document.prefixes", "must be a list")
    prefixes: list[dict[str, str]] = []
    names: set[str] = set()
    for i, entry in enumerate(prefixes_in):
        pointer = f"document.prefixes[{i}]"
        if not isinstance(entry, dict):
            raise ProjectionError(pointer, "must be {prefix, iri}")
        name = entry.get("prefix")
        if not isinstance(name, str) or (name and not _PN_PREFIX_RE.fullmatch(name)):
            raise ProjectionError(f"{pointer}.prefix", "must be a valid prefix name (may be empty)")
        if name in names:
            raise ProjectionError(f"{pointer}.prefix", f"duplicate prefix {name!r}")
        names.add(name)
        _iri(entry.get("iri"), f"{pointer}.iri")
        prefixes.append({"prefix": name, "iri": entry["iri"]})
    base = document.get("base") or None
    if base is not None:
        _iri(base, "document.base")
    version = document.get("version") or None
    if version is not None and (not isinstance(version, str) or '"' in version or "\n" in version):
        raise ProjectionError("document.version", "must be a version string such as 1.2")
    return {"prefixes": prefixes, "base": base, "version": version}


def graph_from_json(obj: Any, pointer: str) -> Any:
    """The graph of a statement: absent / null = the default graph, else an IRI or blank node."""
    import pyoxigraph as ox

    if obj is None:
        return ox.DefaultGraph()
    return term_from_json(obj, pointer, position="subject")


def statement_quad(subject: Any, statement: Any, pointer: str) -> Any:
    """One ``{p, o, g?}`` statement of ``subject`` as a quad (validated)."""
    import pyoxigraph as ox

    if not isinstance(statement, dict):
        raise ProjectionError(pointer, "must be {p, o, g?}")
    return ox.Quad(
        subject,
        _iri(statement.get("p"), f"{pointer}.p"),
        term_from_json(statement.get("o"), f"{pointer}.o"),
        graph_from_json(statement.get("g"), f"{pointer}.g"),
    )


def projection_to_quads(projection: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    """Validate a v2 / v3 projection and return (quads, document). Raises ProjectionError."""
    document = _validate_document(projection.get("document"))
    resources = projection.get("resources")
    if resources is None:
        resources = []
    if not isinstance(resources, list):
        raise ProjectionError("resources", "must be a list")
    quads: list[Any] = []
    seen: set[str] = set()
    for i, resource in enumerate(resources):
        pointer = f"resources[{i}]"
        if not isinstance(resource, dict):
            raise ProjectionError(pointer, "must be a resource object")
        subject = term_from_json(resource.get("term"), f"{pointer}.term", position="subject")
        statements = resource.get("statements") or []
        if not isinstance(statements, list):
            raise ProjectionError(f"{pointer}.statements", "must be a list")
        for j, statement in enumerate(statements):
            quad = statement_quad(subject, statement, f"{pointer}.statements[{j}]")
            identity = str(quad)
            if identity not in seen:
                seen.add(identity)
                quads.append(quad)
    return quads, document


def _grouped(quads: list[Any]) -> list[Any]:
    """A stable order with one block per graph and per subject (first appearance wins).

    The writers start a new graph block / subject paragraph whenever these change, so
    interleaved quads (an edit appended at the end) would otherwise split a block.
    """
    graphs: dict[Any, int] = {}
    subjects: dict[Any, int] = {}
    for quad in quads:
        graphs.setdefault(quad.graph_name, len(graphs))
        subjects.setdefault(quad.subject, len(subjects))
    return sorted(quads, key=lambda q: (graphs[q.graph_name], subjects[q.subject]))


def write_document(quads: list[Any], document: dict[str, Any], *, trig: bool | None = None) -> str:
    """Write quads with the document's declarations: VERSION, @base, @prefix, then the body.

    Turtle when every quad is in the default graph, TriG otherwise (UDR-0182 D3);
    ``trig=True`` forces TriG. Prefix IRIs stay absolute (the serializer is not given
    ``base_iri``, so it does not relativize them); ``@base`` is emitted by hand.
    """
    import pyoxigraph as ox

    if trig is None:
        trig = any(is_named(q) for q in quads)
    quads = _grouped(quads)
    header = ""
    if document.get("version"):
        header += f'VERSION "{document["version"]}"\n'
    if document.get("base"):
        header += f"@base <{document['base']}> .\n"
    prefixes = {p["prefix"]: p["iri"] for p in document.get("prefixes") or []}
    try:
        if trig:
            body = ox.serialize(quads, format=ox.RdfFormat.TRIG, prefixes=prefixes).decode("utf-8")
        else:
            triples = [q.triple for q in quads]
            body = ox.serialize(triples, format=ox.RdfFormat.TURTLE, prefixes=prefixes).decode("utf-8")
    except ValueError as exc:
        raise ProjectionError("document.prefixes", f"cannot serialize: {exc}") from exc
    if not quads and prefixes:
        # pyoxigraph writes nothing for an empty graph; keep the declarations anyway.
        body = "".join(f"@prefix {name}: <{iri}> .\n" for name, iri in prefixes.items())
    return header + body


def projection_to_text(projection: dict[str, Any]) -> tuple[str, bool]:
    """Encode a projection: (Turtle or TriG text, True when it is a TriG dataset). ProjectionError -> 422."""
    quads, document = projection_to_quads(projection)
    dataset = any(is_named(q) for q in quads)
    return write_document(quads, document, trig=dataset), dataset


def projection_to_turtle(projection: dict[str, Any]) -> str:
    """Encode a CTR-0169 projection (Turtle, or TriG for a dataset). Raises ProjectionError (422)."""
    return projection_to_text(projection)[0]


def new_document_turtle(ontology_base: str) -> str:
    """The initial Turtle of a NEW ontology: the built-in prefixes plus ``:`` = its base."""
    document = {
        "prefixes": [
            {"prefix": "", "iri": ontology_base},
            *({"prefix": k, "iri": v} for k, v in TURTLE_PREFIXES.items()),
        ],
        "base": None,
        "version": None,
    }
    return write_document([], document)


class RemoteContextError(ValueError):
    """A JSON-LD document refers to a remote @context, which is never fetched (UDR-0182 D6)."""

    def __init__(self, url: str) -> None:
        super().__init__(f"Remote JSON-LD contexts are not supported: {url}")
        self.url = url


def _remote_context(data: bytes) -> str | None:
    """The first remote ``@context`` / ``@import`` URL in a JSON-LD document, if any."""
    import json

    try:
        doc = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None

    def contexts(node: Any) -> Any:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("@context", "@import"):
                    yield value
                yield from contexts(value)
        elif isinstance(node, list):
            for item in node:
                yield from contexts(item)

    for value in contexts(doc):
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                return item
            if isinstance(item, dict) and isinstance(item.get("@import"), str):
                return item["@import"]
    return None


def import_to_turtle(
    data: bytes, fmt: Any, *, rdfxml: bool | None = None, base_iri: str | None = None
) -> tuple[str, int]:
    """``import_document`` without the dataset flag: (Turtle / TriG text, quad count)."""
    text, count, _ = import_document(data, fmt, rdfxml=rdfxml, base_iri=base_iri)
    return text, count


def import_document(
    data: bytes, fmt: Any, *, rdfxml: bool | None = None, base_iri: str | None = None
) -> tuple[str, int, bool]:
    """Parse an upload in any supported format: (Turtle / TriG text, quad count, is a dataset).

    The source's declarations are kept (UDR-0180 D6; JSON-LD keeps its top-level
    ``@context`` prefixes and ``@base``) and the built-in prefixes are added for names
    it does not bind. ``base_iri`` resolves the relative IRIs of a source that declares
    no base (UDR-0181 D6); it is NOT recorded as the document's base. A dataset with a
    named graph is written as TriG (UDR-0182 D3). Raises on a syntax error (the caller
    tries the next format) and ``RemoteContextError`` for a remote JSON-LD context.
    """
    import pyoxigraph as ox

    if rdfxml is None:
        rdfxml = fmt == ox.RdfFormat.RDF_XML
    jsonld = fmt in (ox.RdfFormat.JSON_LD, ox.RdfFormat.STREAMING_JSON_LD)
    try:
        quads, parser = _parse(data, fmt, base_iri)
    except (SyntaxError, ValueError) as exc:
        if jsonld:
            url = _remote_context(data)
            if url is not None and "LoadDocumentCallback" in str(exc):
                raise RemoteContextError(url) from exc
        raise
    if rdfxml:
        document = rdfxml_declarations(data)
    else:
        declared = getattr(parser, "base_iri", None) or None
        version = None
        if fmt in (ox.RdfFormat.TURTLE, ox.RdfFormat.TRIG):
            version = turtle_version(data.decode("utf-8", errors="replace"))
        document = {
            "prefixes": [{"prefix": k, "iri": v} for k, v in (getattr(parser, "prefixes", None) or {}).items()],
            # The parser reports the supplied base when the source declares none.
            "base": None if declared == base_iri else declared,
            "version": version,
        }
    document["prefixes"] = _with_builtin_prefixes(document["prefixes"])
    unique = list({str(q): q for q in quads}.values())
    dataset = any(is_named(q) for q in unique)
    return write_document(unique, document, trig=dataset), len(unique), dataset


__all__ = [
    "CARDINALITIES",
    "CW",
    "DEFAULT_CARDINALITY",
    "OWL",
    "PROJECTION_VERSION",
    "RDFS",
    "TURTLE_PREFIXES",
    "XSD",
    "ProjectionError",
    "RemoteContextError",
    "base_iri_for",
    "document_of",
    "graph_from_json",
    "import_document",
    "import_to_turtle",
    "is_named",
    "literal_is_ill_typed",
    "local_name",
    "new_document_turtle",
    "projection_to_quads",
    "projection_to_text",
    "projection_to_turtle",
    "quads_to_projection",
    "rdfxml_declarations",
    "read_dataset",
    "statement_quad",
    "term_from_json",
    "term_to_json",
    "turtle_to_projection",
    "turtle_version",
    "write_document",
]
