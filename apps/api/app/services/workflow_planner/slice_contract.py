"""Shared "what can actually run" contract for the session-anchored slice.

The session-anchored C-slice runtime only executes a small whitelist of
capabilities. Every upstream stage that picks capability ids — planner LLM
prompt, deterministic capability binder, constraint enforcer, build-side
binding adapter — needs to honour the same set, otherwise we get the
silent failure we hit with the "1-9 multiplication table" task: planner
picks ``source_structure_extract``, build accepts it, runtime can't
execute it.

This module is the single source of truth. ``planner_output_bindings``
imports the same constants so both sides stay in lockstep.
"""
from __future__ import annotations

from typing import Iterable

# Universal generalist. Used as the fallback when a step's logical kind is
# satisfiable in principle by an LLM-authored script but the planner's
# specialised pick lands outside the slice.
CODING_AGENT_CAPABILITY_ID = "coding_agent"

# Capabilities the session-anchored slice can actually invoke at runtime.
# Keep in sync with ``planner_output_bindings._SESSION_RUNNABLE_IDS``.
SESSION_ANCHORED_RUNNABLE_CAPABILITY_IDS: frozenset[str] = frozenset({
    CODING_AGENT_CAPABILITY_ID,
    "filesystem_read",
    "filesystem_write",
    # Deterministic verifier — the slice binds it to ``session_anchored_verify``
    # rather than the registry's default workload, so it counts as runnable.
    "validate_schema",
})

# Logical step kinds whose generalist fallback is ``coding_agent``. For
# ``act`` (write-side) we deliberately do NOT auto-fallback — writes need
# an explicit destination, which only ``filesystem_write`` carries.
_CODING_AGENT_FALLBACK_STEP_KINDS: frozenset[str] = frozenset({
    "transform",
    "ingest",
    "summarize",
    "validate",  # coding_agent can author a validation script when no specialised verifier matches
    "persist",   # coding_agent can persist via fs.write inside its sandbox
})


def is_runnable(capability_id: str | None) -> bool:
    """True if a capability id can be executed in the session-anchored slice."""
    return bool(capability_id) and capability_id in SESSION_ANCHORED_RUNNABLE_CAPABILITY_IDS


def coding_agent_fallback_for(step_kind: str | None) -> str | None:
    """Return ``coding_agent`` if it's an acceptable fallback for ``step_kind``.

    Returns None for step kinds where coding_agent isn't a sensible default
    (e.g. ``act`` needs an explicit write target; ``human_gate`` is
    structural; ``verify`` has its own deterministic path).
    """
    if step_kind and step_kind in _CODING_AGENT_FALLBACK_STEP_KINDS:
        return CODING_AGENT_CAPABILITY_ID
    return None


def filter_step_kind_preferences(
    preferences: dict[str, list[str]],
    *,
    has_input_source: bool = True,
) -> dict[str, list[str]]:
    """Project ``step_kind → [capability_id]`` down to runnable picks only.

    Within each kind, preserve the original order. ``coding_agent`` is
    always reachable as a fallback for kinds it can cover.

    When ``has_input_source`` is False (parameters-only / pure-compute
    task), strip ``filesystem_read`` from transform/ingest pools so the
    planner doesn't pick it for a step that has nothing to read — the
    common case where the LLM mistakes "compute X" for "fetch X".

    The deterministic binder reads from this dict; the LLM prompt also
    receives it so both sides see the same constrained universe.
    """
    out: dict[str, list[str]] = {}
    for kind, candidates in preferences.items():
        runnable = [cap for cap in candidates if is_runnable(cap)]
        if not has_input_source and kind in {"transform", "ingest"}:
            runnable = [cap for cap in runnable if cap != "filesystem_read"]
        ca_fallback = coding_agent_fallback_for(kind)
        if ca_fallback and ca_fallback not in runnable:
            runnable.append(ca_fallback)
        if not runnable:
            continue
        out[kind] = runnable
    for kind in _CODING_AGENT_FALLBACK_STEP_KINDS:
        out.setdefault(kind, [CODING_AGENT_CAPABILITY_ID])
    return out


def runnable_capability_ids(*, has_input_source: bool = True) -> list[str]:
    """Stable-sorted projection for prompt / log output.

    When ``has_input_source`` is False (parameters-only / synthesis
    task), ``filesystem_read`` is dropped — there's nothing on disk for
    it to read. This keeps the planner from picking ``filesystem_read``
    for a transform step whose "input" is the user's own description.
    """
    ids = set(SESSION_ANCHORED_RUNNABLE_CAPABILITY_IDS)
    if not has_input_source:
        ids.discard("filesystem_read")
    return sorted(ids)


def runnable_capability_id_set(*, has_input_source: bool = True) -> frozenset[str]:
    """Same shape as the constant but input-source-aware.

    Used by ``planner_context_builder`` to filter the capability envelope
    before the binder sees it, so upstream scoring only ranks things
    that can actually run given this session's input state.
    """
    return frozenset(runnable_capability_ids(has_input_source=has_input_source))


__all__: Iterable[str] = (
    "CODING_AGENT_CAPABILITY_ID",
    "SESSION_ANCHORED_RUNNABLE_CAPABILITY_IDS",
    "coding_agent_fallback_for",
    "filter_step_kind_preferences",
    "is_runnable",
    "runnable_capability_ids",
)
