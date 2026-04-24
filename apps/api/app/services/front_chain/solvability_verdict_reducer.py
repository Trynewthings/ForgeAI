"""Solvability Phase C — Verdict Reducer (deterministic, 0 LLM).

Consumes:
    * SubgoalDraftEnvelope  — Phase A output
    * ToolBindingEnvelope   — Phase B output
    * The three closure contracts — authoritative for "is the problem
      even ready to be judged for solvability yet?"

Produces:
    * SolvabilityContract — the byte-equal contract shape the planner
      downstream already consumes. This is the sole mapping point from
      the internal ``required_step_kinds`` vocabulary to the historical
      ``required_capability_classes`` field that compile_context and
      the planner use.

Strict verdict rules (order matters):
    1. If any closure layer is still blocking            → solvable_with_clarification
    1b. If PF.intent_clarity != "clear"                  → solvable_with_clarification
    1c. If IC.closure_status == input_clarification_required
        (and IC is applicable) OR
        OC.closure_status == output_clarification_required → solvable_with_clarification
    2. If there are no subgoals (Phase A empty/failed)   → solvable_with_clarification
    3. If any subgoal has a CAPABILITY gap                → not_solvable
    4. If any subgoal has a JUDGMENT / APPROVAL /        → solvable_with_review
       VERIFICATION gap AND a reviewer insertion point     (when legal)
       is LEGAL (see _reviewer_insertion_legal)
    5. If any subgoal is still partial after rule 4      → not_solvable
       (no legal reviewer recourse)
    6. **Vagueness signals** (downgrade solvable → review):
       6a. resolved_source_locator / delivery_target are not
           path-shaped and not "not_applicable"
       6b. > 50% of subgoals are bound only to coding_agent /
           general_agentic_reasoning (universal escape hatch)
       6c. PF / IC / OC have any review_axes set
       6d. Any subgoal flagged human_review_required
    7. Otherwise (full coverage, no vagueness)            → solvable

Rules 1b, 1c, and 6 are the additions that catch tasks the system
*can* schedule but probably shouldn't run unattended — e.g. "produce
a RAG intro" with no audience / format / length. Without these, the
LLM-judged grader (Tier 4) would have to be the only safety net,
burning tokens on uncertain runs.

No LLM imports. AST-guarded identically to the legacy validator.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.schemas.solvability import (
    CapabilityMatch,
    GraphFeasibility,
    SolvabilityContract,
    SolvabilitySubgoal,
    SubgoalDraft,
    SubgoalDraftEnvelope,
    ToolBinding,
    ToolBindingEnvelope,
)


REDUCER_VERSION = "front_chain_solvability_verdict_reducer_v1"


@dataclass
class ReducerResult:
    contract: SolvabilityContract
    reducer_version: str = REDUCER_VERSION


class SolvabilityVerdictReducer:
    """Deterministic Phase C."""

    def reduce(
        self,
        *,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        subgoal_envelope: SubgoalDraftEnvelope,
        binding_envelope: ToolBindingEnvelope,
    ) -> ReducerResult:
        subgoals_by_id = {sg.subgoal_id: sg for sg in subgoal_envelope.subgoals}
        bindings_by_id = {b.subgoal_id: b for b in binding_envelope.bindings}

        # Project each pair into the external SolvabilitySubgoal +
        # CapabilityMatch shape as we go.
        validated_subgoals: list[SolvabilitySubgoal] = []
        capability_matches: list[CapabilityMatch] = []
        unsatisfied: list[str] = []
        required_reviews: list[str] = []

        has_blocked_closure = bool(
            problem_formulation.blocking_axes
            or input_closure.blocking_axes
            or output_closure.blocking_axes
        )

        # Tally gap classifications.
        capability_gap_subgoal_ids: list[str] = []
        judgment_gap_subgoal_ids: list[str] = []

        for sg in subgoal_envelope.subgoals:
            binding = bindings_by_id.get(sg.subgoal_id)
            match = _project_match(subgoal=sg, binding=binding)
            capability_matches.append(match)
            validated_subgoals.append(_project_subgoal(sg))

            if match.match_status == "unmatched" or (
                binding and binding.unresolved_oov
            ):
                if _gap_is_capability(sg):
                    capability_gap_subgoal_ids.append(sg.subgoal_id)
                    unsatisfied.append(
                        f"capability_gap:{sg.subgoal_id}"
                    )
                else:
                    judgment_gap_subgoal_ids.append(sg.subgoal_id)
            elif match.match_status == "partial":
                if _gap_is_capability(sg):
                    capability_gap_subgoal_ids.append(sg.subgoal_id)
                else:
                    judgment_gap_subgoal_ids.append(sg.subgoal_id)

            if sg.human_review_required:
                required_reviews.append(sg.subgoal_id)

        status, vagueness_reasons = _derive_status(
            has_blocked_closure=has_blocked_closure,
            subgoals=subgoal_envelope.subgoals,
            capability_gap_subgoal_ids=capability_gap_subgoal_ids,
            judgment_gap_subgoal_ids=judgment_gap_subgoal_ids,
            capability_matches=capability_matches,
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
        )
        # Surface vagueness signals through the existing
        # ``unsatisfied_requirements`` channel so the planner /
        # frontend can read them without a schema change.
        unsatisfied.extend(vagueness_reasons)

        graph_feasibility = _derive_graph_feasibility(
            subgoal_envelope=subgoal_envelope,
            unsatisfied_count=len(unsatisfied),
        )

        contract = SolvabilityContract(
            problem_kind=problem_formulation.interpreted_problem_kind,
            planning_intent_id=(
                problem_formulation.planning_intent.intent_id
                if problem_formulation.planning_intent is not None
                else None
            ),
            solvability_status=status,
            required_subgoals=validated_subgoals,
            capability_matches=capability_matches,
            unsatisfied_requirements=unsatisfied,
            unsafe_requirements=list(subgoal_envelope.unsafe_requirements),
            required_reviews=required_reviews,
            graph_feasibility=graph_feasibility,
            llm_candidate_used=True,
            llm_candidate_repaired=False,
            reasoning_summary=subgoal_envelope.reasoning_summary,
        )
        return ReducerResult(contract=contract)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _project_subgoal(sg: SubgoalDraft) -> SolvabilitySubgoal:
    """Map the internal SubgoalDraft onto the external SolvabilitySubgoal.

    This is the one place ``required_step_kinds`` (internal) →
    ``required_capability_classes`` (external contract) crosses the
    boundary. The external name is preserved for byte-equal planner
    compatibility.
    """
    return SolvabilitySubgoal(
        subgoal_id=sg.subgoal_id,
        label=sg.label,
        description=sg.description,
        subgoal_kind=sg.subgoal_kind,  # type: ignore[arg-type]
        required_capability_classes=list(sg.required_step_kinds),
        input_evidence_kinds=list(sg.input_evidence_kinds),
        output_evidence_kinds=list(sg.output_evidence_kinds),
        human_review_required=sg.human_review_required,
        graph_role="transform",  # planner assigns real roles
    )


def _project_match(
    *, subgoal: SubgoalDraft, binding: Optional[ToolBinding]
) -> CapabilityMatch:
    if binding is None:
        return CapabilityMatch(
            subgoal_id=subgoal.subgoal_id,
            required_capability_classes=list(subgoal.required_step_kinds),
            matched_capability_ids=[],
            match_status="unmatched",
            reasoning="No binding produced by Phase B.",
        )
    return CapabilityMatch(
        subgoal_id=binding.subgoal_id,
        required_capability_classes=list(binding.required_step_kinds),
        matched_capability_ids=list(binding.bound_tool_ids),
        match_status=_coverage_to_match_status(binding.coverage),
        reasoning=binding.reasoning,
    )


def _coverage_to_match_status(coverage: str) -> str:
    if coverage == "full":
        return "matched"
    if coverage == "partial":
        return "partial"
    return "unmatched"


def _gap_is_capability(subgoal: SubgoalDraft) -> bool:
    """Decide whether this subgoal's gap is a capability gap or a
    judgment-class gap. Capability gaps cannot be patched by a reviewer
    — the reducer uses this distinction to avoid whitewashing missing
    execution with ``solvable_with_review``.
    """
    return subgoal.gap_kind == "capability"


def _reviewer_insertion_legal(
    problem_formulation: ProblemFormulationContract,
    input_closure: InputClosureContract,
    output_closure: OutputClosureContract,
) -> bool:
    """Whether a human reviewer gate can be inserted for this task.

    Reviewer insertion is illegal when:
      * ``problem_formulation.current_system_fit == "out_of_scope"``
        (the task itself is out of scope for the system, no amount of
        review helps).
      * the input_closure is ``not_applicable`` AND there is no
        deliverable — nothing to review.

    Otherwise we assume a reviewer can be inserted at the subgoal's
    natural gate point. When the admissibility layer later disagrees
    it can still reject — this is the front-chain's best-effort check.
    """
    if problem_formulation.current_system_fit == "out_of_scope":
        return False
    if (
        input_closure.applicability == "not_applicable"
        and not output_closure.deliverable_form
    ):
        return False
    return True


def _derive_status(
    *,
    has_blocked_closure: bool,
    subgoals: list[SubgoalDraft],
    capability_gap_subgoal_ids: list[str],
    judgment_gap_subgoal_ids: list[str],
    capability_matches: list[CapabilityMatch],
    problem_formulation: ProblemFormulationContract,
    input_closure: InputClosureContract,
    output_closure: OutputClosureContract,
) -> tuple[str, list[str]]:
    """Decide solvability status + collect vagueness reasons.

    Returns ``(status, vagueness_reasons)``. The reasons are short
    machine-readable strings the caller appends to
    ``unsatisfied_requirements`` so the planner / frontend can render
    them as concrete prompts (e.g. "input_locator_not_concrete").
    """
    # Tier 1: PF-level blockers --------------------------------------
    if has_blocked_closure:
        return "solvable_with_clarification", ["closure_blocking_axes"]
    if problem_formulation.intent_clarity in {"ambiguous", "missing_goal"}:
        return (
            "solvable_with_clarification",
            [f"intent_clarity:{problem_formulation.intent_clarity}"],
        )

    # Tier 2: IC / OC closure status (LLM may set status without
    # populating blocking_axes — read the status field directly).
    if (
        input_closure.applicability != "not_applicable"
        and input_closure.closure_status == "input_clarification_required"
    ):
        return "solvable_with_clarification", ["input_closure_clarification_required"]
    if output_closure.closure_status == "output_clarification_required":
        return "solvable_with_clarification", ["output_closure_clarification_required"]

    # Tier 3: Phase A produced no proof units at all.
    if not subgoals:
        return "solvable_with_clarification", ["no_subgoals"]

    # Tier 4: Capability gaps (hard fail, no reviewer recourse).
    if capability_gap_subgoal_ids:
        return (
            "not_solvable",
            [f"capability_gap:{','.join(capability_gap_subgoal_ids)}"],
        )

    # Tier 5: Judgment / review gaps (reviewer recourse if legal).
    if judgment_gap_subgoal_ids:
        if _reviewer_insertion_legal(
            problem_formulation, input_closure, output_closure
        ):
            return (
                "solvable_with_review",
                [f"judgment_gap:{','.join(judgment_gap_subgoal_ids)}"],
            )
        return "not_solvable", [f"judgment_gap_no_reviewer:{','.join(judgment_gap_subgoal_ids)}"]

    # Tier 6: Vagueness signals — full coverage but the task itself
    # is loose enough that running unattended is risky.
    vagueness: list[str] = []

    if (
        input_closure.applicability != "not_applicable"
        and not _locator_looks_concrete(input_closure.resolved_source_locator)
    ):
        vagueness.append(
            f"input_locator_not_concrete:{(input_closure.resolved_source_locator or '')[:40]}"
        )
    if not _locator_looks_concrete(output_closure.delivery_target):
        vagueness.append(
            f"output_target_not_concrete:{(output_closure.delivery_target or '')[:40]}"
        )

    if subgoals:
        pure_agentic = sum(1 for m in capability_matches if _is_pure_agentic(m))
        ratio = pure_agentic / len(subgoals)
        if ratio > 0.5:
            vagueness.append(
                f"pure_agentic_majority:{pure_agentic}/{len(subgoals)}"
            )

    review_axis_axes: list[str] = []
    review_axis_axes.extend(problem_formulation.review_axes or [])
    review_axis_axes.extend(input_closure.review_axes or [])
    review_axis_axes.extend(output_closure.review_axes or [])
    if review_axis_axes:
        vagueness.append(f"review_axes:{','.join(review_axis_axes)}")

    if any(sg.human_review_required for sg in subgoals):
        vagueness.append("human_review_required_subgoal")

    if vagueness:
        if _reviewer_insertion_legal(
            problem_formulation, input_closure, output_closure
        ):
            return "solvable_with_review", vagueness
        return "not_solvable", vagueness

    return "solvable", []


# Locator concreteness — same heuristic as
# planner_output_bindings._looks_like_path. Duplicated rather than
# imported to keep the front-chain layer free of build-side imports.
_PLACEHOLDER_LOCATORS = {
    "none", "n/a", "null", "未指定", "无", "n/a.",
    "用户描述", "当前工作目录", "当前目录",
    "unspecified", "unknown", "tbd", "todo", "placeholder",
}


# Concrete "inline" delivery channels — not paths, but authoritative
# destinations the runtime understands. The OC interpreter prompt lists
# these as acceptable non-path values for delivery_target; the
# concreteness heuristic must treat them as concrete too.
_CONCRETE_INLINE_CHANNELS = {
    "stdout", "stderr",
    "user", "user_inline", "user_chat", "chat",
    "inline", "response",
}


def _locator_looks_concrete(raw: Optional[str]) -> bool:
    """True when the locator is a real-looking path / URL.

    Empty or missing → True (treated as "not declared, not vague" —
    the closure layer's applicability flag carries that signal).
    """
    if not raw:
        return True
    locator = str(raw).strip()
    if not locator:
        return True
    lowered = locator.lower()
    if lowered in _PLACEHOLDER_LOCATORS:
        return False
    if lowered in _CONCRETE_INLINE_CHANNELS:
        return True
    # Substring match for the common Chinese placeholder phrases.
    for needle in _PLACEHOLDER_LOCATORS:
        if needle and len(needle) >= 3 and needle in lowered:
            return False
    if locator.startswith(("/", "./", "../", "~", "http://", "https://", "file://", "s3://", "mailto:")):
        return True
    if len(locator) >= 3 and locator[1:3] == ":\\":
        return True
    if " " not in locator and len(locator) <= 80 and "." in locator and locator.isascii():
        return True
    return False


_AGENTIC_FALLBACK_IDS = frozenset({
    "coding_agent",
    "general_agentic_reasoning",
})


def _is_pure_agentic(match: CapabilityMatch) -> bool:
    """True when this match is covered ONLY by the universal LLM tools."""
    ids = list(match.matched_capability_ids or [])
    if not ids:
        return False
    return all(cid in _AGENTIC_FALLBACK_IDS for cid in ids)


def _derive_graph_feasibility(
    *,
    subgoal_envelope: SubgoalDraftEnvelope,
    unsatisfied_count: int,
) -> GraphFeasibility:
    subgoal_count = len(subgoal_envelope.subgoals)
    representable = subgoal_count > 0 and unsatisfied_count == 0
    blocking_reasons: list[str] = []
    if subgoal_count == 0:
        blocking_reasons.append("no proof subgoals emitted")
    if unsatisfied_count > 0:
        blocking_reasons.append(f"{unsatisfied_count} unsatisfied requirements")
    return GraphFeasibility(
        candidate_shape=subgoal_envelope.candidate_shape_hint,
        representable=representable,
        runtime_supported=representable,
        policy_supported=representable,
        blocking_reasons=blocking_reasons,
    )


__all__ = ["REDUCER_VERSION", "ReducerResult", "SolvabilityVerdictReducer"]
