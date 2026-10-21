"""Microsoft-Decision-1 wire client (PRP-0219, CTR-0249, UDR-0201 D1 / D4 / D8).

Microsoft Agent Framework has no client for the decision API and the ``openai``
package has no ``systemone`` resource, so this module speaks the wire protocol with
``httpx`` (a direct dependency), the way the GPT-Live session creation does
(UDR-0170 D1)::

    POST {endpoint}/providers/microsoft/v1/systemone
    {"model": <deployment>, "state": <text | object>, "questions": {...}}

Endpoint and credentials come from the single ``decision`` catalog offering
(``models_catalog.decision_config()``): its ``endpoint`` or the shared
``AZURE_OPENAI_ENDPOINT``; its own ``api_key_env`` key or the shared Azure lane
(``api-key`` when AZURE_OPENAI_API_KEY is set, an Entra ``Bearer`` token for the
cognitiveservices scope otherwise, UDR-0034). The credential never appears in an
error.

Failures are raised as :class:`DecisionError` with a stable ``code`` (UDR-0201 D8);
the tool turns them into JSON. A 429 is retried at most twice, honouring
``Retry-After`` and capped at 10 s per wait.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

#: The decision path on a Foundry resource (Microsoft Learn, 2026-10-07). One constant (R2).
SYSTEMONE_PATH = "/providers/microsoft/v1/systemone"
REQUEST_TIMEOUT_SEC = 30.0
MAX_RETRIES_429 = 2
MAX_RETRY_WAIT_SEC = 10.0
_BACKOFF_SEC = (1.0, 2.0)


class DecisionError(Exception):
    """A decision call that did not produce answers (UDR-0201 D8)."""

    def __init__(self, code: str, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class DecisionTarget:
    """Where and as whom to call: resolved from the decision offering."""

    endpoint: str
    deployment: str
    api_key: str | None = None
    offering_id: str = ""


def systemone_url(endpoint: str) -> str:
    """``{scheme}://{host}/providers/microsoft/v1/systemone``; a bare host gets https."""
    raw = endpoint.strip()
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    return f"{parsed.scheme or 'https'}://{parsed.netloc.rstrip('/')}{SYSTEMONE_PATH}"


def resolve_target() -> DecisionTarget | None:
    """The decision offering as a call target, or None when the catalog has none."""
    from app import models_catalog
    from app.core.config import settings

    config = models_catalog.decision_config()
    if config is None:
        return None
    catalog = models_catalog.active_catalog()
    offering = catalog.decision_offering() if catalog is not None else None
    endpoint = config.endpoint or settings.azure_openai_endpoint or ""
    return DecisionTarget(
        endpoint=endpoint,
        deployment=config.deployment,
        api_key=config.api_key,
        offering_id=offering.id if offering is not None else config.deployment,
    )


async def auth_header(api_key: str | None = None) -> tuple[str, str]:
    """``(name, value)`` for the request. The Entra lane may block, so it runs off-loop."""
    if api_key:
        return ("api-key", api_key)
    from app.azure_credential import get_realtime_websocket_auth_header

    return await asyncio.to_thread(get_realtime_websocket_auth_header)


def _service_message(response: httpx.Response) -> str:
    """The service's own error message, or a generic one (never echoes headers)."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(err, str) and err:
            return err
        for key in ("message", "detail"):
            if body.get(key):
                return str(body[key])
    return f"HTTP {response.status_code}"


def _error_for(response: httpx.Response) -> DecisionError:
    status = response.status_code
    message = _service_message(response)
    if status in (400, 422):
        return DecisionError("decision_rejected", message, status)
    if status in (401, 403):
        return DecisionError("decision_auth_failed", message, status)
    if status == 404:
        return DecisionError(
            "decision_not_found",
            f"{message} -- check the decision offering's endpoint (the Foundry resource endpoint) "
            "and its model_ref (the deployment name)",
            status,
        )
    if status == 429:
        return DecisionError("decision_rate_limited", message, status)
    return DecisionError("decision_failed", message, status)


def retry_wait(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retry ``attempt`` (0-based): Retry-After, else backoff; capped."""
    header = response.headers.get("retry-after")
    if header:
        try:
            return max(0.0, min(MAX_RETRY_WAIT_SEC, float(header)))
        except ValueError:
            pass
    return min(MAX_RETRY_WAIT_SEC, _BACKOFF_SEC[min(attempt, len(_BACKOFF_SEC) - 1)])


async def evaluate(
    target: DecisionTarget,
    state: Any,
    questions: dict[str, dict[str, Any]],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Any = asyncio.sleep,
) -> dict[str, Any]:
    """POST one evaluation and return the parsed JSON body (raises DecisionError)."""
    if not target.endpoint:
        raise DecisionError(
            "decision_unavailable",
            "the decision offering has no endpoint and AZURE_OPENAI_ENDPOINT is not set",
        )
    url = systemone_url(target.endpoint)
    body = {"model": target.deployment, "state": state, "questions": questions}
    name, value = await auth_header(target.api_key)
    headers = {name: value, "Content-Type": "application/json", "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SEC, transport=transport) as client:
        attempt = 0
        while True:
            try:
                response = await client.post(url, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                raise DecisionError("decision_unreachable", "the decision service did not answer in time") from exc
            except httpx.HTTPError as exc:
                raise DecisionError(
                    "decision_unreachable", f"could not reach the decision service ({type(exc).__name__})"
                ) from exc
            if response.status_code == 429 and attempt < MAX_RETRIES_429:
                wait = retry_wait(response, attempt)
                logger.info("decision call rate limited; retrying in %.1fs (attempt %d)", wait, attempt + 1)
                await sleep(wait)
                attempt += 1
                continue
            break

    if response.status_code >= 400:
        raise _error_for(response)
    try:
        data = response.json()
    except ValueError as exc:
        raise DecisionError("decision_failed", "the decision service returned invalid JSON", 200) from exc
    if not isinstance(data, dict):
        raise DecisionError("decision_failed", "the decision service returned an unexpected body", 200)
    return data


__all__ = [
    "MAX_RETRIES_429",
    "REQUEST_TIMEOUT_SEC",
    "SYSTEMONE_PATH",
    "DecisionError",
    "DecisionTarget",
    "auth_header",
    "evaluate",
    "resolve_target",
    "retry_wait",
    "systemone_url",
]
