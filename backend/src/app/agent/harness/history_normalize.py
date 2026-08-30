"""Identity-uniqueness of the harness history store (PRP-0149 C1, UDR-0127 D2).

The harness saves history after EVERY service call
(``require_per_service_call_history_persistence=True``) and MAF's
``InMemoryHistoryProvider`` stores ``context.input_messages`` (``store_inputs=True``,
``agent_framework/_sessions.py:1019-1025``). From the second round of a turn those
input messages ARE the replayed transcript -- the very ``Message`` objects
``get_messages()`` returned from ``state["messages"]``. Each round therefore appends
the whole transcript to the store again (RES-0002 F1).

That is not merely wasteful. It makes the store STRUCTURALLY AMBIGUOUS, and the
consumer that breaks on it sits UPSTREAM of the request seam where the three previous
repairs were placed: MAF's compaction links a distant ``function_call`` to its result
only via ``_unambiguous_function_call_result_pairs``, which gives up on any
``call_id`` with more than one unmatched declaration
(``_compaction.py:105-122``, ``len(candidates) != 1 -> continue``). The declaration
and its result then land in different groups, ``ToolResultCompactionStrategy``
collapses one and keeps the other, and the request carries an output whose call no
longer exists:

    400 'No tool call found for shell call output with call_id call_...'

The request seam cannot repair that -- by then the declaration is not duplicated, it
is gone. So the repair belongs here, at the producer (UDR-0127 D1).

This module MUTATES what is stored, which is why it is NOT in ``history_debug``:
that module promises its readers "counts, ids and shapes" and a module that also
mutates its subject loses the guarantee that makes its own output trustworthy
(UDR-0127 D4, the rule UDR-0126 already applied to ``approval_debug``).

Nothing here may raise. On any internal error the messages are saved UNCHANGED: a
hygiene pass at the store must not become a new way for a turn to fail (UDR-0126 D5
posture).

SECOND DUTY -- IDENTITY ASSIGNMENT (PRP-0151 C1, UDR-0129 D2). MAF 1.15.0 added its
own de-duplication INSIDE ``save_messages`` (``_sessions.filter_new_messages``,
#7242) -- the same defect, fixed at the same seam this module wraps, but keyed
differently: it prefers ``message_id`` and otherwise falls back to a HASH OF THE
MESSAGE CONTENT. Because this module wraps the provider, upstream's rule runs
DOWNSTREAM of ours and wins, and a plain user ``Message`` carries no ``message_id``.
Measured against 1.15.0, a user who sends the same text twice in a row had the
second one silently dropped -- contradicting the guarantee published in the v0.138.0
release notes that identical typed messages are both retained (UDR-0127 D2).

The fix is not to override upstream or to duplicate it: the two rules AGREE whenever
an id is present and disagree only on the fallback. So this module now SUPPLIES the
key upstream's rule prefers. Every message leaving here carries a ``message_id``,
upstream takes its id branch, and its content branch becomes unreachable for
anything ChatWalaʻau saves. Both layers then reach the same verdict instead of one
suppressing the other.
"""

from __future__ import annotations

from collections.abc import Sequence  # noqa: TC003 -- runtime use in a public signature
import logging
from typing import Any
import uuid

from agent_framework import InMemoryHistoryProvider

logger = logging.getLogger("app.agent.harness_history")

# Prefix so an id this module assigned is distinguishable in a log or a session file
# from one MAF's compaction annotation assigned (``_ensure_message_ids``).
_ASSIGNED_ID_PREFIX = "cw-"


