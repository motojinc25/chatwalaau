"""Per-model tool capability subsetting for the Prompt lane (PRP-0185, UDR-0167).

The Model Offering Catalog lets an operator withhold a tool capability for ONE
deployment (CTR-0174). The gate is therefore PER-MODEL -- but the shared tool surface
is assembled exactly ONCE, by ``app.agui.agent_factory._build_tools_and_instructions``,
and that assembly is model-agnostic by construction.

Calling the assembly once per model is not available as a shortcut: it snapshots the
Skills override store and refreshes the live-build set as a side effect, which
UDR-0130 D1 requires to happen ONCE. So the tools are assembled once and SUBSET here,
in the one place that knows the model (UDR-0167 D7).

Everything returned is a NEW list. The inputs are shared, and the MCP entries among
them are the same connection-bearing ``MCPTool`` instances every other agent holds --
``app.agent.tool_surface`` exists because a previous version of this code wrote to
that shared state. Withholding MCP changes EXPOSURE only: no connection is opened,
closed or reconfigured, and no tool object is mutated (UDR-0167 D8).

ONE value decides both the tool and its guidance (UDR-0167 D6). That is the UDR-0112
D3 rule generalized: an agent instructed to search and cite sources while holding no
search tool is a defect, and so is one told it has MCP servers it cannot call.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agent.capability_guidance import ToolGuidance

logger = logging.getLogger(__name__)

# Guidance block names that travel with a capability (UDR-0167 D6). The names are the
# ones `_build_tools_and_instructions` emits; Skills contribute no block of their own
# (MAF injects the skill text through the context provider at run time).
_GUIDANCE_FOR_CAPABILITY: dict[str, str] = {
    "image_generation": "image",
    "mcp": "mcp",
}


@dataclass(frozen=True)
class ModelToolSurface:
    """The tool surface ONE model actually gets, after its capability gates.

    ``web_search_instruction`` is kept OUT of ``guidance`` on purpose. Since PRP-0069
    it is appended to the rendered slot-#3 text as a raw fragment with a leading
    space, not as a ``<tool-guide>`` block; turning it into one here would change
    every prompt on every web-search-capable model, which PRP-0185 did not propose.
    The one-value rule (UDR-0167 D6) is preserved regardless: the same
    ``providers.web_search_tool()`` result decides the tool AND this string.
    """

    tools: list[Any]
    context_providers: list[Any]
    guidance: list[ToolGuidance]
    web_search_instruction: str = ""
    withheld: tuple[str, ...] = ()

    def instructions(self) -> str:
        """Render slot #3 for this model: the blocks, then the web-search fragment."""
        from app.agent.capability_guidance import render_capability_guidance

        return render_capability_guidance(self.guidance) + self.web_search_instruction


def _image_tool_names() -> set[str]:
    """Names of the built-in function tools in the ``image`` category (CTR-0178).

    Read from the static registry rather than restated, so a third image tool cannot
    drift between the gate and the inventory.
    """
    from app.agent.declarative.tool_inventory import BUILTIN_FUNCTION_TOOLS

    return {t.name for t in BUILTIN_FUNCTION_TOOLS if t.category == "image"}


def _is_mcp_tool(obj: Any) -> bool:
    """True when ``obj`` is a MAF MCP tool instance.

    Matched by TYPE, never by name: an MCP tool's ``.name`` is the SERVER name, which
    an operator chooses, so a name-based test would be defeated by a server called
    ``generate_image``. ``MCPTool`` is the common base of the stdio and the streamable
    HTTP transports, both of which stay supported (UDR-0167, PRP-0185 Section 2.1).
    """
    try:
        from agent_framework import MCPTool
    except ImportError:  # pragma: no cover - defensive; MAF is a hard dependency
        logger.debug("agent_framework.MCPTool unavailable; MCP gate inert", exc_info=True)
        return False
    return isinstance(obj, MCPTool)


def _is_skills_provider(obj: Any) -> bool:
    """True when ``obj`` is the mounted SkillsProvider.

    The same duck-typed match ``tool_surface._skills_provider`` uses: the class is a
    MAF type this module does not otherwise import, and the check must not fail a
    build if MAF renames its module path.
    """
    return type(obj).__name__.endswith("SkillsProvider")


