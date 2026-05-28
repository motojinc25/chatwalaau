"""CLI Chat Subcommand (CTR-0081, PRP-0041).

Provides agent chat from the command line via AG-UI SSE streaming
or OpenAI Responses API. Supports single-shot and interactive REPL modes.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any
import uuid

from app.cli.client import ChatWalaauClient, _safe_print, client_from_args, output_json, output_jsonl

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable


def register_chat_parser(
    subparsers: argparse._SubParsersAction,
    add_client_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Register the 'chat' subcommand parser."""
    chat_parser = subparsers.add_parser("chat", help="Chat with the agent")
    add_client_options(chat_parser)
    chat_parser.add_argument("message", nargs="?", default=None, help="User message text")
    chat_parser.add_argument("-s", "--session", default=None, help="Session/thread ID (auto-generated if omitted)")
    chat_parser.add_argument("-m", "--model", default=None, help="Model deployment name")
    chat_parser.add_argument("-i", "--interactive", action="store_true", help="Interactive REPL mode")
    chat_parser.add_argument("--no-stream", action="store_true", help="Wait for complete response")
    chat_parser.add_argument("--api-mode", action="store_true", help="Use /v1/responses instead of /ag-ui/ endpoint")
    chat_parser.set_defaults(func=_run_chat)


def _run_chat(args: argparse.Namespace) -> None:
    """Execute the chat subcommand."""
    if not args.interactive and not args.message:
        print('Error: Message required. Use: chatwalaau chat "message" or chatwalaau chat -i', file=sys.stderr)
        sys.exit(1)

    client = client_from_args(args)
    try:
        if args.interactive:
            _run_interactive(client, args)
        else:
            thread_id = args.session or str(uuid.uuid4())
            _run_single_shot(client, args, thread_id, args.message)
    finally:
        client.close()


def _run_single_shot(
    client: ChatWalaauClient,
    args: argparse.Namespace,
    thread_id: str,
    message: str,
) -> None:
    """Send one message and print the response."""
    if args.api_mode:
        _chat_via_openai_api(client, args, thread_id, message)
    else:
        _chat_via_agui(client, args, thread_id, message)


def _run_interactive(client: ChatWalaauClient, args: argparse.Namespace) -> None:
    """Interactive REPL mode: read user input, send, display response, loop."""
    thread_id = args.session or str(uuid.uuid4())
    if not args.json_output:
        print(f"ChatWalaʻau Interactive Chat (session: {thread_id[:8]}...)")
        print("Type 'exit' or 'quit' to end. Ctrl+C to cancel.\n")

    while True:
        try:
            user_input = input("you> ") if not args.json_output else input()
        except (EOFError, KeyboardInterrupt):
            if not args.json_output:
                print("\nGoodbye.")
            break

        stripped = user_input.strip()
        if not stripped:
            continue
        if stripped.lower() in ("exit", "quit"):
            if not args.json_output:
                print("Goodbye.")
            break

        _run_single_shot(client, args, thread_id, stripped)
        if not args.json_output:
            print()  # blank line between turns


def _chat_via_agui(
    client: ChatWalaauClient,
    args: argparse.Namespace,
    thread_id: str,
    message: str,
) -> None:
    """Chat via AG-UI SSE endpoint (POST /ag-ui/)."""
    payload: dict[str, Any] = {
        "thread_id": thread_id,
        "run_id": str(uuid.uuid4()),
        "messages": [
            {
                "id": str(uuid.uuid4()),
                "role": "user",
                "content": message,
            }
        ],
    }
    state: dict[str, Any] = {}
    if args.model:
        state["model"] = args.model
    if state:
        payload["state"] = state

    if args.no_stream:
        _agui_no_stream(client, args, payload)
    else:
        _agui_stream(client, args, payload)


