"""CLI Client Framework (CTR-0080, PRP-0041).

Thin httpx wrapper providing base URL resolution, Bearer token auth,
JSON/text output mode, and SSE stream consumption for CLI subcommands.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    import argparse
    from collections.abc import Iterator

try:
    from httpx_sse import connect_sse
except ImportError:
    connect_sse = None  # type: ignore[assignment]


class ChatWalaauClient:
    """HTTP client for ChatWalaʻau REST API."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = 30,
        verify_ssl: bool = True,
    ) -> None:
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout, read=300),
            verify=verify_ssl,
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return self._request("GET", path, params=params)

    def post(
        self,
        path: str,
        json_data: dict[str, Any] | None = None,
        data: Any = None,
        files: Any = None,
    ) -> httpx.Response:
        return self._request("POST", path, json=json_data, data=data, files=files)

    def put(self, path: str, json_data: dict[str, Any] | None = None) -> httpx.Response:
        return self._request("PUT", path, json=json_data)

    def patch(self, path: str, json_data: dict[str, Any] | None = None) -> httpx.Response:
        return self._request("PATCH", path, json=json_data)

    def delete(self, path: str) -> httpx.Response:
        return self._request("DELETE", path)

    def stream_sse(self, path: str, json_data: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Stream SSE events from a POST endpoint.

        Yields dicts with 'event' and 'data' keys for each SSE event.
        """
        if connect_sse is None:
            print("Error: httpx-sse is required for streaming. Install with: pip install httpx-sse", file=sys.stderr)
            sys.exit(1)

        with connect_sse(
            self._client,
            "POST",
            path,
            json=json_data,
            headers={"Accept": "text/event-stream"},
        ) as event_source:
            for event in event_source.iter_sse():
                try:
                    data = json.loads(event.data) if event.data else {}
                except json.JSONDecodeError:
                    data = {"raw": event.data}
                # AG-UI encoder uses data-only SSE (no event: field).
                # Event type is in data["type"]. Fall back to SSE event field
                # for non-AG-UI endpoints (e.g., OpenAI API).
                event_type = data.get("type", "") if isinstance(data, dict) else ""
                if not event_type and event.event:
                    event_type = event.event
                yield {"event": event_type, "data": data}

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
            response.raise_for_status()
            return response
        except httpx.HTTPStatusError as e:
            _handle_http_error(e)
            raise  # unreachable but satisfies type checker
        except httpx.TimeoutException:
            base = str(self._client.base_url).rstrip("/")
            print(f"Error: Request timed out connecting to {base}.", file=sys.stderr)
            sys.exit(1)
        except httpx.TransportError as e:
            base = str(self._client.base_url).rstrip("/")
            err_str = str(e).lower()
            if "ssl" in err_str or "certificate" in err_str:
                print(f"Error: TLS certificate verification failed for {base}.", file=sys.stderr)
                print("  Use --no-verify to skip certificate verification (e.g., for mkcert).", file=sys.stderr)
            else:
                print(f"Error: Cannot connect to {base}. Is the server running?", file=sys.stderr)
            sys.exit(1)

    def close(self) -> None:
        self._client.close()


def _handle_http_error(exc: httpx.HTTPStatusError) -> None:
    """Print human-friendly error message for HTTP errors and exit."""
    status = exc.response.status_code
    try:
        detail = exc.response.json().get("detail", str(exc))
    except Exception:
        detail = exc.response.text[:200] if exc.response.text else str(exc)

    messages = {
        401: "Error: Authentication failed. Check --api-key or CHATWALAAU_API_KEY.",
        403: "Error: Access denied.",
        404: f"Error: Resource not found. {detail}",
    }
    if status in messages:
        print(messages[status], file=sys.stderr)
    elif status >= 500:
        print(f"Error: Server error ({status}). Check server logs.", file=sys.stderr)
    else:
        print(f"Error: HTTP {status} - {detail}", file=sys.stderr)
    sys.exit(1)


def client_from_args(args: argparse.Namespace) -> ChatWalaauClient:
    """Create an ChatWalaauClient from parsed CLI arguments."""
    timeout = getattr(args, "timeout", 30)
    # Chat commands get longer timeout
    if getattr(args, "command", None) == "chat":
        timeout = max(timeout, 300)

    return ChatWalaauClient(
        base_url=args.base_url,
        api_key=args.api_key,
        timeout=timeout,
        verify_ssl=not args.no_verify,
    )


def _safe_print(text: str) -> None:
    """Print text handling encoding errors on Windows (cp1252 etc.)."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(sys.stdout.encoding or "utf-8"))


def output_json(data: Any) -> None:
    """Print data as formatted JSON to stdout."""
    _safe_print(json.dumps(data, ensure_ascii=False, indent=2))


def output_jsonl(data: dict[str, Any]) -> None:
    """Print a single JSON line to stdout (JSONL format)."""
    _safe_print(json.dumps(data, ensure_ascii=False))