def _tool_name(obj: Any) -> str:
    return str(getattr(obj, "name", None) or getattr(obj, "__name__", None) or "")


def subset_for_model(
    model: str,
    tools: list[Any],
    context_providers: list[Any],
    guidance: list[ToolGuidance],
) -> ModelToolSurface:
    """Return the tool surface ``model`` gets, after its catalog capability gates.

    Attaches the provider-supplied hosted web search (the ONE gate for it stays
    ``providers.web_search_tool()`` returning None, UDR-0112 D3) and removes whatever
    the offering withholds. Never mutates its arguments; never raises -- a capability
    lookup must not be able to break agent construction, so a failure leaves the
    surface unsubset rather than empty.
    """
    from app import providers
    from app.agui.agent_registry import WEB_SEARCH_INSTRUCTION
    from app.providers.base import capability_withheld

    out_tools = list(tools)
    out_providers = list(context_providers)
    out_guidance = list(guidance)
    withheld: list[str] = []

    def drop_guidance(capability: str) -> None:
        block = _GUIDANCE_FOR_CAPABILITY.get(capability)
        if block:
            out_guidance[:] = [g for g in out_guidance if g.name != block]

    try:
        # -- image generation ------------------------------------------------
        # TWO gates govern these tools and only this one is per-model: the other is
        # "an image OFFERING must exist" (UDR-0095 D1), already applied at assembly.
        # Absence therefore has two causes and the tool surface report names which
        # applied, in that order (PRP-0185 Section 2.3).
        if capability_withheld(model, "image_generation"):
            image_names = _image_tool_names()
            out_tools = [t for t in out_tools if _tool_name(t) not in image_names]
            drop_guidance("image_generation")
            withheld.append("image_generation")

        # -- MCP -------------------------------------------------------------
        if capability_withheld(model, "mcp"):
            out_tools = [t for t in out_tools if not _is_mcp_tool(t)]
            drop_guidance("mcp")
            withheld.append("mcp")

        # -- Skills ----------------------------------------------------------
        # Skills are not in the tool list: MAF injects their tools at run time through
        # the context provider (UDR-0102 D5), so withholding them means dropping the
        # provider. The provider instance itself is untouched and keeps serving the
        # models that do not withhold it.
        if capability_withheld(model, "skills"):
            out_providers = [p for p in out_providers if not _is_skills_provider(p)]
            withheld.append("skills")

        # -- hosted web search -----------------------------------------------
        # Attached LAST so it is never a candidate for the filters above, and decided
        # by the single value that also decides its instruction (UDR-0112 D3 / D6).
        web_search = providers.web_search_tool(model)
        if web_search is not None:
            out_tools = [web_search, *out_tools]
            web_search_instruction = WEB_SEARCH_INSTRUCTION
        else:
            web_search_instruction = ""
            withheld.append("web_search")
    except Exception:
        logger.warning(
            "Capability subsetting failed for model %r; the shared surface is used unchanged.",
            model,
            exc_info=True,
        )
        return ModelToolSurface(list(tools), list(context_providers), list(guidance))

    if withheld:
        logger.info("Model %s withholds: %s", model, ", ".join(sorted(withheld)))
    return ModelToolSurface(
        out_tools,
        out_providers,
        out_guidance,
        web_search_instruction,
        tuple(sorted(withheld)),
    )


def capability_states(model: str) -> dict[str, bool]:
    """Resolved capability states for ``model``, as ``{key: enabled}`` (CTR-0069).

    Published per model so a configuration surface can say "MCP is withheld on this
    model" instead of leaving an operator to infer it from an agent that stopped using
    a tool. Every key of the closed set is reported, including the fixed-enabled one,
    so a consumer renders the whole vocabulary without knowing which are actionable.
    """
    from app.models_catalog import CAPABILITY_KEYS
    from app.providers.base import capability_withheld

    return {key: not capability_withheld(model, key) for key in sorted(CAPABILITY_KEYS)}


__all__ = ["ModelToolSurface", "capability_states", "subset_for_model"]