def message_identities(message: Any) -> frozenset[tuple[str, str]]:
    """The provider-assigned identities one message carries.

    ``function_call`` / ``function_result`` are keyed by ``call_id`` and
    ``text_reasoning`` by its id -- the same identities the request seam de-duplicates
    on one layer down (UDR-0126 D3). A message carrying none of them returns an EMPTY
    set and is therefore NEVER treated as a duplicate: identical text is legitimate
    and removing it deletes a real message (UDR-0126 D4, UDR-0127 D2).
    """
    identities: set[tuple[str, str]] = set()
    try:
        for content in getattr(message, "contents", None) or []:
            ctype = getattr(content, "type", None)
            if ctype in ("function_call", "function_result"):
                call_id = getattr(content, "call_id", None)
                if isinstance(call_id, str) and call_id:
                    identities.add((ctype, call_id))
            elif ctype == "text_reasoning":
                rs_id = getattr(content, "id", None) or getattr(content, "raw_representation_id", None)
                if isinstance(rs_id, str) and rs_id:
                    identities.add(("text_reasoning", rs_id))
    except Exception:  # a key function must never break a save
        logger.exception("[history normalize] failed to read message identities")
        return frozenset()
    return frozenset(identities)


def normalize(existing: Sequence[Any], incoming: Sequence[Any]) -> tuple[list[Any], list[str]]:
    """Return ``(messages_to_append, removed_descriptions)`` (UDR-0127 D2).

    A message is a duplicate when the store -- or an earlier message in the same
    incoming batch -- already holds it, decided in this order:

    1. OBJECT IDENTITY. The replayed input IS the stored object: ``get_messages``
       returns ``list(state["messages"])``, a new list holding the same ``Message``
       objects, and ``after_run`` hands those same objects back to ``save_messages``.
       This alone catches the reported duplication.
    2. ``message_id``, which MAF's own compaction annotation assigns
       (``_ensure_message_ids``).
    3. CONTENT IDENTITY -- the provider-assigned ids above -- which catches a copy
       that round-tripped through serialization and lost its object identity.

    A message whose identity set is only PARTIALLY present is NOT a duplicate. It is
    a shape this codebase has not seen and it is logged, never absorbed: the same
    posture UDR-0126 D7 takes for a duplicate whose fields differ.
    """
    seen_objects: set[int] = set()
    seen_message_ids: set[str] = set()
    seen_identities: set[tuple[str, str]] = set()

    for message in existing:
        seen_objects.add(id(message))
        message_id = getattr(message, "message_id", None)
        if isinstance(message_id, str) and message_id:
            seen_message_ids.add(message_id)
        seen_identities |= message_identities(message)

    kept: list[Any] = []
    removed: list[str] = []
    for message in incoming:
        message_id = getattr(message, "message_id", None)
        identities = message_identities(message)

        if id(message) in seen_objects:
            removed.append(f"object:{message_id or '-'}")
            continue
        if isinstance(message_id, str) and message_id and message_id in seen_message_ids:
            removed.append(f"message_id:{message_id}")
            continue
        if identities:
            already = identities & seen_identities
            if already == identities:
                removed.append("+".join(sorted(f"{kind}:{value}" for kind, value in sorted(identities))))
                continue
            if already:
                # UDR-0126 D7 posture: an unexplained overlap must surface rather than
                # be absorbed by a rule written for a different shape.
                logger.warning(
                    "[history normalize] message %s carries identities that are PARTIALLY "
                    "present in the store (%s of %s); keeping it. This is not the known "
                    "duplication.",
                    message_id or "-",
                    len(already),
                    len(identities),
                )

        seen_objects.add(id(message))
        if isinstance(message_id, str) and message_id:
            seen_message_ids.add(message_id)
        seen_identities |= identities
        kept.append(message)

    return kept, removed


