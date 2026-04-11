"""Execution admissibility service (Phase 3).

Fully deterministic, zero-LLM policy engine that decides which execution
regime a solvable problem is admitted under. The task brief requires
``solvable != admissible``: a problem the solvability layer deems
"technically workable" may still be admitted only as plan_only or
human_gated.

Policy table (priority order — first match wins):

    solvability.solvability_status == "not_solvable"
      → admissibility_status = "unsupported"
      → primary_execution_regime = "analysis_only"
      → max_automation_level = "plan_only"

    any closure layer still has blocking_axes
      → admissibility_status = "review_needed"
      → primary_execution_regime = "analysis_only"
      → max_automation_level = "plan_only"
      → reason = objective_under_specified / deliverable_not_clear

    solvability has any capability_match with status "unmatched"
      → admissibility_status = "unsupported"
      → primary_execution_regime = "analysis_only"
      → max_automation_level = "plan_only"
      → reason = capability_gap_detected

    solvability has any capability_match with status "partial"
      OR any subgoal with human_review_required=True
      → admissibility_status = "review_needed"
      → primary_execution_regime = "human_gated"
      → max_automation_level = "human_gated"
      → reason = human_judgment_required

    closure layers all closed, all matches matched, no human review
      → admissibility_status = "supported"
      → primary_execution_regime = "workflow_build"
      → max_automation_level = "full_workflow"

This module MUST NOT import provider_runtime or any *_llm_service module;
the AST guardrail in test_front_chain_phase3 enforces this.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.schemas.solvability import SolvabilityContract
from app.schemas.workflow_native import (
    ArtifactabilityAssessment,
    ExecutionAdmissibilityContract,
    HumanCheckpoint,
    WorkflowabilityAssessment,
)

ADMISSIBILITY_VERSION = "front_chain_execution_admissibility_v1"


@dataclass
class AdmissibilityDecision:
    contract: ExecutionAdmissibilityContract
    policy_version: str = ADMISSIBILITY_VERSION


class ExecutionAdmissibilityService:
    """Deterministic admissibility decision engine (zero LLM calls)."""

    def decide(
        self,
        *,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        solvability: SolvabilityContract,
        policy_overrides: Optional[dict] = None,
    ) -> AdmissibilityDecision:
        # Policy overrides are reserved for future use. Phase 3 ignores them
        # to keep behaviour deterministic and testable.
        _ = policy_overrides

        problem_blocked = bool(problem_formulation.blocking_axes)
        input_blocked = bool(input_closure.blocking_axes)
        output_blocked = bool(output_closure.blocking_axes)
        any_blocking = problem_blocked or input_blocked or output_blocked

        unmatched_count = sum(
            1 for match in solvability.capability_matches if match.match_status == "unmatched"
        )
        partial_count = sum(
            1 for match in solvability.capability_matches if match.match_status == "partial"
        )
        human_review_count = sum(
            1 for subgoal in solvability.required_subgoals if subgoal.human_review_required
        )

        # Rule 1: solvability says not_solvable → unsupported
        if solvability.solvability_status == "not_solvable":
            return AdmissibilityDecision(
                contract=_build_contract(
                    status="unsupported",
                    regime="analysis_only",
                    automation_level="plan_only",
                    build_decision="reject",
                    reasons=_dedupe(
                        ["capability_gap_detected"]
                        + (["objective_under_specified"] if any_blocking else [])
                    ),
                    workflowability_level="none",
                    workflowability_primary="analysis_only",
                    workflowability_blockers=_collect_workflowability_blockers(
                        solvability=solvability,
                        problem_formulation=problem_formulation,
                        input_closure=input_closure,
                        output_closure=output_closure,
                    ),
                    artifactability_level="draft_only",
                    risk_class="high",
                    confidence=30,
                    capability_gaps=_capability_gap_names(solvability),
                    requires_human_review=True,
                    summary=(
                        "Request is not solvable with the current capability envelope."
                    ),
                    checkpoints=_build_human_checkpoints(
                        solvability=solvability,
                        problem_formulation=problem_formulation,
                        input_closure=input_closure,
                        output_closure=output_closure,
                    ),
                    blocking_assumptions=_collect_blocking_assumptions(
                        problem_formulation=problem_formulation,
                        input_closure=input_closure,
                        output_closure=output_closure,
                    ),
                ),
            )

        # Rule 2: any closure layer still blocking → review_needed, plan_only
        if any_blocking:
            return AdmissibilityDecision(
                contract=_build_contract(
                    status="review_needed",
                    regime="analysis_only",
                    automation_level="plan_only",
                    build_decision="defer_to_plan",
                    reasons=_dedupe(
                        [
                            "objective_under_specified" if problem_blocked else None,
                            "deliverable_not_clear" if output_blocked else None,
                            "environment_access_required" if input_blocked else None,
                        ]
                    ),
                    workflowability_level="plan_only",
                    workflowability_primary="analysis_only",
                    workflowability_blockers=_collect_workflowability_blockers(
                        solvability=solvability,
                        problem_formulation=problem_formulation,
                        input_closure=input_closure,
                        output_closure=output_closure,
                    ),
                    artifactability_level="partial",
                    risk_class="medium",
                    confidence=60,
                    capability_gaps=_capability_gap_names(solvability),
                    requires_human_review=True,
                    summary="Front-chain closure still open; deferring to plan-only output.",
                    checkpoints=_build_human_checkpoints(
                        solvability=solvability,
                        problem_formulation=problem_formulation,
                        input_closure=input_closure,
                        output_closure=output_closure,
                    ),
                    blocking_assumptions=_collect_blocking_assumptions(
                        problem_formulation=problem_formulation,
                        input_closure=input_closure,
                        output_closure=output_closure,
                    ),
                ),
            )

        # Rule 3: unmatched capability → unsupported
        if unmatched_count > 0:
            return AdmissibilityDecision(
                contract=_build_contract(
                    status="unsupported",
                    regime="analysis_only",
                    automation_level="plan_only",
                    build_decision="reject",
                    reasons=["capability_gap_detected"],
                    workflowability_level="none",
                    workflowability_primary="analysis_only",
                    workflowability_blockers=[
                        f"{unmatched_count} subgoal(s) have no matching capability"
                    ],
                    artifactability_level="partial",
                    risk_class="high",
                    confidence=50,
                    capability_gaps=_capability_gap_names(solvability),
                    requires_human_review=True,
                    summary=(
                        f"Cannot execute: {unmatched_count} capability gap(s) in the proof envelope."
                    ),
                    checkpoints=[],
                    blocking_assumptions=[],
                ),
            )

        # Rule 4: partial coverage OR human review required → human_gated
        if partial_count > 0 or human_review_count > 0:
            return AdmissibilityDecision(
                contract=_build_contract(
                    status="review_needed",
                    regime="human_gated",
                    automation_level="human_gated",
                    build_decision="admit_with_review",
                    reasons=["human_judgment_required"],
                    workflowability_level="partial_workflow",
                    workflowability_primary="human_gated",
                    workflowability_blockers=[
                        f"{partial_count} partial capability match(es)",
                    ]
                    if partial_count
                    else [],
                    artifactability_level="partial",
                    risk_class="medium",
                    confidence=75,
                    capability_gaps=[],
                    requires_human_review=True,
                    summary=(
                        "Solvable under a human-gated regime — partial coverage or "
                        "explicit review checkpoints required."
                    ),
                    checkpoints=_build_human_checkpoints(
                        solvability=solvability,
                        problem_formulation=problem_formulation,
                        input_closure=input_closure,
                        output_closure=output_closure,
                    ),
                    blocking_assumptions=[],
                ),
            )

        # Rule 5: fully admissible → full workflow
        return AdmissibilityDecision(
            contract=_build_contract(
                status="supported",
                regime="workflow_build",
                automation_level="full_workflow",
                build_decision="admit",
                reasons=[],
                workflowability_level="full_workflow",
                workflowability_primary="workflow_build",
                workflowability_blockers=[],
                artifactability_level="full",
                risk_class="low",
                confidence=95,
                capability_gaps=[],
                requires_human_review=False,
                summary=(
                    "Front-chain fully closed with complete capability coverage — "
                    "admissible for autonomous workflow execution."
                ),
                checkpoints=[],
                blocking_assumptions=[],
            ),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_contract(
    *,
    status: str,
    regime: str,
    automation_level: str,
    build_decision: str,
    reasons: list,
    workflowability_level: str,
    workflowability_primary: str,
    workflowability_blockers: list[str],
    artifactability_level: str,
    risk_class: str,
    confidence: int,
    capability_gaps: list[str],
    requires_human_review: bool,
    summary: str,
    checkpoints: list,
    blocking_assumptions: list[str],
) -> ExecutionAdmissibilityContract:
    return ExecutionAdmissibilityContract(
        admissibility_status=status,  # type: ignore[arg-type]
        admissibility_scope="current_system_level",
        build_admission_decision=build_decision,  # type: ignore[arg-type]
        primary_execution_regime=regime,  # type: ignore[arg-type]
        max_automation_level=automation_level,  # type: ignore[arg-type]
        expected_deliverable_class="report",
        confidence=confidence,
        requires_human_review=requires_human_review,
        reason_codes=[r for r in reasons if r],  # type: ignore[arg-type]
        summary=summary,
        artifactability=ArtifactabilityAssessment(
            level=artifactability_level,  # type: ignore[arg-type]
        ),
        workflowability=WorkflowabilityAssessment(
            level=workflowability_level,  # type: ignore[arg-type]
            primary_regime=workflowability_primary,  # type: ignore[arg-type]
            blockers=workflowability_blockers,
        ),
        required_human_checkpoints=checkpoints,
        risk_class=risk_class,  # type: ignore[arg-type]
        capability_gaps=capability_gaps,
        blocking_assumptions=blocking_assumptions,
        recommended_next_stage="compile_context_assembly"
        if status == "supported"
        else "clarification_loop",
    )


def _capability_gap_names(solvability: SolvabilityContract) -> list[str]:
    gaps: list[str] = []
    for match in solvability.capability_matches:
        if match.match_status in {"unmatched", "partial"}:
            for cls in match.required_capability_classes:
                marker = f"capability_class:{cls}"
                if marker not in gaps:
                    gaps.append(marker)
    return gaps


def _collect_workflowability_blockers(
    *,
    solvability: SolvabilityContract,
    problem_formulation: ProblemFormulationContract,
    input_closure: InputClosureContract,
    output_closure: OutputClosureContract,
) -> list[str]:
    blockers: list[str] = []
    if problem_formulation.blocking_axes:
        blockers.append(
            f"problem formulation blocking axes: {', '.join(problem_formulation.blocking_axes)}"
        )
    if input_closure.blocking_axes:
        blockers.append(
            f"input closure blocking axes: {', '.join(input_closure.blocking_axes)}"
        )
    if output_closure.blocking_axes:
        blockers.append(
            f"output closure blocking axes: {', '.join(output_closure.blocking_axes)}"
        )
    if solvability.unsatisfied_requirements:
        blockers.append(
            f"{len(solvability.unsatisfied_requirements)} unsatisfied solvability requirement(s)"
        )
    return blockers


def _collect_blocking_assumptions(
    *,
    problem_formulation: ProblemFormulationContract,
    input_closure: InputClosureContract,
    output_closure: OutputClosureContract,
) -> list[str]:
    axes: list[str] = []
    axes.extend(problem_formulation.blocking_axes)
    axes.extend(input_closure.blocking_axes)
    axes.extend(output_closure.blocking_axes)
    return _dedupe_strs(axes)


def _build_human_checkpoints(
    *,
    solvability: SolvabilityContract,
    problem_formulation: ProblemFormulationContract,
    input_closure: InputClosureContract,
    output_closure: OutputClosureContract,
) -> list[HumanCheckpoint]:
    checkpoints: list[HumanCheckpoint] = []
    for axis in problem_formulation.blocking_axes:
        checkpoints.append(
            HumanCheckpoint(
                checkpoint_id=f"clarify_problem_{axis}",
                reason=f"Problem formulation is blocked on axis {axis!r}.",
                related_axes=[axis],
            )
        )
    for axis in input_closure.blocking_axes:
        checkpoints.append(
            HumanCheckpoint(
                checkpoint_id=f"clarify_input_{axis}",
                reason=f"Input closure is blocked on axis {axis!r}.",
                related_axes=[axis],
            )
        )
    for axis in output_closure.blocking_axes:
        checkpoints.append(
            HumanCheckpoint(
                checkpoint_id=f"clarify_output_{axis}",
                reason=f"Output closure is blocked on axis {axis!r}.",
                related_axes=[axis],
            )
        )
    for subgoal in solvability.required_subgoals:
        if subgoal.human_review_required:
            checkpoints.append(
                HumanCheckpoint(
                    checkpoint_id=f"review_subgoal_{subgoal.subgoal_id}",
                    reason=f"Subgoal {subgoal.subgoal_id!r} requires explicit human review.",
                    related_axes=[subgoal.subgoal_id],
                )
            )
    return checkpoints


def _dedupe(items):
    seen = set()
    out = []
    for item in items:
        if item is None:
            continue
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _dedupe_strs(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out
