"""Ontology meta-vocabulary and the lossless Turtle codec (CTR-0169 v2, PRP-0198, UDR-0180).

The ``cw:`` application namespace (https://chatwalaau.com/ontology#) and the
OWL-minimal meta-vocabulary the canvas draws (UDR-0084 D4):

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

PROJECTION_VERSION = 2

# Role precedence when a subject carries several of the drawn types (punning).
ROLES = ("entity", "object_property", "datatype_property", "other")
_ROLE_BY_TYPE = (
    (OWL_CLASS, "entity"),
    (OWL_OBJECT_PROPERTY, "object_property"),
    (OWL_DATATYPE_PROPERTY, "datatype_property"),
)


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


# ---- Turtle -> projection (GET) ---------------------------------------------


def _parse(data: bytes, fmt: Any) -> tuple[list[Any], Any]:
    """Parse into triples WITHOUT a Store (exact lexical forms); returns (triples, parser)."""
    import pyoxigraph as ox

    parser = ox.parse(data, format=fmt)
    triples = [ox.Triple(q.subject, q.predicate, q.object) for q in parser]
    return triples, parser


def read_turtle(data: bytes) -> tuple[list[Any], dict[str, Any]]:
    """Parse Turtle into (triples, document). Raises ValueError on a syntax error."""
    import pyoxigraph as ox

    if not data.strip():
        return [], _empty_document()
    try:
        triples, parser = _parse(data, ox.RdfFormat.TURTLE)
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"Turtle parse error: {exc}") from exc
    document = {
        "prefixes": [{"prefix": k, "iri": v} for k, v in (parser.prefixes or {}).items()],
        "base": parser.base_iri or None,
        "version": turtle_version(data.decode("utf-8", errors="replace")),
    }
    return triples, document


def _key(term: dict[str, Any]) -> tuple[str, str]:
    return (term["type"], term["value"])


def triples_to_projection(triples: list[Any], document: dict[str, Any]) -> dict[str, Any]:
    """Group triples by subject into the statement-complete projection (D2)."""
    resources: dict[tuple[str, str], dict[str, Any]] = {}
    types: dict[tuple[str, str], set[str]] = {}
    diagnostics: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    count = 0
    for triple in triples:
        identity = (str(triple.subject), str(triple.predicate), str(triple.object))
        if identity in seen:  # an RDF graph is a set
            continue
        seen.add(identity)
        count += 1
        subject = term_to_json(triple.subject)
        key = _key(subject)
        resource = resources.get(key)
        if resource is None:
            resource = resources[key] = {"term": subject, "role": "other", "statements": []}
        obj = term_to_json(triple.object)
        predicate = triple.predicate.value
        resource["statements"].append({"p": predicate, "o": obj})
        if predicate == RDF_TYPE and obj["type"] == "iri":
            types.setdefault(key, set()).add(obj["value"])
        _diagnose(obj, subject, predicate, diagnostics)

    for key, resource in resources.items():
        if resource["term"]["type"] != "iri":
            continue  # a blank-node owl:Class is a class expression: shown nested
        for type_iri, role in _ROLE_BY_TYPE:
            if type_iri in types.get(key, set()):
                resource["role"] = role
                break

    ordered = sorted(
        resources.values(),
        key=lambda r: (ROLES.index(r["role"]), r["term"]["type"] != "iri", r["term"]["value"]),
    )
    return {
        "projection_version": PROJECTION_VERSION,
        "document": document,
        "resources": ordered,
        "diagnostics": diagnostics,
        "triple_count": count,
    }


def turtle_to_projection(data: bytes) -> dict[str, Any]:
    """Decode a stored Turtle document into the CTR-0169 v2 projection (GET).

    Lossless by construction: every triple becomes exactly one statement of its
    subject; nothing is lifted, defaulted or coerced. Raises ``ValueError`` on a
    syntax error.
    """
    triples, document = read_turtle(data)
    return triples_to_projection(triples, document)


# ---- projection -> Turtle (PUT) ---------------------------------------------


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
        if not isinstance(name, str) or (name and not re.match(r"^[^\W\d][\w.\-]*$", name)) or name.endswith("."):
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


def projection_to_triples(projection: dict[str, Any]) -> tuple[list[Any], dict[str, Any]]:
    """Validate a v2 projection and return (triples, document). Raises ProjectionError."""
    import pyoxigraph as ox

    document = _validate_document(projection.get("document"))
    resources = projection.get("resources")
    if resources is None:
        resources = []
    if not isinstance(resources, list):
        raise ProjectionError("resources", "must be a list")
    triples: list[Any] = []
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
            sp = f"{pointer}.statements[{j}]"
            if not isinstance(statement, dict):
                raise ProjectionError(sp, "must be {p, o}")
            triple = ox.Triple(
                subject, _iri(statement.get("p"), f"{sp}.p"), term_from_json(statement.get("o"), f"{sp}.o")
            )
            identity = str(triple)
            if identity not in seen:
                seen.add(identity)
                triples.append(triple)
    return triples, document


def write_turtle(triples: list[Any], document: dict[str, Any]) -> str:
    """Write triples with the document's declarations: VERSION, @base, @prefix, triples.

    Prefix IRIs stay absolute (the serializer is not given ``base_iri``, so it does
    not relativize them); ``@base`` is emitted by hand before them.
    """
    import pyoxigraph as ox

    header = ""
    if document.get("version"):
        header += f'VERSION "{document["version"]}"\n'
    if document.get("base"):
        header += f"@base <{document['base']}> .\n"
    prefixes = {p["prefix"]: p["iri"] for p in document.get("prefixes") or []}
    try:
        body = ox.serialize(triples, format=ox.RdfFormat.TURTLE, prefixes=prefixes).decode("utf-8")
    except ValueError as exc:
        raise ProjectionError("document.prefixes", f"cannot serialize: {exc}") from exc
    if not triples and prefixes:
        # pyoxigraph writes nothing for an empty graph; keep the declarations anyway.
        body = "".join(f"@prefix {name}: <{iri}> .\n" for name, iri in prefixes.items())
    return header + body


def projection_to_turtle(projection: dict[str, Any]) -> str:
    """Encode a CTR-0169 v2 projection into Turtle (PUT). Raises ProjectionError (422)."""
    triples, document = projection_to_triples(projection)
    return write_turtle(triples, document)


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
    return write_turtle([], document)


def import_to_turtle(data: bytes, fmt: Any, *, rdfxml: bool) -> tuple[str, int]:
    """Parse an upload (Turtle or RDF/XML) and return (canonical Turtle, triple count).

    The source's declarations are kept (UDR-0180 D6) and the built-in prefixes are
    added for names it does not bind. Raises on a syntax error (the caller tries the
    next format).
    """
    triples, parser = _parse(data, fmt)
    if rdfxml:
        document = rdfxml_declarations(data)
    else:
        document = {
            "prefixes": [{"prefix": k, "iri": v} for k, v in (parser.prefixes or {}).items()],
            "base": parser.base_iri or None,
            "version": turtle_version(data.decode("utf-8", errors="replace")),
        }
    document["prefixes"] = _with_builtin_prefixes(document["prefixes"])
    unique = list({str(t): t for t in triples}.values())
    return write_turtle(unique, document), len(unique)


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
    "base_iri_for",
    "import_to_turtle",
    "literal_is_ill_typed",
    "local_name",
    "new_document_turtle",
    "projection_to_triples",
    "projection_to_turtle",
    "rdfxml_declarations",
    "read_turtle",
    "term_from_json",
    "term_to_json",
    "triples_to_projection",
    "turtle_to_projection",
    "turtle_version",
    "write_turtle",
]