def _maybe_auto_approve(
    client: ChatWalaauClient,
    args: argparse.Namespace,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """PRP-0067 / UDR-0043 D5 -- CLI auto-approve helper.

    When the AG-UI backend emits a ``CUSTOM tool_approval_request`` event
    (because the agent called an approval-gated tool), the CLI has no
    human-in-the-loop UI. Immediately POST ``approved=true`` back to
    ``/api/tool-approval`` so the parked AG-UI stream resumes.
    """
    if event_type != "CUSTOM":
        return
    if data.get("name") != "tool_approval_request":
        return
    value = data.get("value") or {}
    approval_id = value.get("id")
    tool_name = value.get("tool_name", "<unknown>")
    if not approval_id:
        return
    try:
        client.post("/api/tool-approval", json_data={"id": approval_id, "approved": True})
    except SystemExit:  # client._handle_http_error -> sys.exit
        raise
    except Exception as exc:
        print(f"Warning: failed to auto-approve tool={tool_name}: {exc}", file=sys.stderr)
        return
    if not args.json_output:
        print(f"[auto-approved tool: {tool_name}]", file=sys.stderr)


def _agui_stream(
    client: ChatWalaauClient,
    args: argparse.Namespace,
    payload: dict[str, Any],
) -> None:
    """Stream AG-UI SSE events to terminal."""
    thinking_shown = False
    try:
        for event in client.stream_sse("/ag-ui/", payload):
            event_type = event.get("event", "")
            data = event.get("data", {})

            # PRP-0067 / UDR-0043 D5 -- auto-approve any approval requests
            # before rendering, so the parked stream resumes immediately.
            _maybe_auto_approve(client, args, event_type, data)

            if args.json_output:
                output_jsonl(event)
                continue

            # Human-friendly rendering
            if event_type == "TEXT_MESSAGE_CONTENT":
                delta = data.get("delta", "")
                try:
                    print(delta, end="", flush=True)
                except UnicodeEncodeError:
                    print(delta.encode("ascii", errors="replace").decode("ascii"), end="", flush=True)
            elif event_type == "TEXT_MESSAGE_END":
                print()
            elif event_type == "TOOL_CALL_START":
                name = data.get("name", "unknown")
                print(f"[tool: {name}]")
            elif event_type == "TOOL_CALL_RESULT":
                result_str = data.get("result", "")
                if isinstance(result_str, str) and len(result_str) > 200:
                    result_str = result_str[:200] + "..."
                print(f"[result: {result_str}]")
            elif event_type == "REASONING_MESSAGE_CONTENT":
                if not thinking_shown:
                    print("(thinking...)")
                    thinking_shown = True
            elif event_type == "RUN_ERROR":
                error_msg = data.get("message", "Unknown error")
                print(f"Error: {error_msg}", file=sys.stderr)
                sys.exit(1)
            elif event_type == "RUN_FINISHED":
                pass  # end of response
    except KeyboardInterrupt:
        print("\n(interrupted)", file=sys.stderr)
        sys.exit(130)


def _agui_no_stream(
    client: ChatWalaauClient,
    args: argparse.Namespace,
    payload: dict[str, Any],
) -> None:
    """Consume AG-UI SSE events and print final accumulated text."""
    accumulated_text = ""
    events: list[dict[str, Any]] = []

    for event in client.stream_sse("/ag-ui/", payload):
        events.append(event)
        event_type = event.get("event", "")
        data = event.get("data", {})

        # PRP-0067 / UDR-0043 D5 -- non-streaming CLI mode auto-approver.
        _maybe_auto_approve(client, args, event_type, data)

        if event_type == "TEXT_MESSAGE_CONTENT":
            accumulated_text += data.get("delta", "")
        elif event_type == "RUN_ERROR":
            error_msg = data.get("message", "Unknown error")
            print(f"Error: {error_msg}", file=sys.stderr)
            sys.exit(1)

    if args.json_output:
        output_json({"text": accumulated_text, "event_count": len(events)})
    else:
        _safe_print(accumulated_text)


def _chat_via_openai_api(
    client: ChatWalaauClient,
    args: argparse.Namespace,
    thread_id: str,
    message: str,
) -> None:
    """Chat via OpenAI Responses API (POST /v1/responses)."""
    payload: dict[str, Any] = {
        "input": message,
        "stream": not args.no_stream,
    }
    if args.model:
        payload["model"] = args.model
    if args.session:
        # Use previous_response_id for multi-turn
        payload["previous_response_id"] = thread_id

    if args.no_stream:
        response = client.post("/v1/responses", json_data=payload)
        data = response.json()
        if args.json_output:
            output_json(data)
        else:
            # Extract text from response output
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for content in item.get("content", []):
                        if content.get("type") == "output_text":
                            print(content.get("text", ""))
    else:
        # Streaming mode via SSE
        try:
            for event in client.stream_sse("/v1/responses", payload):
                event_type = event.get("event", "")
                data = event.get("data", {})

                if args.json_output:
                    output_jsonl(event)
                    continue

                if event_type == "response.output_text.delta":
                    print(data.get("delta", ""), end="", flush=True)
                elif event_type == "response.completed":
                    print()
                elif event_type == "error":
                    print(f"Error: {data.get('message', 'Unknown')}", file=sys.stderr)
                    sys.exit(1)
        except KeyboardInterrupt:
            print("\n(interrupted)", file=sys.stderr)
            sys.exit(130)
