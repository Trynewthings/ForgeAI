"""Solvability tool binder (Phase 3b, deterministic).

Bridges the solvability LLM's subgoal enumeration with the concrete tool
catalog through a **structured** channel: subgoals declare their required
step_kind(s) (the registered 8-element enum), and the binder resolves
each step_kind to the set of tool ids in the envelope that advertise it.

This is the architectural cleanup flagged by the user: solvability should
not rely on free-form capability_class strings being normalised at
validation time. Instead:

    Phase A (LLM)         enumerate subgoals with step_kind-enum classes
    Phase B (binder)      step_kind → tool_ids via the envelope index
    Phase C (validator)   deterministic match_status + overall status

The binder is zero-LLM, no string similarity, no substring search. It
only accepts canonical step_kinds (the registered vocabulary). Anything
outside that vocabulary is dropped and reported as an unbindable class —
telling the caller that Phase A produced an invalid candidate, which
should trigger ``solvable_with_clarification`` downstream rather than a
false ``matched`` / ``not_solvable``.

No LLM imports — AST-guarded alongside solvability_validator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from app.schemas.solvability import CapabilityMatch, SolvabilityLLMCandidate


BINDER_VERSION = "front_chain_solvability_tool_binder_v1"


# Authoritative step_kind vocabulary — must stay in sync with the
# registry's LOGICAL_STEP_KINDS. Keeping it duplicated as a frozenset
# here avoids importing the capability service into the validator path
# (the AST guardrail forbids any *_service import from the validator).
CANONICAL_STEP_KINDS = frozenset(
    {"ingest", "transform", "summarize", "rank", "validate", "persist", "notify", "review"}
)


@dataclass
class BindingOutcome:
    """Result of binding one subgoal's required_capability_classes to
    concrete tool ids from the envelope."""

    subgoal_id: str
    required_step_kinds: list[str]
    # True when every required step_kind is covered by at least one
    # tool id in the envelope.
    fully_covered: bool
    # step_kinds that had no covering tool in the envelope.
    uncovered_step_kinds: list[str]
    # step_kinds the LLM emitted that aren't in the canonical set. The
    # binder refuses to guess what they mean; downstream uses their
    # presence to classify the whole match as "needs clarification".
    out_of_vocabulary_classes: list[str]
    # The actual tool ids selected for this subgoal (de-duplicated,
    # in insertion order).
    bound_tool_ids: list[str]


@dataclass
class BinderResult:
    bindings: list[BindingOutcome]
    binder_version: str = BINDER_VERSION


class SolvabilityToolBinder:
    """Deterministic binder. See module docstring for the full contract."""

    def bind(
        self,
        *,
        candidate: SolvabilityLLMCandidate,
        capability_envelope: Optional[Iterable[dict]] = None,
    ) -> BinderResult:
        envelope = list(capability_envelope or [])
        step_kind_index = _build_step_kind_index(envelope)

        # Index the LLM's subgoals by id so we can project through
        # capability_matches (if present) or fall back to the subgoal's
        # own required_capability_classes.
        subgoal_classes_by_id: dict[str, list[str]] = {
            sg.subgoal_id: list(sg.required_capability_classes)
            for sg in candidate.required_subgoals
        }
        bindings: list[BindingOutcome] = []
        seen_ids: set[str] = set()

        # Walk capability_matches first — they are the LLM's most
        # explicit signal. When the LLM omits a match for a subgoal we
        # still emit a binding using the subgoal's own declared classes.
        for match in candidate.capability_matches:
            classes = list(match.required_capability_classes) or subgoal_classes_by_id.get(
                match.subgoal_id, []
            )
            bindings.append(_bind_one(match.subgoal_id, classes, step_kind_index))
            seen_ids.add(match.subgoal_id)

        for subgoal in candidate.required_subgoals:
            if subgoal.subgoal_id in seen_ids:
                continue
            bindings.append(
                _bind_one(
                    subgoal.subgoal_id,
                    list(subgoal.required_capability_classes),
                    step_kind_index,
                )
            )

        return BinderResult(bindings=bindings)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_step_kind_index(envelope: list[dict]) -> dict[str, list[str]]:
    """step_kind → ordered list of capability_ids advertising it."""
    index: dict[str, list[str]] = {}
    seen_per_kind: dict[str, set[str]] = {}
    for entry in envelope:
        if not isinstance(entry, dict):
            continue
        cap_id = str(entry.get("capability_id") or entry.get("id") or "").strip()
        if not cap_id:
            continue
        for step_kind in (
            entry.get("supported_step_kinds") or entry.get("logical_step_kinds") or []
        ):
            sk = str(step_kind).strip()
            if sk not in CANONICAL_STEP_KINDS:
                continue
            seen = seen_per_kind.setdefault(sk, set())
            if cap_id in seen:
                continue
            seen.add(cap_id)
            index.setdefault(sk, []).append(cap_id)
    return index


def _bind_one(
    subgoal_id: str,
    raw_classes: list[str],
    step_kind_index: dict[str, list[str]],
) -> BindingOutcome:
    required: list[str] = []
    oov: list[str] = []
    for raw in raw_classes:
        cls = str(raw or "").strip().lower()
        if not cls:
            continue
        if cls in CANONICAL_STEP_KINDS:
            if cls not in required:
                required.append(cls)
        else:
            oov.append(raw)

    bound: list[str] = []
    seen_bound: set[str] = set()
    uncovered: list[str] = []
    for sk in required:
        covered = False
        for cap_id in step_kind_index.get(sk, []):
            if cap_id not in seen_bound:
                bound.append(cap_id)
                seen_bound.add(cap_id)
            covered = True
        if not covered:
            uncovered.append(sk)

    fully_covered = bool(required) and not uncovered and not oov
    return BindingOutcome(
        subgoal_id=subgoal_id,
        required_step_kinds=required,
        fully_covered=fully_covered,
        uncovered_step_kinds=uncovered,
        out_of_vocabulary_classes=oov,
        bound_tool_ids=bound,
    )


def derive_match_from_binding(binding: BindingOutcome) -> CapabilityMatch:
    """Project a binder outcome onto the CapabilityMatch schema.

    Status derivation (deterministic):
        * no required step_kinds AND no oov        → matched (trivial)
        * oov present                              → unmatched
                (LLM gave us a class we can't ground — don't guess)
        * fully_covered                            → matched
        * bound_tool_ids non-empty but uncovered   → partial
        * bound_tool_ids empty                     → unmatched
    """
    if binding.out_of_vocabulary_classes:
        status = "unmatched"
    elif not binding.required_step_kinds:
        status = "matched"
    elif binding.fully_covered:
        status = "matched"
    elif binding.bound_tool_ids:
        status = "partial"
    else:
        status = "unmatched"

    reasoning_bits: list[str] = []
    if binding.out_of_vocabulary_classes:
        reasoning_bits.append(
            f"out-of-vocabulary classes: {binding.out_of_vocabulary_classes}"
        )
    if binding.uncovered_step_kinds:
        reasoning_bits.append(
            f"uncovered step_kinds: {binding.uncovered_step_kinds}"
        )
    reasoning = "; ".join(reasoning_bits) if reasoning_bits else None

    return CapabilityMatch(
        subgoal_id=binding.subgoal_id,
        required_capability_classes=list(binding.required_step_kinds)
        or list(binding.out_of_vocabulary_classes),
        matched_capability_ids=list(binding.bound_tool_ids),
        match_status=status,
        reasoning=reasoning,
    )


__all__ = [
    "BINDER_VERSION",
    "BindingOutcome",
    "BinderResult",
    "SolvabilityToolBinder",
    "derive_match_from_binding",
    "CANONICAL_STEP_KINDS",
]
