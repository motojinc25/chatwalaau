"""Two-axis token measurement for the CTR-0009 usage event (PRP-0157, UDR-0135).

A turn emits ONE MAF usage content per MODEL CALL, not per turn. The two numbers a
reader wants from that stream are different quantities and cannot be served by one
field (UDR-0135 D1):

* the BILLING axis is the turn CUMULATIVE -- every model call of every approval
  round -- and is what a cost ledger and day/month statistics need;
* the CONTEXT axis is the OCCUPANCY the NEXT message starts from, and is derived
  from the LAST model call only.

Accumulation itself stays at the seam in ``app.agui.endpoint`` (one
``add_usage_details`` call, UDR-0135 D2). This module owns the two DERIVATIONS,
which are pure functions of an accumulated / last ``UsageDetails`` plus one
provider property, so both branches are directly testable.

Provider convention (UDR-0135 D5): the OpenAI family counts prompt-cache READ
tokens INSIDE ``input_token_count``; Anthropic reports it EXCLUSIVE of cache_read
and cache_creation. ``providers.input_tokens_include_cache_read`` is the single
declaration of that difference.

Every function here honours UDR-0135 D7: a measurement the provider did not report
stays ABSENT. Zero never stands in for unknown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

# MAF 1.16.0 normalized UsageDetails keys (agent_framework._types.UsageDetails).
# The provider clients map their own SDK shapes onto these, so the seam reads
# these and not the provider-prefixed extras (which stay in the event for
# traceability).
INPUT = "input_token_count"
OUTPUT = "output_token_count"
CACHE_READ = "cache_read_input_token_count"
CACHE_WRITE = "cache_creation_input_token_count"
REASONING = "reasoning_output_token_count"


def _int_or_none(usage: Mapping[str, Any] | None, key: str) -> int | None:
    """Read ``key`` as an int, or None when absent / not an integer.

    ``UsageDetails`` is a non-closed TypedDict whose values are declared
    ``int | None``, so an absent key and an explicit null are the same fact: not
    reported.
    """
    if not usage:
        return None
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def context_base_tokens(last: Mapping[str, Any] | None, *, includes_cache_read: bool) -> int | None:
    """Context occupancy the NEXT message starts from (UDR-0135 D4).

    ``last`` is the LAST model call's usage of the turn -- NOT the turn cumulative.
    Summing input across calls would measure billing, never occupancy.

    The assistant's own output joins the history, so it belongs in the base for the
    next turn; this preserves the intent of UDR-0008 D2 while correcting its
    arithmetic. Cache-read and cache-creation tokens occupy the window whether or
    not the provider counts them inside ``input_token_count``, so they are added
    back exactly when it does not.

    Returns None when the provider reported no input count -- CTR-0041 then falls
    back to its legacy formula rather than rendering a false zero (D7).
    """
    base = _int_or_none(last, INPUT)
    if base is None:
        return None
    base += _int_or_none(last, OUTPUT) or 0
    if not includes_cache_read:
        base += _int_or_none(last, CACHE_READ) or 0
        base += _int_or_none(last, CACHE_WRITE) or 0
    return base


def turn_summary(
    turn: Mapping[str, Any] | None,
    *,
    model_calls: int,
    includes_cache_read: bool,
) -> dict[str, int] | None:
    """The billing axis: what the WHOLE turn consumed (UDR-0135 D1/D6).

    ``turn`` is the accumulated ``UsageDetails`` -- every model call of every
    approval round, summed at the seam with ``add_usage_details``.

    Price points are kept SEPARATE, not merged: cache reads and cache writes are
    billed at different rates from ordinary input, so a ledger cannot recover them
    from a single input figure. ``uncached_input_token_count`` normalizes the one
    difference that is not observable from the numbers themselves -- whether the
    reported input already contains the cache reads.

    No ``total_token_count`` is published (D6): the Anthropic client sets none, and
    a computed total would mean different things on different providers. Consumers
    add the components they need.

    Returns None when nothing was measured, so the event carries no empty object.
    """
    if not turn or model_calls <= 0:
        return None

    summary: dict[str, int] = {}
    for key in (INPUT, OUTPUT, CACHE_READ, CACHE_WRITE, REASONING):
        value = _int_or_none(turn, key)
        if value is not None:
            summary[key] = value

    if not summary:
        return None

    # Full-price input. Omitted when the provider reported no cache reads: the
    # reported input then already IS the uncached count, and echoing it under a
    # second name would assert a cache measurement that was never made.
    raw_input = summary.get(INPUT)
    cache_read = summary.get(CACHE_READ)
    if raw_input is not None and cache_read is not None:
        uncached = raw_input - cache_read if includes_cache_read else raw_input
        summary["uncached_input_token_count"] = max(uncached, 0)

    summary["model_calls"] = model_calls
    return summary
