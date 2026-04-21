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
    2. If there are no subgoals (Phase A empty/failed)   → solvable_with_clarification
    3. If any subgoal has a CAPABILITY gap (uncovered   → not_solvable
       step_kind or unresolved OOV, and gap_kind is
       ``capability``)
    4. If any subgoal has a JUDGMENT / APPROVAL /       → solvable_with_review
       VERIFICATION gap AND a reviewer insertion point    (when legal)
       is LEGAL (see _reviewer_insertion_legal)
    5. If any subgoal is still partial after rule 4     → not_solvable
       (no legal reviewer recourse)
    6. Otherwise (full coverage)                        → solvable

Rule 3 is the key change from the legacy validator: "needs human
review" no longer whitewashes a missing executable capability. Review
only substitutes when the subgoal itself was classified as needing
judgment/approval/verification (not tool execution) AND the runtime
policy allows a reviewer to be inserted.

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

        status = _derive_status(
            has_blocked_closure=has_blocked_closure,
            subgoals=subgoal_envelope.subgoals,
            capability_gap_subgoal_ids=capability_gap_subgoal_ids,
            judgment_gap_subgoal_ids=judgment_gap_subgoal_ids,
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
        )

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
    problem_formulation: ProblemFormulationContract,
    input_closure: InputClosureContract,
    output_closure: OutputClosureContract,
) -> str:
    if has_blocked_closure:
        return "solvable_with_clarification"
    if not subgoals:
        return "solvable_with_clarification"
    if capability_gap_subgoal_ids:
        # Hard rule: executable capability gaps cannot be rescued by
        # human review. A reviewer can judge, approve, or verify —
        # they cannot conjure a missing tool.
        return "not_solvable"
    if judgment_gap_subgoal_ids:
        if _reviewer_insertion_legal(
            problem_formulation, input_closure, output_closure
        ):
            return "solvable_with_review"
        return "not_solvable"
    # Any subgoal flagged as human_review_required even though the
    # binding was full counts as a "needs review" verdict.
    if any(sg.human_review_required for sg in subgoals):
        if _reviewer_insertion_legal(
            problem_formulation, input_closure, output_closure
        ):
            return "solvable_with_review"
        return "not_solvable"
    return "solvable"


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
