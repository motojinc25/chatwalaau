"""Computer Action DSL (CTR-0230, PRP-0189, UDR-0171 D3).

The ONLY way a model expresses desktop actions. A batch is a list of steps with
bounded control flow (``if`` / ``repeat``) and local waits; there are no variables,
no expressions and no code (C2). A batch is validated COMPLETELY before any input is
sent -- an invalid batch executes nothing.

The wire schema the model sees is deliberately loose (``list[dict]``): recursive
JSON schemas are handled differently by each provider, and a provider-neutral tool
must not depend on that. The grammar is enforced here instead, with Pydantic
discriminated unions, and a validation error comes back as a short readable message
the model can act on.

Pure module: no I/O, no Windows imports, so every rule is unit-testable anywhere.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

# ---- Bounds (UDR-0171 D3) -------------------------------------------------------

MAX_DEPTH = 3
MAX_REPEAT = 10
MAX_TIMEOUT_MS = 30_000
BATCH_WALL_CLOCK_S = 120.0
#: A literal ``text`` at least this long that equals a secret value is refused (D4).
SECRET_LITERAL_MIN_LEN = 5

# ---- Keys ------------------------------------------------------------------------

_NAMED_KEYS = {
    "enter",
    "tab",
    "esc",
    "escape",
    "space",
    "backspace",
    "delete",
    "insert",
    "home",
    "end",
    "pageup",
    "pagedown",
    "up",
    "down",
    "left",
    "right",
    "ctrl",
    "shift",
    "alt",
    "apps",
}
_PUNCTUATION = {"-", "=", ",", ".", "/", ";", "'", "[", "]", "\\", "`"}
KEY_NAMES: frozenset[str] = frozenset(
    _NAMED_KEYS
    | _PUNCTUATION
    | {chr(c) for c in range(ord("a"), ord("z") + 1)}
    | {str(d) for d in range(10)}
    | {f"f{n}" for n in range(1, 25)}
)
#: Refused outright (D3): the Windows key opens Run and shell shortcuts.
REFUSED_KEYS: frozenset[str] = frozenset({"win", "lwin", "rwin", "windows", "super", "meta", "cmd"})
#: Refused chords (D3), compared as sets.
REFUSED_CHORDS: tuple[frozenset[str], ...] = (
    frozenset({"ctrl", "alt", "delete"}),
    frozenset({"ctrl", "shift", "esc"}),
    frozenset({"ctrl", "shift", "escape"}),
)


class DslError(ValueError):
    """A batch that must not execute. ``code`` is the result status."""

    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


# ---- Grammar -----------------------------------------------------------------------

_STRICT = ConfigDict(extra="forbid")


class Point(BaseModel):
    """A target: an element id of the observation, or image coordinates of it."""

    model_config = _STRICT

    element: str | None = None
    x: int | None = Field(default=None, ge=0, le=10_000)
    y: int | None = Field(default=None, ge=0, le=10_000)

    @model_validator(mode="after")
    def _one_form(self) -> Point:
        has_xy = self.x is not None or self.y is not None
        if self.element is not None and has_xy:
            raise ValueError("give either element or x/y, not both")
        if self.element is None and (self.x is None or self.y is None):
            raise ValueError("a target needs element, or both x and y")
        return self


class ElementQuery(BaseModel):
    model_config = _STRICT

    name: str = Field(min_length=1, max_length=200)
    control: str | None = Field(default=None, max_length=40)


class Condition(BaseModel):
    """Exactly one of ``element`` / ``window_title`` / ``dialog`` / ``changed``."""

    model_config = _STRICT

    element: ElementQuery | None = None
    present: bool = True
    window_title: str | None = Field(default=None, min_length=1, max_length=200)
    dialog: bool | None = None
    changed: bool | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Condition:
        given = [
            self.element is not None,
            self.window_title is not None,
            self.dialog is not None,
            self.changed is not None,
        ]
        if sum(given) != 1:
            raise ValueError("a condition needs exactly one of element, window_title, dialog, changed")
        return self


class _Targeted(BaseModel):
    model_config = _STRICT

    element: str | None = None
    x: int | None = Field(default=None, ge=0, le=10_000)
    y: int | None = Field(default=None, ge=0, le=10_000)

    def point(self) -> Point:
        return Point(element=self.element, x=self.x, y=self.y)

    @model_validator(mode="after")
    def _has_target(self) -> _Targeted:
        self.point()  # raises when malformed
        return self


class Click(_Targeted):
    type: Literal["click"]
    button: Literal["left", "right", "middle"] = "left"


class DoubleClick(_Targeted):
    type: Literal["double_click"]


class RightClick(_Targeted):
    type: Literal["right_click"]


class Move(_Targeted):
    type: Literal["move"]


class Drag(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["drag"]
    from_: Point = Field(alias="from")
    to: Point


class TypeText(BaseModel):
    model_config = _STRICT

    type: Literal["type_text"]
    text: str | None = Field(default=None, max_length=10_000)
    secret: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _text_xor_secret(self) -> TypeText:
        if (self.text is None) == (self.secret is None):
            raise ValueError("type_text needs exactly one of text or secret")
        return self


class Keypress(BaseModel):
    model_config = _STRICT

    type: Literal["keypress"]
    keys: list[str] | str

    def chord(self) -> list[str]:
        raw = [self.keys] if isinstance(self.keys, str) else list(self.keys)
        out: list[str] = []
        for entry in raw:
            key = entry.strip().lower()
            if len(key) > 1 and "+" in key:
                out.extend(part.strip() for part in key.split("+") if part.strip())
            elif key:
                out.append(key)
        return out

    @model_validator(mode="after")
    def _known_keys(self) -> Keypress:
        chord = self.chord()
        if not chord:
            raise ValueError("keypress needs at least one key")
        if len(chord) > 4:
            raise ValueError("a chord has at most 4 keys")
        refused = [k for k in chord if k in REFUSED_KEYS]
        if refused:
            raise ValueError(f"key {refused[0]!r} is not allowed")
        unknown = [k for k in chord if k not in KEY_NAMES]
        if unknown:
            raise ValueError(f"unknown key {unknown[0]!r}")
        if any(set(chord) >= refused_chord for refused_chord in REFUSED_CHORDS):
            raise ValueError("this key combination is not allowed")
        return self


class Scroll(BaseModel):
    model_config = _STRICT

    type: Literal["scroll"]
    element: str | None = None
    x: int | None = Field(default=None, ge=0, le=10_000)
    y: int | None = Field(default=None, ge=0, le=10_000)
    dy: int = Field(default=0, ge=-50, le=50)
    dx: int = Field(default=0, ge=-50, le=50)

    def point(self) -> Point | None:
        if self.element is None and self.x is None and self.y is None:
            return None
        return Point(element=self.element, x=self.x, y=self.y)

    @model_validator(mode="after")
    def _moves(self) -> Scroll:
        if self.dy == 0 and self.dx == 0:
            raise ValueError("scroll needs dy or dx")
        self.point()
        return self


class WaitForChange(BaseModel):
    model_config = _STRICT

    type: Literal["wait_for_change"]
    timeout_ms: int | None = Field(default=None, ge=100, le=MAX_TIMEOUT_MS)


class WaitUntilStable(BaseModel):
    model_config = _STRICT

    type: Literal["wait_until_stable"]
    stable_ms: int | None = Field(default=None, ge=100, le=5_000)
    timeout_ms: int | None = Field(default=None, ge=100, le=MAX_TIMEOUT_MS)


class WaitFor(BaseModel):
    model_config = _STRICT

    type: Literal["wait_for"]
    cond: Condition
    timeout_ms: int = Field(default=5_000, ge=100, le=MAX_TIMEOUT_MS)


class If(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["if"]
    cond: Condition
    then: list[Action]
    else_: list[Action] = Field(default_factory=list, alias="else")


class Repeat(BaseModel):
    model_config = _STRICT

    type: Literal["repeat"]
    times: int = Field(ge=1, le=MAX_REPEAT)
    until: Condition | None = None
    body: list[Action] = Field(min_length=1)


Action = Annotated[
    Click
    | DoubleClick
    | RightClick
    | Move
    | Drag
    | TypeText
    | Keypress
    | Scroll
    | WaitForChange
    | WaitUntilStable
    | WaitFor
    | If
    | Repeat,
    Field(discriminator="type"),
]

If.model_rebuild()
Repeat.model_rebuild()

AFTER_MODES = ("wait_until_stable", "wait_for_change", "none")
OBSERVE_MODES = ("auto", "always", "never")


class ActionBatch(BaseModel):
    """One decision: what the model wants done, and what it expects afterwards."""

    model_config = _STRICT

    obs: str = Field(min_length=1, max_length=20)
    state: str = Field(default="", max_length=80)
    actions: list[Action] = Field(min_length=1)
    after: Literal["wait_until_stable", "wait_for_change", "none"] = "wait_until_stable"
    expect: list[Condition] = Field(default_factory=list, max_length=10)
    observe: Literal["auto", "always", "never"] = "auto"


_ACTIONS = TypeAdapter(list[Action])

# ---- Counting ----------------------------------------------------------------------


def _depth(actions: list[Any], level: int = 1) -> int:
    deepest = level
    for step in actions:
        if isinstance(step, If):
            deepest = max(deepest, _depth(step.then, level + 1), _depth(step.else_, level + 1))
        elif isinstance(step, Repeat):
            deepest = max(deepest, _depth(step.body, level + 1))
    return deepest


def count_steps(actions: list[Any]) -> int:
    """Worst-case executed steps with repeats expanded (the bound is on this)."""
    total = 0
    for step in actions:
        total += 1
        if isinstance(step, If):
            total += max(count_steps(step.then), count_steps(step.else_))
        elif isinstance(step, Repeat):
            total += step.times * count_steps(step.body)
    return total


def iter_steps(actions: list[Any]):
    """Every step at every depth (for static checks)."""
    for step in actions:
        yield step
        if isinstance(step, If):
            yield from iter_steps(step.then)
            yield from iter_steps(step.else_)
        elif isinstance(step, Repeat):
            yield from iter_steps(step.body)


def secrets_named(batch: ActionBatch) -> set[str]:
    return {s.secret for s in iter_steps(batch.actions) if isinstance(s, TypeText) and s.secret}


# ---- Parsing -------------------------------------------------------------------------


def _first_error(exc: ValidationError) -> str:
    err = exc.errors()[0]
    where = ".".join(str(p) for p in err.get("loc", ()) if not (isinstance(p, str) and p[0].isupper()))
    msg = str(err.get("msg", "invalid")).removeprefix("Value error, ")
    return f"{where}: {msg}" if where else msg


def parse_batch(
    *,
    obs: str,
    actions: Any,
    after: str = "wait_until_stable",
    expect: Any = None,
    observe: str = "auto",
    state: str = "",
    max_steps: int,
    secret_values: set[str] | None = None,
) -> ActionBatch:
    """Validate one batch completely, or raise :class:`DslError`. Nothing runs before this."""
    try:
        batch = ActionBatch.model_validate(
            {
                "obs": obs,
                "state": state or "",
                "actions": actions,
                "after": after or "wait_until_stable",
                "expect": expect or [],
                "observe": observe or "auto",
            }
        )
    except ValidationError as exc:
        raise DslError(_first_error(exc)) from None

    if _depth(batch.actions) > MAX_DEPTH:
        raise DslError(f"nesting deeper than {MAX_DEPTH} levels")
    steps = count_steps(batch.actions)
    if steps > max_steps:
        raise DslError(f"{steps} steps (repeats expanded) exceed the limit of {max_steps}; split the batch")

    if secret_values:
        for step in iter_steps(batch.actions):
            if (
                isinstance(step, TypeText)
                and step.text is not None
                and len(step.text) >= SECRET_LITERAL_MIN_LEN
                and step.text in secret_values
            ):
                raise DslError('type_text carries a secret value literally; use {"secret": NAME}', "secret_literal")
    return batch


def parse_conditions(raw: Any) -> list[Condition]:
    try:
        return TypeAdapter(list[Condition]).validate_python(raw or [])
    except ValidationError as exc:
        raise DslError(_first_error(exc)) from None


__all__ = [
    "AFTER_MODES",
    "BATCH_WALL_CLOCK_S",
    "KEY_NAMES",
    "MAX_DEPTH",
    "MAX_REPEAT",
    "MAX_TIMEOUT_MS",
    "OBSERVE_MODES",
    "ActionBatch",
    "Click",
    "Condition",
    "DoubleClick",
    "Drag",
    "DslError",
    "ElementQuery",
    "If",
    "Keypress",
    "Move",
    "Point",
    "Repeat",
    "RightClick",
    "Scroll",
    "TypeText",
    "WaitFor",
    "WaitForChange",
    "WaitUntilStable",
    "count_steps",
    "iter_steps",
    "parse_batch",
    "parse_conditions",
    "secrets_named",
]
