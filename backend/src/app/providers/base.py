"""Base-model provider Protocol (CTR-0102, PRP-0069, UDR-0045).

A Provider owns everything that differs between base-model providers:
how to construct the MAF ``ChatClient`` for a model (including
``(provider x hosting)`` credential / endpoint resolution, resolved from the
model's catalog offering; PRP-0113 / UDR-0094), the per-model GENERATION
OPTION CATALOG (the set of knobs the
model accepts: reasoning effort plus, where supported, verbosity; PRP-0081 /
UDR-0057), the per-model generation ``default_options`` resolved from a
selection, and the provider-supplied hosted web search tool (when any).

No new in-house seam is introduced: the returned chat client is a MAF
``ChatClient`` (BaseChatClient), reused exactly as ``OpenAIChatClient`` and
``DemoChatClient`` already are (UDR-0045 D1/D9).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# One INFO line per (offering, capability) per process (UDR-0112 D5).
_logged_withheld: set[str] = set()


@runtime_checkable
class Provider(Protocol):
    """Provider seam consumed by ``app.providers`` dispatch and CTR-0070."""

    name: str

    # Whether this provider supports OpenAI-style background responses
    # (Responses API ``background=true`` + continuation_token resume,
    # CTR-0045). Azure OpenAI supports it; Anthropic does not. Consumed by
    # the GET /api/model background_supported_map (CTR-0041) and the AG-UI
    # run-option guard (CTR-0045), and surfaced in the UI to disable the
    # Background toggle for non-supporting models (PRP-0073).
    supports_background: bool

    def build_chat_client(self, model: str) -> Any:
        """Construct the MAF ChatClient for ``model`` (credential owned here)."""
        ...

    def model_options_catalog(self, model: str) -> dict[str, Any]:
        """Generation option catalog for ``model`` (PRP-0081, UDR-0057 D2).

        Returns ``{"options": [descriptor, ...]}`` where each descriptor is one
        of:

          - ``{"key", "kind": "enum",   "allowed": [...], "default": ...}``
          - ``{"key", "kind": "number", "min", "max", "step", "default"}``

        Each provider advertises ONLY the knobs the model actually accepts, so
        the UI renders nothing unsupported (UDR-0057 D2). Classic sampling params
        (temperature / top_p / top_k) MUST NOT be advertised for adaptive-thinking
        / reasoning-only models; the ``number`` kind is reserved for a future
        non-reasoning model (UDR-0057 D3).
        """
        ...

    def reasoning_catalog(self, model: str) -> dict[str, Any]:
        """Reasoning effort catalog for ``model`` (PRP-0071, UDR-0047 D2).

        Returns ``{"allowed": [<effort>, ...], "default": <effort>}``, the
        ``effort`` axis of :meth:`model_options_catalog`. Retained as a derived
        back-compat view for the GET /api/model ``reasoning_options`` map
        (CTR-0069); the generalized catalog is the source of truth.
        """
        ...

    def build_model_options(self, model: str, selected: dict[str, Any] | None = None) -> dict[str, Any]:
        """Per-model Agent ``default_options`` (provider-specific shape).

        ``selected`` maps advertised option keys to chosen values
        (e.g. ``{"effort": "high", "verbosity": "low"}``). ``None``, missing, or
        invalid / out-of-range values resolve to the catalog default (UDR-0057
        D7). A resolved value equal to its default is OMITTED from the request so
        the un-changed path stays byte-for-byte (output-neutral default, UDR-0057
        D6).
        """
        ...

    def web_search_tool(self, model: str) -> Any | None:
        """Provider-supplied hosted web search tool, or None when unsupplied.

        Returning None is the ONE gate for hosted web search (UDR-0112 D3): the
        caller (``agui.agent_factory``) uses this single value both to attach the
        tool AND to append ``WEB_SEARCH_INSTRUCTION`` to the system prompt. A tool
        removed anywhere else would leave the agent instructed to search and cite
        sources while holding no search tool.

        An implementation that supplies a hosted tool MUST first consult
        :func:`hosted_tool_withheld`: availability can depend on the DEPLOYMENT (its
        endpoint / workspace), not only on the provider, and one provider class may
        serve several (UDR-0112 D1).
        """
        ...

    def structured_output_support(self, model: str) -> dict[str, Any]:
        """Per-model structured-output capability (CTR-0102 v5, UDR-0058 D1/D6).

        Returns ``{"supported": bool, "native": bool, "fallback": str}`` where
        ``fallback`` is ``"forced_tool_use"`` or ``"none"``. ``native`` is True when
        the model accepts the provider's first-class structured-output request
        (OpenAI ``text.format`` json_schema / Anthropic ``output_config.format``);
        when False the model still produces JSON via the forced-tool-use fallback
        (UDR-0058 D2). Published per model by GET /api/model (CTR-0069 v5) so the UI
        renders strictly what is advertised.
        """
        ...

    def build_structured_output(self, model: str, schema: dict[str, Any] | None, mode: str) -> dict[str, Any]:
        """Per-request structured-output run-options fragment (CTR-0102 v5, UDR-0058 D2/D3).

        ``mode`` is ``"json_schema"`` (use the explicit ``schema`` verbatim) or
        ``"json_object"`` (generic permissive / free-JSON when no schema is supplied,
        UDR-0058 D3). The returned dict is merged into the ``agent.run`` options:

          - native models -> the provider's first-class shape (OpenAI
            ``{"text": {"format": {...}}}`` / Anthropic
            ``{"output_config": {"format": {...}}}``).
          - non-native models -> a forced-tool-use fragment
            (``{"tools": [...], "tool_choice": {...}}``) constraining the answer to
            the schema (UDR-0058 D2).

        Returns ``{}`` when structured output is not requested. MUST NOT raise: a bad
        or oversized schema resolves to the generic object mode (UDR-0058 D2/D4).
        """
        ...


# ---------------------------------------------------------------------------
# Hosted-tool capability gate (PRP-0129, UDR-0112)
# ---------------------------------------------------------------------------
def hosted_tool_withheld(model: str, capability: str) -> bool:
    """True when ``model``'s offering EXPLICITLY withholds hosted tool ``capability``.

    Hosted-tool availability is a property of the deployment an offering names, not
    of its provider (UDR-0112 D1): one provider class can serve two API surfaces
    whose hosted-tool sets differ -- ``anthropic`` direct vs. ``anthropic`` on
    Foundry, where the workspace must enable Anthropic's server tools.

    Only an explicit ``false`` withholds. An absent catalog, an unknown model, an
    offering without a ``capabilities`` block, and an undeclared key ALL return
    False, so a deployment that declares nothing behaves exactly as it did before
    this gate existed (UDR-0112 D2). Never raises: a capability lookup must not be
    able to break agent construction.
    """
    try:
        from app import models_catalog

        offering = models_catalog.offering_for(model)
    except Exception:  # pragma: no cover - defensive; catalog is optional
        return False
    if offering is None or offering.capability(capability) is not False:
        return False
    _log_withheld(offering.id, capability)
    return True


def _log_withheld(offering_id: str, capability: str) -> None:
    """Announce a withheld hosted tool ONCE per offering+capability (UDR-0112 D5).

    The logging lives here rather than at each call site so a provider cannot gate a
    tool without announcing it. A capability that silently reduces function is
    otherwise indistinguishable from a defect: the operator sees a model that stopped
    searching and has nothing to read. Mirrors the ``_log_lane`` discipline (UDR-0034).
    """
    key = f"{offering_id}:{capability}"
    if key in _logged_withheld:
        return
    _logged_withheld.add(key)
    logger.info(
        "Hosted tool '%s' is withheld for offering '%s' by its catalog capabilities declaration",
        capability,
        offering_id,
    )