def assign_message_ids(messages: Sequence[Any]) -> int:
    """Give every message a ``message_id`` it does not already have (UDR-0129 D2).

    Returns the number of ids assigned.

    Three properties are load-bearing, and each is a rule rather than a detail:

    1. **Never overwrite.** MAF's compaction annotation assigns ``message_id`` via
       ``_ensure_message_ids`` and GROUPS on it; overwriting would break its
       grouping, which is the very mechanism UDR-0127 D1 found the defect in. Only
       gaps are filled.
    2. **Never derived from content.** A content hash is the obvious way to make an
       id "stable", and it is exactly wrong: it would re-introduce content
       de-duplication under another name and defeat UDR-0127 D2 while appearing to
       honour it. Two messages with identical text MUST get DIFFERENT ids -- that is
       the whole point. UUID4 has no relationship to the message.
    3. **Assigned in place, once.** The id is written onto the message object, so a
       message that passes this seam again keeps the id it already has and object
       identity, ``message_id`` and upstream's identity all continue to agree.

    Nothing here may raise: an unassigned id costs the guarantee, an exception would
    cost the turn.
    """
    assigned = 0
    for message in messages or []:
        try:
            existing = getattr(message, "message_id", None)
            if isinstance(existing, str) and existing:
                continue
            message.message_id = f"{_ASSIGNED_ID_PREFIX}{uuid.uuid4().hex}"
            assigned += 1
        except Exception:  # a message that refuses an id is saved without one
            logger.exception("[history normalize] failed to assign a message_id")
    return assigned


def attach_history_normalization(agent: Any, *, thread_id: str) -> bool:
    """Wrap the save seam of MAF's own history provider instance (UDR-0119 D12).

    The instance the framework built is WRAPPED, never replaced: UDR-0119 D4's "the
    history provider parameter is OMITTED" is pinned by the PRP-0135 / PRP-0144
    invariants, and a correction must not require relaxing a shipped decision. This is
    the same technique PRP-0148 Section 4.1 used for the load-time observer, applied
    to the other end of the provider.

    Idempotent, and best-effort: returns True when the normaliser was attached. A
    failure here costs the guarantee, never a turn.
    """
    try:
        for provider in getattr(agent, "context_providers", None) or []:
            if not isinstance(provider, InMemoryHistoryProvider):
                continue
            if getattr(provider, "_chatwalaau_normalized", False):
                return True
            inner = provider.save_messages

            # `_inner` is bound as a default, not captured: the closure is created
            # inside a loop and a late-binding capture would make every wrapper call
            # the last provider's method (ruff B023).
            async def save_messages(
                session_id: str | None,
                messages: Any,
                *,
                state: Any = None,
                _inner: Any = inner,
                **kwargs: Any,
            ) -> Any:
                to_save = messages
                try:
                    if isinstance(state, dict):
                        existing = state.get("messages") or []
                        kept, removed = normalize(existing, list(messages or []))
                        if removed:
                            # UDR-0126 D5: a repair that erases its own evidence makes
                            # an unfixed producer invisible. The count is the signal.
                            logger.info(
                                "[history normalize] thread=%s dropped %d duplicate message(s) of %d offered: %s",
                                thread_id,
                                len(removed),
                                len(list(messages or [])),
                                "; ".join(removed[:20]) + ("; ..." if len(removed) > 20 else ""),
                            )
                            to_save = kept
                except Exception:  # normalisation must never break the save
                    logger.exception("[history normalize] failed; saving the messages unchanged")
                    to_save = messages
                # UDR-0129 D2: stamp identity AFTER the verdict, never before. Our
                # rules already decided what survives; the id exists so that MAF's
                # own de-duplication one layer down reaches the SAME verdict instead
                # of falling back to a content hash and deleting a real message.
                try:
                    assigned = assign_message_ids(list(to_save or []))
                    if assigned:
                        logger.debug(
                            "[history normalize] thread=%s assigned %d message_id(s)",
                            thread_id,
                            assigned,
                        )
                except Exception:  # identity assignment must never break the save
                    logger.exception("[history normalize] failed to assign message ids")
                return await _inner(session_id, to_save, state=state, **kwargs)

            provider.save_messages = save_messages  # type: ignore[method-assign]
            provider._chatwalaau_normalized = True  # type: ignore[attr-defined]
            return True
    except Exception:  # attaching must never break agent construction
        logger.exception("[history normalize] failed to attach the normaliser")
    return False


__all__ = ["attach_history_normalization", "message_identities", "normalize"]
