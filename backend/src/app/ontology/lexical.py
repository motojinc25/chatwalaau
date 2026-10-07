"""Faithful query results: map Store output back to the file's terms (CTR-0170 v2, UDR-0181).

The read-only executor runs on an in-memory ``pyoxigraph.Store`` (UDR-0084 D3/D7).
The Store keeps typed XSD values in a value encoding, so a result shows the
NORMALIZED lexical form (``"01"^^xsd:integer`` -> ``1``), and triples that differ
only in lexical form are merged (``"01"``, ``"1"``, ``"+1"`` -> one triple).

This module keeps a lexical index over the SAME parse that fills the Store and
restores results to what the file says (UDR-0181 D2):

- the normalized form of a literal / triple term is obtained from the engine
  itself (a scratch Store), never from a hand-written canonicalizer, so the index
  follows engine upgrades. Literals of ``xsd:string``, ``rdf:langString`` and
  ``rdf:dirLangString`` are stored verbatim by the engine (asserted by the
  invariant suite) and are not sent through it;
- a CONSTRUCT / DESCRIBE triple that is in the file expands back into ALL its
  original triples; any other literal is restored only when exactly one original
  exists, otherwise it stays normalized and is reported (``lexical_forms``,
  ``ambiguous_values``);
- the index is built on first use, so a query whose results hold no typed value
  (ASK, IRIs, labels) never pays for it.

What cannot be restored is stated in ``notices`` (UDR-0181 D3).
"""

from __future__ import annotations

import re
import threading
from typing import Any

# String functions see the Store's normalized lexical form (UDR-0181 D3).
_STRING_FUNCTION_RE = re.compile(
    r"\b(STR|REGEX|STRLEN|SUBSTR|CONTAINS|STRSTARTS|STRENDS|STRBEFORE|STRAFTER|REPLACE)\s*\(",
    re.IGNORECASE,
)
# ``(expr AS ?v)`` in a projection or ``BIND(expr AS ?v)``: the query computes ?v,
# so its value is never a file term (a COUNT of 1 is not the file's "01").
_COMPUTED_VARIABLE_RE = re.compile(r"\bAS\s+[?$]([A-Za-z0-9_]+)", re.IGNORECASE)

# Datatypes the engine stores verbatim (no value encoding changes their lexical form).
VERBATIM_DATATYPES = frozenset(
    {
        "http://www.w3.org/2001/XMLSchema#string",
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString",
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#dirLangString",
    }
)
_SCRATCH_SUBJECT = "urn:x-chatwalaau:lexical:"
_SCRATCH_PREDICATE = "urn:x-chatwalaau:lexical#value"


def is_restorable(term: Any) -> bool:
    """A term whose lexical form the engine may change (typed literal or triple term)."""
    import pyoxigraph as ox

    if isinstance(term, ox.Literal):
        return term.datatype.value not in VERBATIM_DATATYPES
    return isinstance(term, ox.Triple)


def _walk(term: Any, out: set[Any]) -> None:
    """Collect a restorable term and every restorable term nested in a triple term."""
    import pyoxigraph as ox

    if isinstance(term, ox.Triple):
        out.add(term)
        _walk(term.subject, out)
        _walk(term.object, out)
    elif is_restorable(term):
        out.add(term)


def normalize(terms: list[Any]) -> dict[Any, Any]:
    """original -> the term as the engine stores it (engine-derived, UDR-0181 D2)."""
    import pyoxigraph as ox

    if not terms:
        return {}
    scratch = ox.Store()
    predicate = ox.NamedNode(_SCRATCH_PREDICATE)
    scratch.extend(ox.Quad(ox.NamedNode(f"{_SCRATCH_SUBJECT}{i}"), predicate, term) for i, term in enumerate(terms))
    offset = len(_SCRATCH_SUBJECT)
    return {terms[int(quad.subject.value[offset:])]: quad.object for quad in scratch}


def _kind(term: Any) -> str:
    """The bucket a restorable term can match in: its datatype, or ``triple``."""
    import pyoxigraph as ox

    return term.datatype.value if isinstance(term, ox.Literal) else "triple"


