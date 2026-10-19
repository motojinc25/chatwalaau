"""Table data import: CSV / TSV -> individuals (CTR-0247, PRP-0217, UDR-0199).

A table is mapped to ONE class (D3): a subject IRI template built from key columns, an
``rdf:type`` statement per row, and one rule per column -- ``value`` (a literal),
``reference`` (an IRI from a template or a lookup of an existing individual) or
``ignore``. Nothing here writes: ``inspect`` reads the file and suggests a mapping,
``preview`` converts every row, validates the stored model plus the converted rows with
the ontology's own SHACL configuration (UDR-0194) and traces every result back to its
rows (D6-D9). The accepted rows go back to the editor as statements to add; the
existing save path writes them (D2, UDR-0195 D6).

The file is read with the standard ``csv`` module (D5): the encoding is detected
(UTF-8 BOM, UTF-8, CP932) and the delimiter sniffed, both can be given. Values are
converted by explicit rules (group separators, slashed dates, yes / no); a value that
does not convert is a row error, never a silent string.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import io
import re
from typing import Any
import unicodedata
from urllib.parse import quote

FORMAT = "chatwalaau.table-mapping/1"
MAX_COLUMNS = 256
SAMPLE_ROWS = 20
MAX_ROUNDS = 5
NAME_MAX = 100
ENCODINGS = ("utf-8", "utf-8-sig", "cp932", "euc-jp", "utf-16", "latin-1")
DELIMITERS = {",": ",", "\t": "\t", "tab": "\t", ";": ";", "|": "|"}
EXISTING = ("add", "replace", "skip")
KINDS = ("value", "reference", "ignore")

XSD = "http://www.w3.org/2001/XMLSchema#"
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"
OWL = "http://www.w3.org/2002/07/owl#"
SH = "http://www.w3.org/ns/shacl#"
SKOS = "http://www.w3.org/2004/02/skos/core#"
RDF_TYPE = f"{RDF}type"
RDF_LANG_STRING = f"{RDF}langString"
XSD_STRING = f"{XSD}string"

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
_LANG_RE = re.compile(r"^[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*$")
_GROUPED_RE = re.compile(r"^[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?$")
_DATE_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_DATETIME_RE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$")
_ASCII_TYPES = {"decimal", "double", "float", "boolean", "date", "dateTime", "time", "gYear", "gYearMonth"}
_TRUE = {"true", "yes", "y", "1", "on"}
_FALSE = {"false", "no", "n", "0", "off"}
_INTEGER_NAMES = {
    "integer",
    "int",
    "long",
    "short",
    "byte",
    "nonNegativeInteger",
    "positiveInteger",
    "nonPositiveInteger",
    "negativeInteger",
    "unsignedLong",
    "unsignedInt",
    "unsignedShort",
    "unsignedByte",
}


class TableError(ValueError):
    """A table or a mapping that cannot be used (422; 409 for ``stale_revision``)."""

    def __init__(self, code: str, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = detail

    def __reduce__(self) -> Any:  # crosses the worker process boundary (pickled)
        return (_rebuild_error, (self.code, self.message, self.extra))

    @property
    def detail(self) -> dict[str, Any]:
        return {"error": self.code, "message": self.message, **self.extra}


def _rebuild_error(code: str, message: str, extra: dict[str, Any]) -> TableError:
    return TableError(code, message, **extra)


# ---- Reading the file (D5) -----------------------------------------------------------


@dataclass
class Table:
    encoding: str
    delimiter: str
    header_row: int
    columns: list[str]
    rows: list[list[str]]
    # The physical line each data row starts on (1-based, header included in the count).
    lines: list[int]


def decode(data: bytes, encoding: str = "auto") -> tuple[str, str]:
    """(text, encoding used). ``auto``: UTF-8 BOM, strict UTF-8, CP932, else ask (422)."""
    choice = (encoding or "auto").strip().lower()
    if choice == "auto":
        if data.startswith(b"\xef\xbb\xbf"):
            return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
        for candidate in ("utf-8", "cp932"):
            try:
                return data.decode(candidate), candidate
            except UnicodeDecodeError:
                continue
        raise TableError(
            "decode_failed",
            "The file is neither UTF-8 nor Shift_JIS (CP932). Choose its encoding.",
            encodings=list(ENCODINGS),
        )
    if choice in ("shift_jis", "sjis", "shift-jis", "windows-31j"):
        choice = "cp932"
    if choice not in ENCODINGS:
        raise TableError("invalid_encoding", f"Unknown encoding {encoding!r}.", encodings=list(ENCODINGS))
    try:
        text = data.decode(choice)
    except UnicodeDecodeError as exc:
        raise TableError(
            "decode_failed", f"The file cannot be read as {choice}: {exc.reason} at byte {exc.start}."
        ) from exc
    return text.removeprefix("﻿"), choice


def sniff(text: str, delimiter: str = "auto", filename: str = "") -> str:
    """The delimiter: given (``,`` tab ``;`` ``|``), ``.tsv`` -> tab, else sniffed (default comma)."""
    choice = delimiter or "auto"
    if choice != "auto":
        if choice not in DELIMITERS:
            raise TableError("invalid_delimiter", f"Unknown delimiter {delimiter!r}.", delimiters=[",", "\t", ";", "|"])
        return DELIMITERS[choice]
    if filename.lower().endswith((".tsv", ".tab")):
        return "\t"
    sample = text[:65536]
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        return ","


def _column_names(raw: list[str]) -> list[str]:
    names: list[str] = []
    seen: dict[str, int] = {}
    for index, cell in enumerate(raw):
        name = cell.strip() or f"Column {index + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 1
        names.append(name)
    return names


def read_table(
    data: bytes,
    filename: str = "",
    *,
    encoding: str = "auto",
    delimiter: str = "auto",
    header_row: int = 1,
    max_rows: int | None = None,
) -> Table:
    """Read a CSV / TSV file: the header at ``header_row``, then the data rows.

    Rows whose cells are all blank are skipped. More data rows than ``max_rows`` -> 422
    ``too_many_rows`` (with the count); more than ``MAX_COLUMNS`` columns -> 422.
    """
    text, used = decode(data, encoding)
    delim = sniff(text, delimiter, filename)
    if header_row < 1:
        raise TableError("invalid_mapping", "header_row must be 1 or more.", field="source.header_row")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    columns: list[str] | None = None
    rows: list[list[str]] = []
    lines: list[int] = []
    count = 0
    previous_line = 0
    try:
        for record in reader:
            start = previous_line + 1
            previous_line = reader.line_num
            if columns is None:
                if start < header_row and reader.line_num < header_row:
                    continue
                columns = _column_names(record)
                if len(columns) > MAX_COLUMNS:
                    raise TableError(
                        "too_many_columns",
                        f"The table has {len(columns)} columns; at most {MAX_COLUMNS} are read.",
                        columns=len(columns),
                        limit=MAX_COLUMNS,
                    )
                continue
            if not any(cell.strip() for cell in record):
                continue
            count += 1
            if max_rows is None or count <= max_rows:
                rows.append(record)
                lines.append(start)
    except csv.Error as exc:
        raise TableError("invalid_table", f"The file is not a readable table (line {reader.line_num}): {exc}") from exc
    if columns is None:
        raise TableError("empty_table", "The file has no header row.")
    if max_rows is not None and count > max_rows:
        raise TableError(
            "too_many_rows",
            f"The table has {count} data rows; at most {max_rows} are imported at once "
            "(App Settings: ontology_table_max_rows).",
            rows=count,
            limit=max_rows,
        )
    return Table(used, delim, header_row, columns, rows, lines)


# ---- The mapping (CTR-0247, D3 / D4) -------------------------------------------------


def _absolute_iri(value: Any, pointer: str) -> str:
    import pyoxigraph as ox

    if not isinstance(value, str) or not value.strip():
        raise TableError("invalid_mapping", f"{pointer} must be an absolute IRI.", field=pointer)
    value = value.strip()
    if not _SCHEME_RE.match(value):
        raise TableError("invalid_mapping", f"{pointer} must be an absolute IRI.", field=pointer)
    try:
        ox.NamedNode(value)
    except ValueError as exc:
        raise TableError("invalid_mapping", f"{pointer}: invalid IRI ({exc}).", field=pointer) from exc
    return value


def placeholders(template: str) -> list[str]:
    return _PLACEHOLDER_RE.findall(template)


def _template(value: Any, pointer: str, columns: list[str] | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TableError("invalid_mapping", f"{pointer} must be a non-empty template.", field=pointer)
    names = placeholders(value)
    if not names:
        raise TableError("invalid_mapping", f"{pointer} must name at least one column, e.g. {{ID}}.", field=pointer)
    if columns is not None:
        for name in names:
            if name not in columns:
                raise TableError(
                    "unknown_column",
                    f"{pointer} names the column {name!r}, which the table does not have.",
                    field=pointer,
                    column=name,
                )
    return value.strip()


def validate_mapping(raw: Any, columns: list[str] | None = None, *, require_name: bool = False) -> dict[str, Any]:
    """The mapping, normalized; ``TableError`` (``invalid_mapping`` / ``unknown_column``) otherwise.

    ``columns`` = the table's columns (rules and placeholders must name them); None = a
    saved mapping checked on its own.
    """
    if not isinstance(raw, dict):
        raise TableError("invalid_mapping", "The mapping must be a JSON object.", field="")
    fmt = raw.get("format", FORMAT)
    if fmt != FORMAT:
        raise TableError("invalid_mapping", f"format must be {FORMAT!r}.", field="format")
    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        raise TableError("invalid_mapping", "name must be a string.", field="name")
    name = (name or "").strip()
    if require_name and not name:
        raise TableError("invalid_mapping", "name is required to save a mapping.", field="name")
    if len(name) > NAME_MAX:
        raise TableError("invalid_mapping", f"name is longer than {NAME_MAX} characters.", field="name")

    source_raw = raw.get("source") or {}
    if not isinstance(source_raw, dict):
        raise TableError("invalid_mapping", "source must be an object.", field="source")
    nulls = source_raw.get("null_values", [""])
    if not isinstance(nulls, list) or not all(isinstance(v, str) for v in nulls):
        raise TableError("invalid_mapping", "source.null_values must be a list of strings.", field="source.null_values")
    header_row = source_raw.get("header_row", 1)
    if not isinstance(header_row, int) or isinstance(header_row, bool) or header_row < 1:
        raise TableError("invalid_mapping", "source.header_row must be 1 or more.", field="source.header_row")
    source = {
        "encoding": str(source_raw.get("encoding") or "auto"),
        "delimiter": str(source_raw.get("delimiter") or "auto"),
        "header_row": header_row,
        "null_values": list(dict.fromkeys(nulls)),
        "trim": bool(source_raw.get("trim", True)),
    }

    cls = _absolute_iri(raw.get("class"), "class")
    subject_raw = raw.get("subject") or {}
    if not isinstance(subject_raw, dict):
        raise TableError("invalid_mapping", "subject must be an object.", field="subject")
    subject = {"template": _template(subject_raw.get("template"), "subject.template", columns)}
    graph = raw.get("graph")
    graph = None if graph in (None, "") else _absolute_iri(graph, "graph")
    existing = raw.get("existing", "add")
    if existing not in EXISTING:
        raise TableError("invalid_mapping", f"existing must be one of {', '.join(EXISTING)}.", field="existing")

    rules_raw = raw.get("columns") or []
    if not isinstance(rules_raw, list):
        raise TableError("invalid_mapping", "columns must be a list.", field="columns")
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, rule_raw in enumerate(rules_raw):
        pointer = f"columns[{index}]"
        if not isinstance(rule_raw, dict):
            raise TableError("invalid_mapping", f"{pointer} must be an object.", field=pointer)
        column = rule_raw.get("column")
        if not isinstance(column, str) or not column:
            raise TableError("invalid_mapping", f"{pointer}.column must name a column.", field=f"{pointer}.column")
        if columns is not None and column not in columns:
            raise TableError(
                "unknown_column",
                f"{pointer} maps the column {column!r}, which the table does not have.",
                field=f"{pointer}.column",
                column=column,
            )
        if column in seen:
            raise TableError("invalid_mapping", f"The column {column!r} has two rules.", field=f"{pointer}.column")
        seen.add(column)
        kind = rule_raw.get("kind", "ignore")
        if kind not in KINDS:
            raise TableError(
                "invalid_mapping", f"{pointer}.kind must be one of {', '.join(KINDS)}.", field=f"{pointer}.kind"
            )
        rule: dict[str, Any] = {"column": column, "kind": kind}
        if kind != "ignore":
            rule["property"] = _absolute_iri(rule_raw.get("property"), f"{pointer}.property")
            separator = rule_raw.get("separator")
            if separator not in (None, ""):
                if not isinstance(separator, str):
                    raise TableError(
                        "invalid_mapping", f"{pointer}.separator must be a string.", field=f"{pointer}.separator"
                    )
                rule["separator"] = separator
        if kind == "value":
            datatype, lang = rule_raw.get("datatype"), rule_raw.get("lang")
            if datatype not in (None, "") and lang not in (None, ""):
                raise TableError(
                    "invalid_mapping", f"{pointer}: give a datatype or a language, not both.", field=f"{pointer}.lang"
                )
            if lang not in (None, ""):
                if not isinstance(lang, str) or not _LANG_RE.match(lang):
                    raise TableError(
                        "invalid_mapping", f"{pointer}.lang is not a language tag.", field=f"{pointer}.lang"
                    )
                rule["lang"] = lang
            else:
                rule["datatype"] = (
                    XSD_STRING if datatype in (None, "") else _absolute_iri(datatype, f"{pointer}.datatype")
                )
                if rule["datatype"] == RDF_LANG_STRING:
                    raise TableError(
                        "invalid_mapping",
                        f"{pointer}: rdf:langString needs a language (lang).",
                        field=f"{pointer}.lang",
                    )
        elif kind == "reference":
            target = rule_raw.get("target") or {}
            if not isinstance(target, dict) or ("template" in target) == ("lookup" in target):
                raise TableError(
                    "invalid_mapping",
                    f"{pointer}.target needs exactly one of template or lookup.",
                    field=f"{pointer}.target",
                )
            if "template" in target:
                rule["target"] = {"template": _template(target["template"], f"{pointer}.target.template", columns)}
                if column not in placeholders(rule["target"]["template"]):
                    raise TableError(
                        "invalid_mapping",
                        f"{pointer}.target.template must name its own column {{{column}}}.",
                        field=f"{pointer}.target.template",
                    )
            else:
                lookup = target["lookup"]
                if not isinstance(lookup, dict):
                    raise TableError(
                        "invalid_mapping",
                        f"{pointer}.target.lookup must be an object.",
                        field=f"{pointer}.target.lookup",
                    )
                rule["target"] = {
                    "lookup": {
                        "class": _absolute_iri(lookup.get("class"), f"{pointer}.target.lookup.class"),
                        "property": _absolute_iri(lookup.get("property"), f"{pointer}.target.lookup.property"),
                    }
                }
        rules.append(rule)
    out: dict[str, Any] = {
        "format": FORMAT,
        "name": name,
        "source": source,
        "class": cls,
        "subject": subject,
        "graph": graph,
        "existing": existing,
        "columns": rules,
    }
    return out


# ---- Values ----------------------------------------------------------------------------


def iri_segment(value: str) -> str:
    """Percent-encode a value as an IRI path segment: ASCII outside the unreserved set is
    encoded, non-ASCII letters stay (IRIs allow them); spaces and controls are encoded."""
    out: list[str] = []
    for ch in value:
        if ch.isascii():
            out.append(quote(ch, safe="-._~"))
        elif ch.isspace() or unicodedata.category(ch).startswith("C"):
            out.append(quote(ch, safe=""))
        else:
            out.append(ch)
    return "".join(out)


def expand(template: str, values: dict[str, str | None], base: str) -> str:
    """Expand ``{Column}`` placeholders (RFC 6570 simple expansion) into an absolute IRI.

    An empty value is an error; a relative result is resolved against ``base`` (the
    ontology's minting namespace).
    """
    import pyoxigraph as ox

    def one(match: re.Match[str]) -> str:
        name = match.group(1)
        value = values.get(name)
        if value is None or value == "":
            raise TableError("empty_key", f"{name} is empty", column=name)
        return iri_segment(value)

    iri = _PLACEHOLDER_RE.sub(one, template)
    if not _SCHEME_RE.match(iri):
        iri = base + iri
    try:
        ox.NamedNode(iri)
    except ValueError as exc:
        raise TableError("invalid_iri", f"<{iri}> is not a valid IRI ({exc})") from exc
    return iri


def _xsd_name(datatype: str) -> str | None:
    return datatype[len(XSD) :] if datatype.startswith(XSD) else None


def convert_value(raw: str, datatype: str) -> str:
    """The lexical form of ``raw`` for ``datatype``; ``TableError`` when it does not convert.

    A value that is already a valid lexical form is kept as written; otherwise the
    documented conversions apply (CTR-0247): numbers with group separators (``1,234``)
    or full-width digits, ``YYYY/MM/DD`` dates, ``YYYY/MM/DD hh:mm[:ss]`` date-times,
    ``yes / no / on / off / y / n`` booleans.
    """
    from app.ontology.vocabulary import literal_is_ill_typed

    name = _xsd_name(datatype) or ""
    # XSD numbers, dates and booleans are ASCII; Python's \d also matches full-width
    # digits, so a non-ASCII form is never "already valid" (it is normalized below).
    ascii_only = name in _INTEGER_NAMES or name in _ASCII_TYPES
    if not literal_is_ill_typed(raw, datatype) and (raw.isascii() or not ascii_only):
        return raw
    text = unicodedata.normalize("NFKC", raw).strip()
    candidate: str | None = None
    if name in _INTEGER_NAMES or name in ("decimal", "double", "float"):
        number = text.replace(",", "") if _GROUPED_RE.match(text) else text
        if name in _INTEGER_NAMES:
            try:
                candidate = str(int(number))
            except ValueError:
                candidate = None
        elif name == "decimal":
            try:
                candidate = format(Decimal(number), "f")
            except InvalidOperation:
                candidate = None
        else:
            candidate = number
    elif name == "boolean":
        lowered = text.lower()
        candidate = "true" if lowered in _TRUE else "false" if lowered in _FALSE else None
    elif name == "date":
        m = _DATE_RE.match(text)
        if m:
            candidate = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    elif name == "dateTime":
        m = _DATETIME_RE.match(text)
        if m:
            y, mo, d, h, mi, s = m.groups()
            candidate = f"{y}-{int(mo):02d}-{int(d):02d}T{int(h):02d}:{mi}:{s or '00'}"
    else:
        candidate = text
    if candidate is None or literal_is_ill_typed(candidate, datatype) or (ascii_only and not candidate.isascii()):
        shown = f"xsd:{name}" if name else f"<{datatype}>"
        raise TableError("invalid_value", f"{raw!r} is not a valid {shown}")
    return candidate


# ---- The stored model ------------------------------------------------------------------


@dataclass
class Model:
    quads: list[Any]
    document: dict[str, Any]
    subjects: set[str] = field(default_factory=set)

    @classmethod
    def read(cls, data: bytes) -> Model:
        from app.ontology.vocabulary import read_dataset

        try:
            quads, document = read_dataset(data)
        except ValueError as exc:
            raise TableError("invalid_ontology", f"The stored ontology cannot be read: {exc}") from exc
        model = cls(quads, document)
        model.subjects = {str(q.subject) for q in quads}
        return model

    def values(self, predicate: str) -> list[tuple[Any, Any]]:
        return [(q.subject, q.object) for q in self.quads if q.predicate.value == predicate]


def _lookup_index(model: Model, cls: str, prop: str) -> dict[str, list[str]]:
    """Cell text -> the IRIs of the existing individuals of ``cls`` whose ``prop`` has that value."""
    import pyoxigraph as ox

    members = {
        q.subject.value
        for q in model.quads
        if q.predicate.value == RDF_TYPE
        and isinstance(q.object, ox.NamedNode)
        and q.object.value == cls
        and isinstance(q.subject, ox.NamedNode)
    }
    index: dict[str, list[str]] = {}
    for q in model.quads:
        if q.predicate.value != prop or not isinstance(q.subject, ox.NamedNode) or q.subject.value not in members:
            continue
        obj = q.object
        text = obj.value if isinstance(obj, ox.Literal | ox.NamedNode) else None
        if text is None:
            continue
        found = index.setdefault(text, [])
        if q.subject.value not in found:
            found.append(q.subject.value)
    return index


# ---- Converting rows (D3 / D11) --------------------------------------------------------


@dataclass
class RowResult:
    row: int
    line: int
    cells: list[str]
    subject: str | None = None
    key: str = ""
    quads: list[Any] = field(default_factory=list)
    stage: str | None = None  # convert / existing / shacl / dependency (None = accepted)
    reasons: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    depends_on: list[int] = field(default_factory=list)


def _cell_values(table: Table, record: list[str], mapping: dict[str, Any]) -> tuple[dict[str, str | None], str | None]:
    source = mapping["source"]
    nulls = set(source["null_values"])
    width = len(table.columns)
    extra = [cell for cell in record[width:] if cell.strip()]
    values: dict[str, str | None] = {}
    for index, name in enumerate(table.columns):
        cell = record[index] if index < len(record) else ""
        if source["trim"]:
            cell = cell.strip()
        values[name] = None if cell in nulls else cell
    problem = None
    if extra:
        problem = f"The row has {len(record)} cells; the header has {width}"
    return values, problem


def convert_rows(table: Table, mapping: dict[str, Any], model: Model, base: str) -> list[RowResult]:
    """Every row -> its statements, or a row error (stage ``convert`` / ``existing``)."""
    import pyoxigraph as ox

    graph = ox.NamedNode(mapping["graph"]) if mapping["graph"] else ox.DefaultGraph()
    cls = ox.NamedNode(mapping["class"])
    rdf_type = ox.NamedNode(RDF_TYPE)
    key_names = placeholders(mapping["subject"]["template"])
    lookups: dict[tuple[str, str], dict[str, list[str]]] = {}
    for rule in mapping["columns"]:
        lookup = rule.get("target", {}).get("lookup") if rule["kind"] == "reference" else None
        if lookup:
            pair = (lookup["class"], lookup["property"])
            if pair not in lookups:
                lookups[pair] = _lookup_index(model, *pair)
    out: list[RowResult] = []
    for index, record in enumerate(table.rows):
        result = RowResult(row=index + 1, line=table.lines[index], cells=list(record))
        out.append(result)
        values, problem = _cell_values(table, record, mapping)
        result.key = ", ".join(str(values.get(name) or "") for name in key_names)
        if problem:
            result.stage = "convert"
            result.reasons.append({"message": problem})
            continue
        try:
            result.subject = expand(mapping["subject"]["template"], values, base)
        except TableError as exc:
            result.stage = "convert"
            result.reasons.append({"column": exc.extra.get("column"), "message": f"Subject: {exc.message}"})
            continue
        subject = ox.NamedNode(result.subject)
        quads = [ox.Quad(subject, rdf_type, cls, graph)]
        for rule in mapping["columns"]:
            if rule["kind"] == "ignore":
                continue
            raw = values.get(rule["column"])
            if raw is None:
                continue
            parts = [raw]
            if rule.get("separator"):
                parts = [p.strip() if mapping["source"]["trim"] else p for p in raw.split(rule["separator"])]
                parts = [p for p in parts if p != "" and p not in mapping["source"]["null_values"]]
            predicate = ox.NamedNode(rule["property"])
            for part in parts:
                try:
                    obj = _object(rule, part, values, base, lookups)
                except TableError as exc:
                    result.reasons.append({"column": rule["column"], "value": part, "message": exc.message})
                    continue
                quads.append(ox.Quad(subject, predicate, obj, graph))
        if result.reasons:
            result.stage = "convert"
            continue
        if mapping["existing"] == "skip" and result.subject is not None and f"<{result.subject}>" in model.subjects:
            result.stage = "existing"
            result.reasons.append({"message": f"<{result.subject}> already exists (existing: skip)"})
            continue
        result.quads = list(dict.fromkeys(quads))
    return out


def _object(
    rule: dict[str, Any],
    part: str,
    values: dict[str, str | None],
    base: str,
    lookups: dict[tuple[str, str], dict[str, list[str]]],
) -> Any:
    import pyoxigraph as ox

    if rule["kind"] == "value":
        if "lang" in rule:
            return ox.Literal(part, language=rule["lang"])
        lexical = convert_value(part, rule["datatype"])
        return ox.Literal(lexical, datatype=ox.NamedNode(rule["datatype"]))
    target = rule["target"]
    if "template" in target:
        return ox.NamedNode(expand(target["template"], {**values, rule["column"]: part}, base))
    lookup = target["lookup"]
    found = lookups.get((lookup["class"], lookup["property"]), {}).get(part, [])
    if not found:
        raise TableError("lookup_none", f"No existing individual has the value {part!r}")
    if len(found) > 1:
        raise TableError("lookup_many", f"{len(found)} existing individuals have the value {part!r}")
    return ox.NamedNode(found[0])


def removals(model: Model, mapping: dict[str, Any], rows: list[RowResult]) -> list[Any]:
    """``replace``: the existing values, in the target graph, of the mapped properties an
    accepted row GIVES values for (D11). An empty cell leaves the existing value alone;
    ``rdf:type`` is never replaced."""
    import pyoxigraph as ox

    if mapping["existing"] != "replace":
        return []
    graph = ox.NamedNode(mapping["graph"]) if mapping["graph"] else ox.DefaultGraph()
    given: dict[str, set[str]] = {}
    added: set[Any] = set()
    for row in rows:
        if row.stage is not None or not row.subject:
            continue
        for quad in row.quads:
            added.add(quad)
            if quad.predicate.value != RDF_TYPE:
                given.setdefault(row.subject, set()).add(quad.predicate.value)
    return [
        q
        for q in model.quads
        if isinstance(q.subject, ox.NamedNode)
        and q.predicate.value in given.get(q.subject.value, ())
        and q.graph_name == graph
        and q not in added
    ]


# ---- Validation and attribution (D6-D9) ------------------------------------------------


def _candidate_text(model: Model, rows: list[RowResult], removed: list[Any]) -> tuple[str, bool]:
    from app.ontology.vocabulary import is_named, write_document

    gone = set(removed)
    quads = [q for q in model.quads if q not in gone]
    present = set(quads)
    for row in rows:
        if row.stage is None:
            for quad in row.quads:
                if quad not in present:
                    present.add(quad)
                    quads.append(quad)
    dataset = any(is_named(q) for q in quads)
    return write_document(quads, model.document, trig=dataset), dataset


def _validate(
    ontology_id: str, config: dict[str, Any], entry: dict[str, Any], text: str, dataset: bool
) -> tuple[dict[str, Any], list[dict[str, Any]] | None]:
    from app.ontology import shacl_results, store

    data = text.encode("utf-8")
    key = shacl_results.key_for(ontology_id, config, data=data, dataset=dataset)
    candidate_entry = dict(entry)
    candidate_entry["file"] = f"{ontology_id}{store.TRIG_SUFFIX if dataset else store.TURTLE_SUFFIX}"
    return shacl_results.compute(ontology_id, config, key, data=data, entry=candidate_entry, stored_entailment=False)


def _reason(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "severity": item.get("severity"),
        "focus": (item.get("focus") or {}).get("shown"),
        "path": item.get("path"),
        "value": (item.get("value") or {}).get("shown"),
        "shape": (item.get("shape") or {}).get("shown"),
        "component": item.get("component"),
        "constraint": item.get("constraint"),
        "message": item.get("message"),
    }


def attribute(
    ontology_id: str,
    config: dict[str, Any],
    entry: dict[str, Any],
    model: Model,
    mapping: dict[str, Any],
    rows: list[RowResult],
) -> dict[str, Any]:
    """Validate stored + accepted rows, exclude the rows with Violations, repeat (D6-D9)."""
    from app.ontology import shacl_results

    by_subject: dict[str, list[RowResult]] = {}
    by_object: dict[str, list[RowResult]] = {}
    for row in rows:
        if row.subject is None:
            continue
        by_subject.setdefault(f"<{row.subject}>", []).append(row)
        for quad in row.quads:
            by_object.setdefault(str(quad.object), []).append(row)
    baseline: set[str] | None = None
    rounds = 0
    settled = False
    final: list[dict[str, Any]] = []
    while rounds < MAX_ROUNDS:
        rounds += 1
        text, dataset = _candidate_text(model, rows, removals(model, mapping, rows))
        record, results = _validate(ontology_id, config, entry, text, dataset)
        if record.get("state") != shacl_results.READY or results is None:
            return {
                "checked": False,
                "reason": record.get("state"),
                "message": record.get("message"),
                "rounds": rounds,
            }
        final = results
        excluded_now: set[int] = set()
        for item in results:
            if item.get("severity") != "violation":
                continue
            # A violation the stored model already has is pre-existing: never a row's fault.
            if baseline is None:
                baseline = _baseline_keys(ontology_id, config, entry, model)
            if item.get("key") in baseline:
                continue
            focus = (item.get("focus") or {}).get("nt") or ""
            hit = [r for r in by_subject.get(focus, []) if r.stage is None]
            if not hit and focus not in by_subject:
                hit = [r for r in by_object.get(focus, []) if r.stage is None]
            for row in hit:
                excluded_now.add(row.row)
                reason = _reason(item)
                if reason not in row.reasons:  # one line per distinct result (uniqueLang repeats)
                    row.reasons.append(reason)
                value = (item.get("value") or {}).get("nt")
                for other in by_subject.get(value or "", []):
                    if other.stage is not None and other.row != row.row and other.row not in row.depends_on:
                        row.depends_on.append(other.row)
        if not excluded_now:
            settled = True
            break
        for row in rows:
            if row.row in excluded_now:
                row.stage = "dependency" if row.depends_on else "shacl"
    # Warnings / infos of the accepted rows, pre-existing violations (final round).
    preexisting = 0
    unattributed = 0
    for item in final:
        focus = (item.get("focus") or {}).get("nt") or ""
        rows_hit = [r for r in by_subject.get(focus, []) if r.stage is None]
        if item.get("severity") != "violation":
            reason = _reason(item)
            for row in rows_hit:
                if reason not in row.warnings:
                    row.warnings.append(reason)
            continue
        if baseline is not None and item.get("key") in baseline:
            preexisting += 1
        elif not rows_hit and focus not in by_subject and not by_object.get(focus):
            unattributed += 1
    return {
        "checked": True,
        "rounds": rounds,
        "settled": settled,
        "preexisting": preexisting,
        "unattributed": unattributed,
    }


def _baseline_keys(ontology_id: str, config: dict[str, Any], entry: dict[str, Any], model: Model) -> set[str]:
    """The result keys of the stored model, serialized the same way as the candidates."""
    from app.ontology.vocabulary import is_named, write_document

    dataset = any(is_named(q) for q in model.quads)
    text = write_document(model.quads, model.document, trig=dataset)
    _, results = _validate(ontology_id, config, entry, text, dataset)
    return {str(item.get("key")) for item in results or []}


# ---- Entry points ----------------------------------------------------------------------


def _statement_json(quad: Any) -> dict[str, Any]:
    from app.ontology.vocabulary import is_named, term_to_json

    out = {"s": term_to_json(quad.subject), "p": quad.predicate.value, "o": term_to_json(quad.object)}
    if is_named(quad):
        out["g"] = term_to_json(quad.graph_name)
    return out


def _read_stored(ontology_id: str) -> tuple[dict[str, Any], bytes]:
    from app.ontology import store

    pair = store.read_entry_and_bytes(ontology_id)
    if pair is None:
        raise KeyError(ontology_id)
    return pair


def inspect(
    ontology_id: str,
    data: bytes,
    filename: str,
    *,
    encoding: str = "auto",
    delimiter: str = "auto",
    header_row: int = 1,
    class_iri: str | None = None,
    max_rows: int | None = None,
) -> dict[str, Any]:
    """The file's encoding, delimiter, columns, row count and first rows; a suggested
    mapping when ``class_iri`` is given (D4: suggestions only pre-fill)."""
    from app.ontology.vocabulary import base_iri_for

    table = read_table(data, filename, encoding=encoding, delimiter=delimiter, header_row=header_row, max_rows=max_rows)
    out: dict[str, Any] = {
        "encoding": table.encoding,
        "delimiter": table.delimiter,
        "header_row": table.header_row,
        "columns": table.columns,
        "row_count": len(table.rows),
        "sample": [
            {"row": i + 1, "line": table.lines[i], "cells": table.rows[i]}
            for i in range(min(SAMPLE_ROWS, len(table.rows)))
        ],
        "suggestion": None,
    }
    if class_iri:
        cls = _absolute_iri(class_iri, "class")
        _, stored = _read_stored(ontology_id)
        model = Model.read(stored)
        out["suggestion"] = suggest(model, cls, table, base_iri_for(ontology_id))
    return out


def preview(
    ontology_id: str,
    data: bytes,
    filename: str,
    mapping_raw: Any,
    revision: str | None,
    *,
    max_rows: int | None = None,
    validate: bool = True,
    unchecked: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert, validate and attribute; the statements to add and the row report (CTR-0247).

    ``validate=False`` converts only and reports ``unchecked`` as the SHACL state (the
    validation did not finish, D9: importing then needs an explicit choice).
    """
    from app.ontology import store
    from app.ontology.vocabulary import base_iri_for

    entry, stored = _read_stored(ontology_id)
    current = store.revision_of(stored)
    if revision and revision != current:
        raise TableError(
            "stale_revision", "The ontology was saved elsewhere; reload it and preview again.", revision=current
        )
    if not isinstance(mapping_raw, dict):
        raise TableError("invalid_mapping", "The mapping must be a JSON object.", field="")
    source = mapping_raw.get("source") if isinstance(mapping_raw.get("source"), dict) else {}
    header_row = source.get("header_row", 1)
    if not isinstance(header_row, int) or isinstance(header_row, bool):
        header_row = 1
    table = read_table(
        data,
        filename,
        encoding=str(source.get("encoding") or "auto"),
        delimiter=str(source.get("delimiter") or "auto"),
        header_row=header_row,
        max_rows=max_rows,
    )
    mapping = validate_mapping(mapping_raw, table.columns)
    model = Model.read(stored)
    rows = convert_rows(table, mapping, model, base_iri_for(ontology_id))
    config = store.shacl_config(entry)
    if not validate:
        shacl: dict[str, Any] = dict(unchecked or {"checked": False, "reason": "skipped"})
    elif config is None:
        shacl = {
            "checked": False,
            "reason": "not_configured",
            "message": "SHACL is not configured for this ontology; only the conversion was checked.",
        }
    else:
        shacl = attribute(ontology_id, config, entry, model, mapping, rows)
    accepted = [r for r in rows if r.stage is None]
    removed = removals(model, mapping, rows)
    stored_set = set(model.quads)
    gone = set(removed)
    adds: list[Any] = []
    seen: set[Any] = set()
    present = 0
    for row in accepted:
        for quad in row.quads:
            if quad in seen:
                continue
            seen.add(quad)
            if quad in stored_set and quad not in gone:
                present += 1
                continue
            adds.append(quad)
    new_subjects = {r.subject for r in accepted if f"<{r.subject}>" not in model.subjects}
    existing_subjects = {r.subject for r in accepted} - new_subjects
    return {
        "format": "chatwalaau.table-import-report/1",
        "revision": current,
        "mapping": mapping,
        "table": {
            "encoding": table.encoding,
            "delimiter": table.delimiter,
            "columns": table.columns,
        },
        "rows": {
            "total": len(rows),
            "accepted": len(accepted),
            "excluded": len(rows) - len(accepted),
            "with_warnings": sum(1 for r in accepted if r.warnings),
        },
        "subjects": {"new": len(new_subjects), "existing": len(existing_subjects)},
        "add": [_statement_json(q) for q in adds],
        "remove": [_statement_json(q) for q in removed],
        "present": present,
        "excluded": [
            {
                "row": r.row,
                "line": r.line,
                "key": r.key,
                "subject": r.subject,
                "stage": r.stage,
                "reasons": r.reasons,
                "depends_on": r.depends_on,
                "cells": r.cells,
            }
            for r in rows
            if r.stage is not None
        ],
        "warnings": [
            {"row": r.row, "line": r.line, "key": r.key, "subject": r.subject, "reasons": r.warnings}
            for r in accepted
            if r.warnings
        ],
        "shacl": shacl,
    }


# ---- Suggestions (D4) ------------------------------------------------------------------


def _fold(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\s_\-.:/]+", "", folded)


def _local(iri: str) -> str:
    from app.ontology.vocabulary import local_name

    return local_name(iri)


def _superclasses(model: Model, cls: str) -> list[str]:
    import pyoxigraph as ox

    parents: dict[str, set[str]] = {}
    for q in model.quads:
        if (
            q.predicate.value == f"{RDFS}subClassOf"
            and isinstance(q.subject, ox.NamedNode)
            and isinstance(q.object, ox.NamedNode)
        ):
            parents.setdefault(q.subject.value, set()).add(q.object.value)
    out, todo = [cls], [cls]
    while todo:
        for parent in parents.get(todo.pop(), set()):
            if parent not in out:
                out.append(parent)
                todo.append(parent)
    return out


def suggest(model: Model, cls: str, table: Table, base: str) -> dict[str, Any]:
    """A mapping pre-filled from the schema: properties of the class (``rdfs:domain`` or a
    SHACL property shape of a node shape targeting it) matched by label / local name."""
    import pyoxigraph as ox

    classes = set(_superclasses(model, cls))
    labels: dict[str, set[str]] = {}
    info: dict[str, dict[str, Any]] = {}
    declared: set[str] = set()
    property_types = {f"{OWL}DatatypeProperty", f"{OWL}ObjectProperty", f"{RDF}Property", f"{OWL}AnnotationProperty"}
    object_props: set[str] = set()
    ranges: dict[str, str] = {}
    preferred: list[str] = []
    datatypes: set[str] = {f"{RDFS}Literal", RDF_LANG_STRING, f"{RDF}HTML", f"{RDF}XMLLiteral", f"{RDF}JSON"}
    for q in model.quads:
        s, p, o = q.subject, q.predicate.value, q.object
        if not isinstance(s, ox.NamedNode):
            continue
        if p == RDF_TYPE and isinstance(o, ox.NamedNode) and o.value in property_types:
            declared.add(s.value)
            if o.value == f"{OWL}ObjectProperty":
                object_props.add(s.value)
        elif p == RDF_TYPE and isinstance(o, ox.NamedNode) and o.value == f"{RDFS}Datatype":
            datatypes.add(s.value)
        elif p == f"{RDFS}domain" and isinstance(o, ox.NamedNode) and o.value in classes:
            preferred.append(s.value)
        elif p == f"{RDFS}range" and isinstance(o, ox.NamedNode):
            ranges[s.value] = o.value
        elif p in (f"{RDFS}label", f"{SKOS}prefLabel") and isinstance(o, ox.Literal):
            labels.setdefault(s.value, set()).add(o.value)
    # SHACL: node shapes targeting the class -> sh:property -> sh:path / sh:datatype / sh:class.
    targeting = {
        str(q.subject)
        for q in model.quads
        if q.predicate.value == f"{SH}targetClass" and isinstance(q.object, ox.NamedNode) and q.object.value in classes
    }
    shape_props = {
        str(q.object) for q in model.quads if q.predicate.value == f"{SH}property" and str(q.subject) in targeting
    }
    for q in model.quads:
        node = str(q.subject)
        if node not in shape_props:
            continue
        p, o = q.predicate.value, q.object
        if p == f"{SH}path" and isinstance(o, ox.NamedNode):
            info.setdefault(node, {})["path"] = o.value
        elif p == f"{SH}datatype" and isinstance(o, ox.NamedNode):
            info.setdefault(node, {})["datatype"] = o.value
        elif p == f"{SH}class" and isinstance(o, ox.NamedNode):
            info.setdefault(node, {})["class"] = o.value
        elif p == f"{SH}name" and isinstance(o, ox.Literal):
            info.setdefault(node, {}).setdefault("names", set()).add(o.value)
    shape_by_path: dict[str, dict[str, Any]] = {}
    for item in info.values():
        path = item.get("path")
        if not path:
            continue
        merged = shape_by_path.setdefault(path, {})
        for k, v in item.items():
            if k == "names":
                merged.setdefault("names", set()).update(v)
            else:
                merged.setdefault(k, v)
        preferred.append(path)
    pool = list(dict.fromkeys([*preferred, *sorted(declared)]))
    # Existing language of langString values (for a language suggestion).
    langs: dict[str, str] = {}
    for q in model.quads:
        if isinstance(q.object, ox.Literal) and q.object.language and q.predicate.value not in langs:
            langs[q.predicate.value] = q.object.language

    def names_of(prop: str) -> set[str]:
        out = {_fold(_local(prop))}
        out |= {_fold(v) for v in labels.get(prop, set())}
        out |= {_fold(v) for v in shape_by_path.get(prop, {}).get("names", set())}
        return {n for n in out if n}

    folded_pool = [(prop, names_of(prop)) for prop in pool]
    used: set[str] = set()
    rules: list[dict[str, Any]] = []
    for column in table.columns:
        key = _fold(column)
        match = next((prop for prop, names in folded_pool if key and key in names and prop not in used), None)
        if match is None:
            rules.append({"column": column, "kind": "ignore"})
            continue
        used.add(match)
        shape = shape_by_path.get(match, {})
        rng = ranges.get(match) or ""
        literal_range = rng.startswith(XSD) or rng in datatypes
        datatype = shape.get("datatype") or (rng if rng.startswith(XSD) else None)
        if rng == RDF_LANG_STRING or datatype == RDF_LANG_STRING:
            rules.append({"column": column, "kind": "value", "property": match, "lang": langs.get(match, "en")})
            continue
        target_cls = shape.get("class") or (rng if rng and not literal_range else None)
        if not shape.get("datatype") and (target_cls or match in object_props):
            local = _local(target_cls or "item")
            rules.append(
                {
                    "column": column,
                    "kind": "reference",
                    "property": match,
                    "target": {"template": f"{base}{local.lower()}-{{{column}}}"},
                }
            )
            continue
        rules.append({"column": column, "kind": "value", "property": match, "datatype": datatype or XSD_STRING})
    key_column = table.columns[0] if table.columns else "ID"
    return {
        "format": FORMAT,
        "name": "",
        "source": {
            "encoding": table.encoding,
            "delimiter": table.delimiter,
            "header_row": table.header_row,
            "null_values": [""],
            "trim": True,
        },
        "class": cls,
        "subject": {"template": f"{base}{_local(cls).lower()}-{{{key_column}}}"},
        "graph": None,
        "existing": "add",
        "columns": rules,
    }
