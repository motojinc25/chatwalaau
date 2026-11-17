"""Knowledge scope of a declarative agent (CTR-0142 v3, PRP-0256, UDR-0237 D3/D4/D6).

An agent YAML may limit where its tools search and how it answers:

```yaml
metadata:
  chatwalaau:
    knowledge:
      rag_collections: [hr-handbook, hr-faq]   # the first is the default collection
      ontologies: [hr-rules]                   # ids or exact names
      grounding: strict                        # off (default) | strict
      max_distance: 1.2                        # optional (L2); strict drops farther hits
      web_search: false                        # default false: no provider web search
```

The block lives under ``metadata.chatwalaau`` because the MAF declarative ``PromptAgent``
rejects an unknown top-level key, while ``metadata`` is part of its schema: a desk YAML
stays loadable by MAF tooling. A top-level ``knowledge`` key is a warning that says so.

The scope is applied at the tool-assembly chokepoint (``app.agui.agent_factory``), so
every lane that runs the spec gets it. ``grounding`` is read only by the /v1/responses
gate of a key-bound agent (CTR-0287).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

GROUNDING_MODES: tuple[str, ...] = ("off", "strict")
KNOWLEDGE_KEYS: tuple[str, ...] = ("rag_collections", "ontologies", "grounding", "max_distance", "web_search")
#: The path of the block inside the YAML, for messages.
KNOWLEDGE_PATH = "metadata.chatwalaau.knowledge"


@dataclass(frozen=True)
class KnowledgeScope:
    rag_collections: tuple[str, ...] = ()
    ontologies: tuple[str, ...] = ()
    grounding: str = "off"
    max_distance: float | None = None
    web_search: bool = False

    @property
    def strict(self) -> bool:
        return self.grounding == "strict"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rag_collections": list(self.rag_collections),
            "ontologies": list(self.ontologies),
            "grounding": self.grounding,
            "max_distance": self.max_distance,
            "web_search": self.web_search,
        }


def _names(raw: Any, key: str, warnings: list[str]) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        warnings.append(f"{KNOWLEDGE_PATH}.{key} ignored (expected a list of names).")
        return ()
    seen: set[str] = set()
    return tuple(name.strip() for name in raw if not (name.strip() in seen or seen.add(name.strip())))


def raw_knowledge(data: dict[str, Any]) -> Any:
    """The raw block under metadata.chatwalaau.knowledge, or None."""
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        return None
    ours = meta.get("chatwalaau")
    if not isinstance(ours, dict):
        return None
    return ours.get("knowledge")


def map_knowledge(data: dict[str, Any], warnings: list[str]) -> KnowledgeScope | None:
    """Map the knowledge block; structural problems are warnings (they block activation)."""
    if "knowledge" in data:
        warnings.append(f"knowledge must be written under {KNOWLEDGE_PATH}, not at the top level.")
    raw = raw_knowledge(data)
    if raw is None:
        return None
    if not isinstance(raw, dict):
        warnings.append(f"{KNOWLEDGE_PATH} ignored (expected a mapping).")
        return None
    unknown = sorted(set(raw) - set(KNOWLEDGE_KEYS))
    if unknown:
        warnings.append(f"{KNOWLEDGE_PATH}: unknown key(s) {unknown}; use {', '.join(KNOWLEDGE_KEYS)}.")

    collections = _names(raw.get("rag_collections"), "rag_collections", warnings)
    ontologies = _names(raw.get("ontologies"), "ontologies", warnings)

    grounding = raw.get("grounding", "off")
    if grounding not in GROUNDING_MODES:
        warnings.append(f"{KNOWLEDGE_PATH}.grounding must be one of {', '.join(GROUNDING_MODES)}.")
        grounding = "off"

    max_distance = raw.get("max_distance")
    if max_distance is not None and (
        isinstance(max_distance, bool) or not isinstance(max_distance, int | float) or max_distance <= 0
    ):
        warnings.append(f"{KNOWLEDGE_PATH}.max_distance must be a number above 0.")
        max_distance = None

    web_search = raw.get("web_search", False)
    if not isinstance(web_search, bool):
        warnings.append(f"{KNOWLEDGE_PATH}.web_search must be true or false.")
        web_search = False

    if grounding == "strict" and not collections:
        warnings.append(f"{KNOWLEDGE_PATH}.grounding strict needs at least one rag_collections entry.")
    if not collections and not ontologies:
        warnings.append(f"{KNOWLEDGE_PATH} names no rag_collections and no ontologies.")

    return KnowledgeScope(
        rag_collections=collections,
        ontologies=ontologies,
        grounding=str(grounding),
        max_distance=float(max_distance) if max_distance is not None else None,
        web_search=web_search,
    )


def allows_web_search(spec: Any) -> bool:
    """False for a spec with a knowledge scope that did not set web_search: true (D4)."""
    scope = getattr(spec, "knowledge", None)
    return scope is None or scope.web_search


def check_tools(scope: KnowledgeScope | None, tool_allowlist: list[str] | None, warnings: list[str]) -> None:
    """A named source whose tool the allow-list leaves out can never be searched."""
    if scope is None or tool_allowlist is None:
        return
    if scope.rag_collections and "function:rag_search" not in tool_allowlist:
        warnings.append(f"{KNOWLEDGE_PATH} names rag_collections but tools does not list rag_search.")
    if scope.ontologies and "function:ontology_query" not in tool_allowlist:
        warnings.append(f"{KNOWLEDGE_PATH} names ontologies but tools does not list ontology_query.")


def document_knowledge(data: dict[str, Any]) -> dict[str, Any] | None:
    """The block reshaped for the authoring form (CTR-0177), or None. Tolerant."""
    raw = raw_knowledge(data)
    if not isinstance(raw, dict):
        return None
    scope = map_knowledge({"metadata": {"chatwalaau": {"knowledge": raw}}}, [])
    return scope.to_dict() if scope is not None else None


def knowledge_yaml(document: Any) -> dict[str, Any] | None:
    """The canonical block written by the authoring serializer, or None when empty."""
    if not isinstance(document, dict):
        return None
    collections = [str(c).strip() for c in document.get("rag_collections") or [] if str(c).strip()]
    ontologies = [str(o).strip() for o in document.get("ontologies") or [] if str(o).strip()]
    if not collections and not ontologies:
        return None
    out: dict[str, Any] = {}
    if collections:
        out["rag_collections"] = collections
    if ontologies:
        out["ontologies"] = ontologies
    grounding = str(document.get("grounding") or "off")
    out["grounding"] = grounding if grounding in GROUNDING_MODES else "off"
    max_distance = document.get("max_distance")
    if isinstance(max_distance, int | float) and not isinstance(max_distance, bool) and max_distance > 0:
        out["max_distance"] = float(max_distance)
    if document.get("web_search") is True:
        out["web_search"] = True
    return out


__all__ = [
    "GROUNDING_MODES",
    "KNOWLEDGE_KEYS",
    "KNOWLEDGE_PATH",
    "KnowledgeScope",
    "allows_web_search",
    "check_tools",
    "document_knowledge",
    "knowledge_yaml",
    "map_knowledge",
    "raw_knowledge",
]
