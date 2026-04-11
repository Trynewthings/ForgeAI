"""Solvability validator (Phase 3).

Deterministic validator that converts a ``SolvabilityLLMCandidate`` into a
full ``SolvabilityContract``. Its job is to:

    * Reject planner-structure leakage (R4 layer boundary).
      - subgoals arriving with ``graph_role`` attempts are cleared to the
        default ``transform`` (not preserved as planner authority).
      - any compatibility_step_refs / legacy_step_refs / edge / node_ordering
        content on the raw LLM payload is flagged and stripped.
    * Verify capability_matches reference real capability_ids from the
      provided capability registry projection.
    * Derive ``solvability_status`` deterministically:
        - not_solvable           if any required subgoal is unmatched AND
                                  unsatisfied_requirements is non-empty
        - solvable_with_clarification  if candidate has 0 subgoals or
                                  closure contracts still blocking
        - solvable_with_review   if partial matches exist
        - solvable               if every subgoal is matched
    * Derive a ``graph_feasibility`` assessment that is *descriptive*, not
      prescriptive — representable/runtime_supported/policy_supported flags
      without ever constructing an edge or node ordering.
    * NEVER overwrite admissibility — that is the next service's job.
    * NEVER call an LLM — this module must not import provider_runtime or
      any ``*_llm_service`` module (AST-guarded).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.schemas.solvability import (
    CapabilityMatch,
    GraphFeasibility,
    SolvabilityContract,
    SolvabilityLLMCandidate,
    SolvabilityLLMSubgoal,
    SolvabilitySubgoal,
)

VALIDATOR_VERSION = "front_chain_solvability_validator_v1"


# Fields on the raw candidate payload that must NEVER appear. These belong
# to planner/runtime layers and are stripped + recorded as issues.
_FORBIDDEN_PLANNER_FIELDS = frozenset(
    {
        "edges",
        "edge_drafts",
        "node_drafts",
        "node_instances",
        "execution_order",
        "graph_shape",
        "workflow_graph",
        "runtime_schedule",
        "compatibility_step_refs",
        "legacy_step_refs",
    }
)


@dataclass
class SolvabilityValidationIssue:
    axis: str
    kind: str  # "forbidden_field" | "unknown_capability_id" | "graph_role_leak" | ...
    detail: str


@dataclass
class SolvabilityValidationResult:
    contract: SolvabilityContract
    issues: list[SolvabilityValidationIssue] = field(default_factory=list)
    validator_version: str = VALIDATOR_VERSION


class SolvabilityValidator:
    """Deterministic candidate → contract converter."""

    def validate_and_produce(
        self,
        *,
        candidate: SolvabilityLLMCandidate,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        known_capability_ids: Optional[Iterable[str]] = None,
        raw_candidate_payload: Optional[dict[str, Any]] = None,
    ) -> SolvabilityValidationResult:
        issues: list[SolvabilityValidationIssue] = []

        # Strip forbidden planner fields from the raw payload if provided
        # (tests can pass a dict when they want to simulate leakage).
        if raw_candidate_payload is not None:
            _strip_forbidden_fields(raw_candidate_payload, issues)

        known_ids = set(known_capability_ids or [])

        # Subgoal validation — convert LLM subgoals to full
        # SolvabilitySubgoal objects with graph_role defaulted to
        # "transform" (never derived from the LLM).
        validated_subgoals: list[SolvabilitySubgoal] = []
        for llm_sg in candidate.required_subgoals:
            # Detect graph_role leakage if present on the raw dict
            raw_dict = llm_sg.model_dump()
            if "graph_role" in raw_dict:
                issues.append(
                    SolvabilityValidationIssue(
                        axis=llm_sg.subgoal_id,
                        kind="graph_role_leak",
                        detail=(
                            f"Subgoal {llm_sg.subgoal_id!r} carried graph_role from LLM; "
                            "stripped to enforce R4 layer boundary."
                        ),
                    )
                )
            validated_subgoals.append(
                SolvabilitySubgoal(
                    subgoal_id=llm_sg.subgoal_id,
                    label=llm_sg.label,
                    description=llm_sg.description,
                    subgoal_kind=llm_sg.subgoal_kind,
                    required_capability_classes=list(llm_sg.required_capability_classes),
                    input_evidence_kinds=list(llm_sg.input_evidence_kinds),
                    output_evidence_kinds=list(llm_sg.output_evidence_kinds),
                    human_review_required=llm_sg.human_review_required,
                    graph_role="transform",  # always default; planner will decide
                )
            )

        # Capability match validation — drop capability_ids not present in
        # the registry, mark as partial/unmatched if coverage drops.
        validated_matches: list[CapabilityMatch] = []
        for match in candidate.capability_matches:
            filtered_ids = [cid for cid in match.matched_capability_ids if not known_ids or cid in known_ids]
            unknown_ids = [cid for cid in match.matched_capability_ids if known_ids and cid not in known_ids]
            for unknown in unknown_ids:
                issues.append(
                    SolvabilityValidationIssue(
                        axis=match.subgoal_id,
                        kind="unknown_capability_id",
                        detail=f"Capability {unknown!r} is not present in the registry; dropped.",
                    )
                )
            new_status = _recompute_match_status(
                required_classes=match.required_capability_classes,
                matched_ids=filtered_ids,
                original_status=match.match_status,
            )
            validated_matches.append(
                CapabilityMatch(
                    subgoal_id=match.subgoal_id,
                    required_capability_classes=list(match.required_capability_classes),
                    matched_capability_ids=filtered_ids,
                    match_status=new_status,
                    reasoning=match.reasoning,
                )
            )

        # Build unsatisfied requirements — union of LLM output + any
        # subgoal whose match_status is unmatched.
        unsatisfied: list[str] = list(candidate.unsatisfied_requirements)
        for match in validated_matches:
            if match.match_status == "unmatched":
                for req in match.required_capability_classes:
                    marker = f"capability_class:{req}"
                    if marker not in unsatisfied:
                        unsatisfied.append(marker)
        unsafe = list(candidate.unsafe_requirements)

        # Required reviews — derived purely from subgoal flags.
        required_reviews: list[str] = [
            sg.subgoal_id for sg in validated_subgoals if sg.human_review_required
        ]

        # Graph feasibility — descriptive only. We do NOT construct edges.
        graph_feasibility = _derive_graph_feasibility(
            candidate=candidate,
            subgoal_count=len(validated_subgoals),
            unsatisfied_count=len(unsatisfied),
        )

        # Derive solvability status deterministically.
        status = _derive_solvability_status(
            subgoals=validated_subgoals,
            matches=validated_matches,
            unsatisfied=unsatisfied,
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
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
            capability_matches=validated_matches,
            unsatisfied_requirements=unsatisfied,
            unsafe_requirements=unsafe,
            required_reviews=required_reviews,
            graph_feasibility=graph_feasibility,
            llm_candidate_used=True,
            llm_candidate_repaired=bool(issues),
            reasoning_summary=candidate.reasoning_summary,
        )

        return SolvabilityValidationResult(contract=contract, issues=issues)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_forbidden_fields(
    payload: dict[str, Any], issues: list[SolvabilityValidationIssue]
) -> None:
    for key in list(payload.keys()):
        if key in _FORBIDDEN_PLANNER_FIELDS:
            issues.append(
                SolvabilityValidationIssue(
                    axis=key,
                    kind="forbidden_field",
                    detail=(
                        f"Field {key!r} is not allowed on a solvability candidate; "
                        "stripped to enforce R4 layer boundary."
                    ),
                )
            )
            payload.pop(key, None)


def _recompute_match_status(
    *,
    required_classes: list[str],
    matched_ids: list[str],
    original_status: str,
) -> str:
    if not required_classes:
        return "matched"
    if not matched_ids:
        return "unmatched"
    if len(matched_ids) >= len(required_classes):
        return "matched"
    return "partial"


def _derive_solvability_status(
    *,
    subgoals: list[SolvabilitySubgoal],
    matches: list[CapabilityMatch],
    unsatisfied: list[str],
    problem_formulation: ProblemFormulationContract,
    input_closure: InputClosureContract,
    output_closure: OutputClosureContract,
) -> str:
    # If any closure layer is still blocking, we're stuck at the
    # clarification stage regardless of what the solvability LLM returned.
    if (
        problem_formulation.blocking_axes
        or input_closure.blocking_axes
        or output_closure.blocking_axes
    ):
        return "solvable_with_clarification"
    if not subgoals:
        return "solvable_with_clarification"
    if any(m.match_status == "unmatched" for m in matches):
        return "not_solvable"
    if any(m.match_status == "partial" for m in matches):
        return "solvable_with_review"
    if any(sg.human_review_required for sg in subgoals):
        return "solvable_with_review"
    if unsatisfied:
        return "not_solvable"
    return "solvable"


def _derive_graph_feasibility(
    *,
    candidate: SolvabilityLLMCandidate,
    subgoal_count: int,
    unsatisfied_count: int,
) -> GraphFeasibility:
    shape = candidate.candidate_shape_hint
    representable = subgoal_count > 0 and unsatisfied_count == 0
    runtime_supported = representable
    policy_supported = representable
    blocking_reasons: list[str] = []
    if subgoal_count == 0:
        blocking_reasons.append("no proof subgoals were emitted")
    if unsatisfied_count > 0:
        blocking_reasons.append(f"{unsatisfied_count} unsatisfied requirements")
    return GraphFeasibility(
        candidate_shape=shape,
        representable=representable,
        runtime_supported=runtime_supported,
        policy_supported=policy_supported,
        blocking_reasons=blocking_reasons,
    )