class LexicalIndex:
    """Normalized -> original lookup for one parsed ontology file.

    Built on demand and only as far as a result needs it: CONSTRUCT restoration
    normalizes the file objects of the (subject, predicate) pairs in the result;
    SELECT restoration normalizes the file terms of the result's datatypes.

    A cached index is shared by concurrent queries (UDR-0183 D2): the lookup tables
    fill under ``_lock``, and what they describe -- the parsed file -- never changes.
    """

    def __init__(self, kept: list[Any], *, file_triples: int, store_triples: int) -> None:
        # ``kept`` = the file's triples whose object is restorable; every other object
        # is stored verbatim and needs no mapping.
        self._kept = kept
        self.file_triples = file_triples
        self.store_triples = store_triples
        self._norm: dict[Any, Any] = {}  # original -> normalized (cache)
        self._by_pair: dict[tuple[Any, Any], list[Any]] | None = None
        self._by_kind: dict[str, list[Any]] | None = None
        self._by_value: dict[str, dict[Any, list[Any]]] = {}
        self._lock = threading.RLock()

    @property
    def merged(self) -> int:
        return max(0, self.file_triples - self.store_triples)

    def _normalized(self, terms: list[Any]) -> dict[Any, Any]:
        with self._lock:
            missing = [t for t in dict.fromkeys(terms) if t not in self._norm]
            if missing:
                self._norm.update(normalize(missing))
            return self._norm

    def _pairs(self) -> dict[tuple[Any, Any], list[Any]]:
        with self._lock:
            if self._by_pair is None:
                by_pair: dict[tuple[Any, Any], list[Any]] = {}
                for triple in self._kept:
                    by_pair.setdefault((triple.subject, triple.predicate), []).append(triple.object)
                self._by_pair = by_pair
            return self._by_pair

    def _kinds(self) -> dict[str, list[Any]]:
        with self._lock:
            if self._by_kind is None:
                terms: set[Any] = set()
                for triple in self._kept:
                    _walk(triple.object, terms)
                by_kind: dict[str, list[Any]] = {}
                for term in terms:
                    by_kind.setdefault(_kind(term), []).append(term)
                self._by_kind = by_kind
            return self._by_kind

    def prepare_statements(self, triples: list[Any]) -> None:
        """Normalize, in one batch, the file objects of every (s, p) in ``triples``."""
        pairs = self._pairs()
        wanted: list[Any] = []
        for triple in triples:
            wanted.extend(pairs.get((triple.subject, triple.predicate), ()))
        self._normalized(wanted)

    def statement_originals(self, subject: Any, predicate: Any, normalized: Any) -> list[Any]:
        """The file objects of (subject, predicate) the engine stores as ``normalized``."""
        objects = self._pairs().get((subject, predicate), [])
        norm = self._normalized(objects)
        return sorted((o for o in objects if norm.get(o, o) == normalized), key=str)

    def value_candidates(self, term: Any) -> list[Any]:
        """Every file term of the same kind the engine stores as ``term``."""
        kind = _kind(term)
        with self._lock:
            if kind not in self._by_value:
                terms = self._kinds().get(kind, [])
                norm = self._normalized(terms)
                index: dict[Any, list[Any]] = {}
                for original in terms:
                    index.setdefault(norm.get(original, original), []).append(original)
                for bucket in index.values():
                    if len(bucket) > 1:
                        bucket.sort(key=str)
                self._by_value[kind] = index
            return self._by_value[kind].get(term, [])

    def has_normalized_terms(self) -> bool:
        """Whether the engine changes the lexical form of any term of the file."""
        terms = [t for bucket in self._kinds().values() for t in bucket]
        norm = self._normalized(terms)
        return any(norm.get(t, t) != t for t in terms)


def build_index(kept: list[Any], *, file_triples: int, store_triples: int) -> LexicalIndex:
    """The (lazy) index over ``kept`` (triples with a restorable object).

    ``file_triples`` is the number of triples parsed from the file and
    ``store_triples`` the Store's size after loading them.
    """
    return LexicalIndex(kept, file_triples=file_triples, store_triples=store_triples)


def computed_variables(sparql: str) -> set[str]:
    """Names of the variables the query computes itself (never restored)."""
    return set(_COMPUTED_VARIABLE_RE.findall(sparql))


def restore_term(index: LexicalIndex, term: Any) -> tuple[Any, list[str] | None]:
    """(file term, None) when unique; (term as given, candidate forms) when ambiguous."""
    import pyoxigraph as ox

    if term is None or not is_restorable(term):
        return term, None
    candidates = index.value_candidates(term)
    if not candidates:
        return term, None  # not in the file (computed or template-built): nothing to restore
    if len(candidates) == 1:
        return candidates[0], None
    return term, [c.value if isinstance(c, ox.Literal) else str(c) for c in candidates]


def restore_triples(index: LexicalIndex, triples: list[Any]) -> tuple[list[Any], int]:
    """Expand CONSTRUCT / DESCRIBE output to the file's triples; returns (triples, ambiguous)."""
    import pyoxigraph as ox

    index.prepare_statements([t for t in triples if is_restorable(t.object)])
    out: dict[Any, None] = {}
    ambiguous = 0
    for triple in triples:
        if not is_restorable(triple.object):
            out.setdefault(triple, None)
            continue
        originals = index.statement_originals(triple.subject, triple.predicate, triple.object)
        if originals:
            for obj in originals:
                out.setdefault(ox.Triple(triple.subject, triple.predicate, obj), None)
            continue
        obj, forms = restore_term(index, triple.object)
        if forms:
            ambiguous += 1
        out.setdefault(ox.Triple(triple.subject, triple.predicate, obj), None)
    return list(out), ambiguous


def notices(index: LexicalIndex, sparql: str, ambiguous: int) -> list[dict[str, Any]]:
    """The differences that remain after restoration (UDR-0181 D3); empty when none."""
    out: list[dict[str, Any]] = []
    if index.merged:
        out.append(
            {
                "code": "merged_values",
                "message": (
                    f"The query engine stores {index.store_triples} triples for the file's "
                    f"{index.file_triples}: values that differ only in how they are written "
                    '(for example "01" and "1" as integers) count once in this query.'
                ),
                "file_triples": index.file_triples,
                "store_triples": index.store_triples,
            }
        )
    if ambiguous:
        out.append(
            {
                "code": "ambiguous_values",
                "message": (
                    f"{ambiguous} value(s) are written in more than one way in the file and are "
                    "shown in the query engine's normalized form."
                ),
                "count": ambiguous,
            }
        )
    if _STRING_FUNCTION_RE.search(sparql) and index.has_normalized_terms():
        out.append(
            {
                "code": "string_functions_normalized",
                "message": (
                    "String functions (STR, REGEX, ...) see the query engine's normalized form of "
                    'typed values (for example "01"^^xsd:integer is seen as "1").'
                ),
            }
        )
    return out


__all__ = [
    "VERBATIM_DATATYPES",
    "LexicalIndex",
    "build_index",
    "computed_variables",
    "is_restorable",
    "normalize",
    "notices",
    "restore_term",
    "restore_triples",
]
