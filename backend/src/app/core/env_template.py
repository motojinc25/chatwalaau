"""Pure helpers for .env reconciliation against the bundled template (CTR-0097).

This module implements the parsing, drift computation, and template-driven
re-render that back the ``chatwalaau env diff`` / ``chatwalaau env sync``
CLI actions and the startup drift advisory (PRP-0064, UDR-0039).

Design rules (UDR-0039):
- The package-embedded ``.env.template`` is the operator-facing SSOT for the
  env layout: keys, order, section comments, and defaults (D1).
- ``render()`` re-projects the operator's ACTIVE values into the template
  skeleton so the managed region matches the template byte-for-byte except
  for substituted values (D2). Operator values are emitted from their
  ORIGINAL RAW line, never re-serialized, so quoting / ``export`` / inline
  comments / escaping survive intact (D3).
- Keys present in the operator file but absent from the template are never
  deleted; ``render()`` returns them for the caller to quarantine (D4).
- Pure functions only; no file writes here (the CLI owns I/O + backup, D5).
- No new dependency: value detection is a stdlib regex; the CLI uses
  ``python-dotenv`` only where convenient (already a dependency).
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
from pathlib import Path
import re

# An ACTIVE assignment line: optional leading whitespace, optional `export `,
# an identifier, optional whitespace, then `=`. A leading `#` makes it NOT
# active (the `^\s*` cannot consume the `#`).
_ACTIVE_RE = re.compile(r"^\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")

# A COMMENTED assignment line: a `#`, optional whitespace, optional `export `,
# an identifier, optional whitespace, then `=`. This matches the template's
# commented defaults (e.g. `#APP_PORT=8000`) but NOT prose comments that merely
# mention `KEY` followed by a space (e.g. `#   AZURE_OPENAI_API_KEY unset ...`),
# because the identifier must be immediately followed by `=`.
_COMMENTED_RE = re.compile(r"^\s*#\s*(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=")


def find_env_template() -> Path:
    """Return the path to the package-embedded .env.template.

    Resolves correctly in both the dev layout (backend/src/app/templates)
    and the pip-installed layout (site-packages/app/templates, shipped via
    setuptools package-data). Mirrors the resolution used by ``init``.
    """
    return Path(__file__).resolve().parent.parent / "templates" / ".env.template"


def read_template_text() -> str:
    """Read the embedded template text. Raises FileNotFoundError if absent."""
    path = find_env_template()
    if not path.exists():
        msg = f".env template not found in package: {path}"
        raise FileNotFoundError(msg)
    return path.read_text(encoding="utf-8")


def _assignment_key(line: str) -> str | None:
    """Return the KEY of an assignment line (active or commented), else None."""
    m = _ACTIVE_RE.match(line)
    if m:
        return m.group("key")
    m = _COMMENTED_RE.match(line)
    if m:
        return m.group("key")
    return None


def template_keys_in_order(template_text: str) -> list[str]:
    """Ordered, de-duplicated list of every key the template declares.

    Includes both active and commented assignments, in template order.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for line in template_text.splitlines():
        key = _assignment_key(line)
        if key is not None and key not in seen_set:
            seen_set.add(key)
            seen.append(key)
    return seen


def parse_active_assignments(env_text: str) -> tuple[dict[str, str], list[str]]:
    """Parse ACTIVE assignments from an operator .env.

    Returns (raw_by_key, order) where:
    - raw_by_key maps KEY -> the operator's original raw assignment line
      (newline stripped). The LAST active occurrence wins (dotenv semantics).
    - order is the list of keys in first-seen order (for stable quarantine
      output).
    """
    raw_by_key: dict[str, str] = {}
    order: list[str] = []
    for line in env_text.splitlines():
        m = _ACTIVE_RE.match(line)
        if not m:
            continue
        key = m.group("key")
        if key not in raw_by_key:
            order.append(key)
        raw_by_key[key] = line
    return raw_by_key, order


def _all_assignment_keys(env_text: str) -> set[str]:
    """Every key that appears in the operator file, active OR commented.

    Used so that a key the operator has merely parked as a comment is not
    reported as a brand-new "added" key.
    """
    keys: set[str] = set()
    for line in env_text.splitlines():
        key = _assignment_key(line)
        if key is not None:
            keys.add(key)
    return keys


@dataclass(frozen=True)
class EnvDrift:
    """Structural delta between the bundled template and an operator .env."""

    added: list[str]  # template keys absent from the operator file entirely
    removed: list[str]  # operator ACTIVE keys not present in the template
    template_key_count: int
    env_key_count: int

    @property
    def has_drift(self) -> bool:
        return bool(self.added) or bool(self.removed)


def compute_drift(template_text: str, env_text: str) -> EnvDrift:
    """Compute added / removed keys of env_text relative to template_text.

    - added:   template keys not present in the operator file in ANY form
               (neither active nor commented) -- the new knobs to discover.
    - removed: operator ACTIVE keys absent from the template -- no longer
               read by this version (or operator-custom).
    """
    template_keys = template_keys_in_order(template_text)
    template_key_set = set(template_keys)
    operator_all = _all_assignment_keys(env_text)
    active_raw, active_order = parse_active_assignments(env_text)

    added = [k for k in template_keys if k not in operator_all]
    removed = [k for k in active_order if k not in template_key_set]
    return EnvDrift(
        added=added,
        removed=removed,
        template_key_count=len(template_keys),
        env_key_count=len(active_raw),
    )


# Quarantine block delimiters appended below the managed region by render().
_QUARANTINE_HEADER = (
    "# ============================================================\n"
    "# Unmanaged keys (present in your .env, not in the bundled template).\n"
    "# No longer read by this version, or operator-custom. Review and remove\n"
    "# if unneeded. Preserved here so nothing is lost (PRP-0064 / UDR-0039 D4).\n"
    "# ============================================================"
)


def render(template_text: str, env_text: str) -> tuple[str, list[str]]:
    """Re-render an operator .env to the template's canonical layout.

    Walks the template line by line. For each assignment line whose KEY the
    operator has set ACTIVE, the operator's original raw line is emitted
    verbatim; otherwise the template line (commented default) is emitted as
    is. Every comment / header / blank line is emitted from the template
    verbatim, so the managed region matches the template exactly except for
    substituted operator values.

    Operator ACTIVE keys absent from the template are appended verbatim in a
    clearly delimited quarantine block (never deleted).

    Returns (rendered_text, unmanaged_keys).
    """
    active_raw, active_order = parse_active_assignments(env_text)
    template_key_set = set(template_keys_in_order(template_text))

    out_lines: list[str] = []
    for line in template_text.splitlines():
        key = _assignment_key(line)
        if key is not None and key in active_raw:
            out_lines.append(active_raw[key])
        else:
            out_lines.append(line)

    unmanaged = [k for k in active_order if k not in template_key_set]

    rendered = "\n".join(out_lines)
    if unmanaged:
        quarantine_lines = [active_raw[k] for k in unmanaged]
        rendered = rendered.rstrip("\n") + "\n\n" + _QUARANTINE_HEADER + "\n" + "\n".join(quarantine_lines)

    # Always end with exactly one trailing newline.
    return rendered.rstrip("\n") + "\n", unmanaged


def unified_diff(old_text: str, new_text: str, path: str = ".env") -> str:
    """Return a unified diff of old_text -> new_text (empty string if equal)."""
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    # Guarantee trailing newline so difflib does not emit "No newline" markers.
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"{path} (current)",
        tofile=f"{path} (after sync)",
    )
    return "".join(diff)
