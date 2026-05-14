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


def maf_contents_to_openai_output(contents: list[Any]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Convert MAF response contents to OpenAI Responses API output items.

    Returns (output_items, usage_dict).
    """
    output: list[dict[str, Any]] = []
    text_parts: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0}

    for content in contents:
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

        elif content_type == "usage":
            usage_details = getattr(content, "usage_details", None) or {}
            usage["input_tokens"] = getattr(usage_details, "input_token_count", 0) or usage_details.get(
                "input_token_count", 0
            )
            usage["output_tokens"] = getattr(usage_details, "output_token_count", 0) or usage_details.get(
                "output_token_count", 0
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

    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    return output, usage
