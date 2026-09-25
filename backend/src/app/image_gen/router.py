"""Image editor API (CTR-0053 v2, PRP-0187; mask editing since PRP-0028).

The Canvas Image Editor's Generate calls this endpoint DIRECTLY -- it does not ask
the agent to call a tool (UDR-0169 D8, re-affirming UDR-0013 D5). The editor uploads
its layers through CTR-0022 first and then sends named SLOTS, never an ordered list:

    source      the image being edited (already normalized to the size rule)
    mask        optional PNG, alpha 0 = may change; applies to the source
    annotated   optional copy of the source with labelled regions (A, B, ...)
    references  up to 14 more images

The server builds ``image[]`` (source, annotated, references -- UDR-0169 D9), composes
the prompt with a legend of those inputs (app.image_gen.prompting), calls the Images
edit API, saves the results, and PERSISTS THE TURN: the user message and an assistant
message carrying an ``image_edit`` tool call, through the same idempotent session
write the SPA uses after an agent run (PRP-0174 / UDR-0156 D4). The server writes it
rather than the SPA because an xhigh edit is slow enough for the tab to be closed
while it runs.
"""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app import models_catalog
from app.auth import verify_api_key
from app.image_gen import capabilities
from app.image_gen.names import IMAGE_EDIT_TOOL
from app.image_gen.prompting import AnnotationNote, EditIntent, compose_edit_prompt
from app.image_gen.tools import (
    EditInputError,
    clamp_n,
    resolve_edit_inputs,
    resolve_image_params,
    run_edit,
    validate_params,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/images", tags=["Image Edit"])


class AnnotationItem(BaseModel):
    label: str = Field(max_length=2)
    note: str = ""


class DisplayImage(BaseModel):
    uri: str
    media_type: str = "image/png"


class ImageEditRequest(BaseModel):
    thread_id: str
    source: str
    mask: str | None = None
    annotated: str | None = None
    references: list[str] = Field(default_factory=list)
    change: str
    preserve: str = ""
    annotations: list[AnnotationItem] = Field(default_factory=list)
    # PRP-0187 Q1: the editor may choose quality / background per edit. Size is not
    # offered: an edit keeps its source's shape (UDR-0169 D4).
    quality: str | None = None
    background: str | None = None
    size: str | None = None
    # PRP-0187 Q3: 1 by default, up to 10.
    n: int = 1
    # The turn the server persists. Ids are minted by the SPA so a retry is idempotent.
    user_message_id: str
    assistant_message_id: str
    display_text: str = ""
    display_images: list[DisplayImage] = Field(default_factory=list)


async def _persist_turn(req: ImageEditRequest, tool_call_id: str, result_json: str, assistant_text: str) -> None:
    """Append the user message and the assistant result to the session (idempotent)."""
    from app.session.router import SaveMessageItem, SaveMessagesRequest, save_messages

    user = SaveMessageItem(
        id=req.user_message_id,
        role="user",
        content=req.display_text or f"Change: {req.change.strip()}",
        images=[{"uri": img.uri, "media_type": img.media_type} for img in req.display_images] or None,
    )
    assistant = SaveMessageItem(
        id=req.assistant_message_id,
        role="assistant",
        content=assistant_text,
        tool_calls=[
            {
                "id": tool_call_id,
                "name": IMAGE_EDIT_TOOL,
                "status": "completed",
                "args": json.dumps({"source": req.source, "references": req.references}),
                "result": result_json,
            }
        ],
    )
    await save_messages(req.thread_id, SaveMessagesRequest(messages=[user, assistant]))


def _result_text(payload: dict) -> str:
    """The text line stored with the result, so later agent turns can name the file."""
    names = [img.get("filename") for img in payload.get("images", []) if img.get("filename")]
    if not names:
        return "The image edit produced no image."
    listed = ", ".join(names)
    return f"Edited image saved as {listed} (source: {payload.get('inputs', ['?'])[0]})."


@router.post("/edit", dependencies=[Depends(verify_api_key)])
async def edit_image_direct(req: ImageEditRequest) -> dict:
    """Run one editor edit and persist it as a chat turn (CTR-0053 v2).

    DEMO_MODE (PRP-0066 / UDR-0041 D3) returns the bundled "edited" placeholder and
    persists the same turn, so the editor flow is exercised end to end.
    """
    from app.demo import is_demo_mode

    # PRP-0114 / UDR-0095 D1/D2: image generation is configured SOLELY by a catalog
    # ``image`` offering (non-demo). Absent -> refuse with an actionable message.
    if not is_demo_mode() and models_catalog.image_config() is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Image generation is not configured. Add an offering with "
                'operations: ["image"] to model_offerings.jsonc '
                "(author via `chatwalaau models add` or the Model Settings screen)."
            ),
        )
    if not req.change.strip():
        raise HTTPException(status_code=400, detail="Describe what to change.")
    if len(req.references) > capabilities.MAX_REFERENCE_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"At most {capabilities.MAX_REFERENCE_IMAGES} reference images are allowed.",
        )

    ordered = [req.source, *([req.annotated] if req.annotated else []), *req.references]
    intent = EditIntent(
        change=req.change,
        preserve=req.preserve,
        annotations=[AnnotationNote(a.label, a.note) for a in req.annotations],
        has_mask=bool(req.mask),
        has_annotated=bool(req.annotated),
        reference_count=len(req.references),
    )
    prompt = compose_edit_prompt(intent)
    n = clamp_n(req.n)
    tool_call_id = f"edit_{uuid.uuid4().hex[:12]}"

    if is_demo_mode():
        from app.demo.image_gen import demo_mask_edit_sync

        payload = await asyncio.to_thread(
            demo_mask_edit_sync, prompt=prompt, thread_id=req.thread_id, n=n, inputs=ordered
        )
        payload["prompt"] = prompt
        result_json = json.dumps(payload)
        await _persist_turn(req, tool_call_id, result_json, _result_text(payload))
        return {**payload, "tool_call_id": tool_call_id}

    async def reject(detail: str) -> HTTPException:
        # A rejected edit still persists the user's turn, with the reason as the answer.
        failure = json.dumps({"error": f"Image editing failed: {detail}", "no_file_created": True})
        await _persist_turn(req, tool_call_id, failure, f"Image editing failed: {detail}")
        return HTTPException(status_code=400, detail=detail)

    try:
        inputs = await asyncio.to_thread(resolve_edit_inputs, req.thread_id, ordered, req.mask)
    except EditInputError as exc:
        raise await reject(str(exc)) from exc

    params = resolve_image_params(req.size, req.quality, req.background, source_size=inputs.source_size)
    invalid = validate_params(params)
    if invalid:
        raise await reject("; ".join(invalid))

    payload, error = await asyncio.to_thread(run_edit, req.thread_id, inputs, prompt, params, n)
    if error is not None:
        # The user's input is never lost: the turn is persisted with the failure.
        detail = json.loads(error).get("error", "Image editing failed")
        await _persist_turn(req, tool_call_id, error, detail)
        blocked = "moderation_blocked" in detail or "safety system" in detail
        raise HTTPException(
            status_code=422 if blocked else 502,
            detail=(
                "Request blocked by content safety filter. Try a different prompt or image." if blocked else detail
            ),
        )
    assert payload is not None
    payload["prompt"] = prompt
    # The caption under an editor result is the user's own words, not the composed
    # legend; a revised prompt the provider returned is kept as it is.
    caption = req.display_text or req.change.strip()
    for image in payload.get("images", []):
        if not image.get("revised_prompt") or image.get("revised_prompt") == prompt:
            image["revised_prompt"] = caption
    result_json = json.dumps(payload)
    await _persist_turn(req, tool_call_id, result_json, _result_text(payload))
    if not payload.get("images"):
        raise HTTPException(status_code=502, detail="No image returned from API")
    return {**payload, "tool_call_id": tool_call_id}
