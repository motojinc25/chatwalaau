"""Microsoft Foundry base-model provider (CTR-0102, PRP-0106, UDR-0085).

The fourth base-model provider, behind the existing ``app.providers`` seam
(UDR-0045). Foundry uses the MAF stable connector
``agent_framework_foundry.FoundryChatClient`` (UDR-0085 D1): the OpenAI
Responses API spoken through a Microsoft Foundry PROJECT endpoint
(``https://<resource>.services.ai.azure.com/api/projects/<project>``),
authenticated by an Entra ID credential ONLY -- the connector has no API-key
parameter, so the shared ``app.azure_credential`` lanes (cli /
managed-identity / default via AZURE_CREDENTIAL_MODE + AZURE_TENANT_ID) are
reused 1:1 and ``AZURE_OPENAI_API_KEY`` never applies to this lane
(UDR-0085 D4).

NOTE: this is the NATIVE Foundry provider (``FOUNDRY_*`` namespace). It is
unrelated to the ``anthropic`` provider's ``ANTHROPIC_HOSTING=foundry`` lane
(Claude models hosted on Foundry, ``ANTHROPIC_FOUNDRY_*`` keys); Claude
deployments are best served through that lane, not this provider.

Model-family-aware generation options (UDR-0085 D5 as amended by A1): the
Foundry catalog spans many model families, and the OpenAI generation controls
are NOT accepted outside the OpenAI reasoning family. Verified live against a
Foundry project (DeepSeek-V4-Pro deployment, 2026-07-05): ``reasoning.effort``,
``reasoning.summary`` and non-default ``text.verbosity`` are each rejected with
HTTP 400 ``unsupported_parameter``, while bare requests, native structured
output (``text.format`` json_schema), and the hosted web_search tool all
succeed. Therefore:

- Deployments whose name matches the OpenAI REASONING family (``gpt-5*`` /
  ``o<digit>*``) inherit the azure-openai reasoning-only catalog unchanged
  (effort + verbosity, always-send ``reasoning.effort``).
- Every OTHER family (DeepSeek, Grok, Llama, Phi, Mistral, ...) advertises an
  EMPTY option catalog and builds BARE requests -- no ``reasoning``, no
  ``text.verbosity``, no sampling params. The UI is catalog-driven (UDR-0057
  D2), so no generation control is rendered for these models.

Detection is by DEPLOYMENT NAME prefix: Foundry deployment names default to
the model name but are operator-chosen, so keep OpenAI reasoning deployments
on their standard names (a non-matching name is safely treated as
"no generation options" -- the failure mode is a missing control, not an
HTTP 400).

Verification status (UDR-0085 D6/D8, PRP-0106 completion notes):

- ``web_search_tool()`` returns the country-scoped hosted tool (UDR-0085 D8
  VERIFIED live 2026-07-05: hosted web_search completed on a non-OpenAI
  deployment; the tool is platform-level on the Foundry Responses lane).
- ``supports_background = False`` (UDR-0085 D6): ``background=true`` was
  ACCEPTED live (status=queued), but the CTR-0045 contract also requires
  continuation_token resume through the app lane, which is not yet verified
  end-to-end. Flip to True once resume is verified.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from agent_framework_foundry import FoundryChatClient

from app import models_catalog
from app.azure_credential import get_credential
from app.core.config import settings
from app.providers.azure_openai import AzureOpenAIProvider, _StructuredOutputMixin

NAME = "foundry"

# OpenAI reasoning-family deployment-name prefixes (gpt-5*, o3 / o4-mini / ...).
# Only these accept the OpenAI reasoning.effort / text.verbosity controls on the
# Foundry Responses lane (UDR-0085 A1). gpt-4o does NOT match (gpt-4 prefix).
_OPENAI_REASONING_NAME = re.compile(r"^(gpt-5|o\d)", re.IGNORECASE)


def is_openai_reasoning_deployment(model: str) -> bool:
    """True when ``model`` names an OpenAI reasoning-family deployment."""
    return bool(_OPENAI_REASONING_NAME.match(model.strip()))


def _effective_family(model: str) -> str:
    """Resolve the option-catalog family for a Foundry model (UDR-0085 A1 + UDR-0087 D6).

    A catalog offering's declared ``family`` (PRP-0109) wins -- so a non-standard
    Foundry deployment name (e.g. ``my-deepseek-deployment``) can declare
    ``family: bare`` instead of relying on the name-prefix heuristic. When no
    override is given (legacy lane or offering without a family), fall back to the
    UDR-0085 A1 deployment-name heuristic: OpenAI reasoning family -> the
    azure-openai catalog; every other family -> bare.
    """
    override = models_catalog.offering_family(model)
    if override:
        return override
    return "openai-reasoning" if is_openai_reasoning_deployment(model) else "bare"


_structured_client_cls: type | None = None


def _structured_foundry_client_class() -> type:
    """Structured-output wrapper over FoundryChatClient (PRP-0082, UDR-0085 D7).

    ``FoundryChatClient`` inherits ``RawOpenAIChatClient`` and therefore exposes
    the same ``_prepare_options`` request-assembly chokepoint the azure lane
    hooks (verified against the pinned connector 1.0.0), so the same
    ``_StructuredOutputMixin`` drops the hosted web_search tool whenever a JSON
    ``text.format`` is set. Inert when no structured format is present.
    """
    global _structured_client_cls
    if _structured_client_cls is None:
        _structured_client_cls = type("StructuredFoundryChatClient", (_StructuredOutputMixin, FoundryChatClient), {})
    return _structured_client_cls


class FoundryProvider(AzureOpenAIProvider):
    """Provider for Microsoft Foundry projects (Entra ID auth).

    Reuses ``AzureOpenAIProvider`` for the OpenAI reasoning family; every other
    model family runs with generation parameters DISABLED (empty catalog, bare
    requests; UDR-0085 D7 + A1).
    """

    name = NAME
    # UDR-0085 D6: background=true is ACCEPTED by the Foundry Responses lane
    # (verified live, status=queued), but continuation_token resume through the
    # app lane (CTR-0045) is not yet verified end-to-end, so v1 keeps False --
    # the Background toggle stays disabled for Foundry models. Flipping to True
    # after the resume E2E is additive.
    supports_background = False

    def models(self) -> list[str]:
        return settings.foundry_model_list

    def build_chat_client(self, model: str) -> Any:
        # Foundry project lane (UDR-0085 D1/D4): project endpoint + the
        # mode-selected Entra ID credential from app.azure_credential (the
        # accessor logs the active lane once per process, UDR-0034 D7). The
        # connector has no api_key parameter; AZURE_OPENAI_API_KEY never
        # applies here. Prompt caching is pass-through exactly as the azure
        # lane (automatic service-side, no request rewrite; UDR-0056 D4).
        #
        # Catalog lane (PRP-0109, UDR-0087): `model` is the offering id, so the
        # connector `model=` uses the offering's model_ref (real deployment) and
        # the project endpoint may be per-offering. Entra-only stays enforced --
        # api_key_env is ignored for Foundry (UDR-0085 D4). Legacy lane
        # (offering is None): byte-for-byte the prior behavior.
        offering = models_catalog.offering_for(model)
        model_ref = offering.model_ref if offering is not None else model
        endpoint = (offering.endpoint if offering is not None and offering.endpoint else settings.foundry_project_endpoint) or None
        return _structured_foundry_client_class()(
            project_endpoint=endpoint,
            model=model_ref,
            credential=get_credential(),
        )

    def model_options_catalog(self, model: str) -> dict[str, Any]:
        # OpenAI reasoning family -> the inherited azure-openai catalog (effort
        # + verbosity). Any other family -> EMPTY catalog: the UI renders no
        # generation control (UDR-0057 D2) and resolve_options() yields {}
        # (UDR-0085 A1, generalized by the catalog family override, UDR-0087 D6).
        if _effective_family(model) == "openai-reasoning":
            return super().model_options_catalog(model)
        return {"options": []}

    def reasoning_catalog(self, model: str) -> dict[str, Any]:
        # Derived effort-axis back-compat view (CTR-0069). A family with no
        # effort axis advertises an empty allowed list and a None default; the
        # AG-UI endpoint omits the usage `reasoning` echo for None (UDR-0085 A1).
        if _effective_family(model) == "openai-reasoning":
            return super().reasoning_catalog(model)
        return {"allowed": [], "default": None}

    def build_model_options(self, model: str, selected: dict[str, Any] | None = None) -> dict[str, Any]:
        # BARE request for non-OpenAI families: no reasoning.effort /
        # reasoning.summary / text.verbosity -- each was rejected with HTTP 400
        # unsupported_parameter on the verified DeepSeek deployment (UDR-0085 A1).
        if _effective_family(model) == "openai-reasoning":
            return super().build_model_options(model, selected)
        return {}

    def web_search_tool(self, model: str) -> Any | None:
        # UDR-0085 D8 (VERIFIED live 2026-07-05): the hosted web_search tool
        # completed against a Foundry project deployment (including a
        # non-OpenAI family), so the provider supplies the same country-scoped
        # shape as azure-openai. The PRP-0082 mixin still strips it whenever a
        # JSON text.format is set.
        #
        # v0.102.0 defect fix (PRP-0108 completion note): the connector factory
        # returns an azure.ai.projects WebSearchTool whose user_location is a
        # WebSearchApproximateLocation MODEL OBJECT. MAF 1.10 (#6556) serializes
        # every tool definition to JSON for the OTel invocation span, and
        # json.dumps raises "Object of type WebSearchApproximateLocation is not
        # JSON serializable" -- killing the FIRST turn of every Foundry model.
        # Deep-convert to plain JSON values: the wire shape is identical (the
        # azure-openai lane ships the same shape as a plain dict), and
        # is_web_search_tool() keeps matching via its dict branch.
        return _to_plain_json(
            FoundryChatClient.get_web_search_tool(
                user_location={"type": "approximate", "country": settings.web_search_country},
            )
        )


def _to_plain_json(value: Any) -> Any:
    """Recursively convert Mapping / sequence SDK models to plain JSON values."""
    if isinstance(value, Mapping):
        return {key: _to_plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain_json(item) for item in value]
    return value
