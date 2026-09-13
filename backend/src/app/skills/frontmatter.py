"""Minimal SKILL.md frontmatter reader (CTR-0202, PRP-0165).

The catalog needs three keys out of a skill's leading ``---`` block: ``name``,
``description`` and the optional ``license``. This is a flat ``key: value`` scan,
not a YAML parser, for the same reason ``app.skills.inventory`` already scans
rather than parses: the frontmatter this reads is UNTRUSTED third-party content
fetched from a public repository, and a scan cannot be talked into constructing
objects. Anything it does not understand simply does not appear.

Folded/indented continuation lines are supported for ``description`` only,
because a long description wrapped across lines is common upstream and dropping
its tail would silently truncate what the operator reads before installing.
"""

from __future__ import annotations

_WANTED = ("name", "description", "license")


def parse_frontmatter(text: str) -> dict[str, str]:
    """Return the wanted keys from the leading YAML frontmatter block."""
    if not text or not text.lstrip().startswith("---"):
        return {}

    lines = text.splitlines()
    started = False
    out: dict[str, str] = {}
    current: str | None = None

    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            if started:
                break
            started = True
            continue
        if not started:
            continue

        # Continuation of a folded value: an indented line with no new key.
        if current and line[:1] in (" ", "\t") and ":" not in stripped.split(" ", 1)[0]:
            out[current] = f"{out[current]} {stripped}".strip()
            continue

        if ":" not in stripped:
            current = None
            continue
        key, _, value = stripped.partition(":")
        key = key.strip().lower()
        if key not in _WANTED:
            current = None
            continue
        value = value.strip().strip("'\"")
        out[key] = value
        current = key if key == "description" else None

    return {k: v for k, v in out.items() if v}


__all__ = ["parse_frontmatter"]
