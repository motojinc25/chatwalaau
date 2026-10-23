"""Format converter between OpenAI Responses API and MAF (CTR-0057, PRP-0030)."""

from typing import Any


def openai_input_to_maf_messages(input_data: str | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI Responses API input to AG-UI style messages for normalize_agui_input_messages.

    String input is wrapped as a single user message.
    Array input is passed through with role/content mapping.
    """
    if isinstance(input_data, str):
        return [{"role": "user", "content": input_data}]

    messages = []
    for item in input_data:
        role = item.get("role", "user")
        content = item.get("content", "")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            # Multi-part content (text + images etc.)
            text_parts = [
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in ("input_text", "text")
            ]
            messages.append({"role": role, "content": " ".join(text_parts)})
        else:
            messages.append({"role": role, "content": str(content)})
    return messages


def api_usage(last_call: Any) -> dict[str, int]:
    """The response's ``usage`` object: the LAST model call (CTR-0057 field semantics)."""
    details = last_call or {}
    input_tokens = details.get("input_token_count", 0) or 0
    output_tokens = details.get("output_token_count", 0) or 0
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}


def maf_contents_to_openai_output(contents: list[Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Convert MAF response contents to OpenAI Responses API output items.

    Returns (output_items, usage_dict). The streamed contents are folded first
    (CTR-0266, PRP-0229, UDR-0212 D3.6): since MAF 1.22.0 a finished text or argument
    string is re-sent in full with ``merge_mode="replace"``, so concatenating contents
    would repeat the answer and split one call into several items.
    """
    from app.agent.stream_merge import fold_contents

    folded = fold_contents(contents)
    output: list[dict[str, Any]] = []
    text_parts: list[str] = []

    for content in folded.contents:
        content_type = getattr(content, "type", None)

        if content_type == "text":
            text = getattr(content, "text", "")
            if text:
                text_parts.append(text)

        elif content_type == "text_reasoning":
            # Reasoning content is not exposed in OpenAI API output
            pass

        elif content_type == "function_call":
            call_id = getattr(content, "call_id", "")
            name = getattr(content, "name", "")
            arguments = getattr(content, "arguments", "")
            if isinstance(arguments, dict):
                import json

                arguments = json.dumps(arguments)
            output.append(
                {
                    "type": "function_call",
                    "name": name,
                    "arguments": arguments or "",
                    "call_id": call_id,
                }
            )

        elif content_type == "function_result":
            call_id = getattr(content, "call_id", "")
            result = getattr(content, "result", "")
            if not isinstance(result, str):
                import json

                result = json.dumps(result)
            output.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result,
                }
            )

    # Add accumulated text as a message output item
    if text_parts:
        full_text = "".join(text_parts)
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": full_text}],
            }
        )

    return output, api_usage(folded.usage.last_call)
