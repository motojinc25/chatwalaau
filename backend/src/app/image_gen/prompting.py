"""Deterministic prompt composition for the image editor (PRP-0187, UDR-0169 D8/D9).

The editor collects intent as separate parts -- what to CHANGE, what to PRESERVE, one
note per annotation label, and reference images -- and the server turns them into ONE
prompt with a legend of the input images. The user never types an input number: the
legend maps the annotated image and ``Ref n`` to wherever they landed in ``image[]``,
which is what makes "the numbering shifts when there is no annotated image" harmless.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AnnotationNote:
    label: str
    note: str = ""


@dataclass(frozen=True)
class EditIntent:
    change: str
    preserve: str = ""
    annotations: list[AnnotationNote] = field(default_factory=list)
    has_mask: bool = False
    has_annotated: bool = False
    reference_count: int = 0


def input_legend(intent: EditIntent) -> list[str]:
    """The ``Input images:`` lines, numbered to match the order image[] is built in."""
    lines = []
    if intent.has_mask:
        lines.append("- Input image 1 is the image to edit. Only the transparent area of the mask may change.")
    else:
        lines.append("- Input image 1 is the image to edit. Change only what the instructions ask for.")
    next_index = 2
    if intent.has_annotated:
        labels = ", ".join(a.label for a in intent.annotations) or "A"
        lines.append(
            f"- Input image {next_index} shows the same image with regions marked {labels}. "
            "Use it only to locate those regions; do not reproduce the marks."
        )
        next_index += 1
    if intent.reference_count:
        first = next_index
        last = next_index + intent.reference_count - 1
        if intent.reference_count == 1:
            lines.append(f"- Input image {first} is a reference image, called Ref 1 in the instructions.")
        else:
            lines.append(
                f"- Input images {first}-{last} are reference images, called "
                f"Ref 1-Ref {intent.reference_count} in the instructions."
            )
    return lines


def compose_edit_prompt(intent: EditIntent) -> str:
    """Compose the prompt sent to the Images edit API."""
    parts = ["Input images:", *input_legend(intent), "", "Change:", intent.change.strip()]
    notes = [a for a in intent.annotations if a.label]
    if intent.has_annotated and notes:
        parts += ["", "Marked regions:"]
        parts += [f"- {a.label}: {a.note.strip() or '(see the change above)'}" for a in notes]
    parts += ["", "Preserve (keep exactly as in input image 1):"]
    if intent.preserve.strip():
        parts.append(intent.preserve.strip())
    parts.append("Anything not listed under Change stays unchanged.")
    return "\n".join(parts)


def display_text(intent: EditIntent) -> str:
    """What the user bubble shows: the user's own words, not the legend."""
    parts = [f"Change: {intent.change.strip()}"]
    parts.extend(f"{a.label}: {a.note.strip()}" for a in intent.annotations if a.label and a.note.strip())
    if intent.preserve.strip():
        parts.append(f"Preserve: {intent.preserve.strip()}")
    return "\n".join(parts)


__all__ = ["AnnotationNote", "EditIntent", "compose_edit_prompt", "display_text", "input_legend"]
