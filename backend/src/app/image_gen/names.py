"""Image tool names (PRP-0187, UDR-0169 D6).

A dependency-free module so the session store, the bundle and the index can recognise
image results in stored history without importing the Images API client.

The tools are named category + verb. The legacy names (``generate_image`` /
``edit_image``) are accepted ONLY by read-only paths over stored history -- image
rendering and image counting -- because a stored conversation keeps the name it was
written with. Nothing registers, advertises or dispatches a legacy name.
"""

IMAGE_GENERATE_TOOL = "image_generate"
IMAGE_EDIT_TOOL = "image_edit"

# legacy name -> current name
LEGACY_IMAGE_TOOL_NAMES: dict[str, str] = {
    "generate_image": IMAGE_GENERATE_TOOL,
    "edit_image": IMAGE_EDIT_TOOL,
}

# Every name a stored tool call carrying generated images may have.
IMAGE_RESULT_TOOL_NAMES = frozenset({IMAGE_GENERATE_TOOL, IMAGE_EDIT_TOOL, *LEGACY_IMAGE_TOOL_NAMES})

__all__ = ["IMAGE_EDIT_TOOL", "IMAGE_GENERATE_TOOL", "IMAGE_RESULT_TOOL_NAMES", "LEGACY_IMAGE_TOOL_NAMES"]
