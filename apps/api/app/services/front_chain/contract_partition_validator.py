"""Deterministic contract partition validator (Phase 1A).

Consumes a ``FrontChainInterpreterLLMOutput`` envelope and produces the three
authoritative contracts (``ProblemFormulationContract``,
``InputClosureContract``, ``OutputClosureContract``).

Hard guardrails (R4 — layer boundaries):
    * Problem formulation must NOT carry closure semantics
      (no closure_status, no deliverable_form, no source_locator, no
      admissibility fields).
    * Input closure must NOT carry output or admissibility semantics
      (no deliverable_form, no admissibility_status, no runtime regime).
    * Output closure must NOT carry input/capability semantics
      (no source_locator, no acquisition_path_kind, no capability refs).

The validator treats candidate content identically regardless of origin — LLM
response, substrate-enriched prompt, or test stub. If a forbidden field is
present on the wrong layer, the validator records it as a
``PartitionValidationIssue`` and strips it (does not silently migrate).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.schemas.front_chain import (
    FrontChainInterpreterInputClosureCandidate,
    FrontChainInterpreterLLMOutput,
    FrontChainInterpreterOutputClosureCandidate,
    FrontChainInterpreterProblemCandidate,
)
from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    PlanningIntentContract,
    ProblemAssumption,
    ProblemDecision,
    ProblemFormulationContract,
)

VALIDATOR_VERSION = "front_chain_partition_validator_v1"


# ---------------------------------------------------------------------------
# Forbidden-field rules
# ---------------------------------------------------------------------------

# Fields that must NEVER appear on a problem formulation candidate because
# they belong to closure / admissibility / planner layers.
_FORBIDDEN_ON_PROBLEM_FORMULATION = frozenset(
    {
        "closure_status",
        "resolved_source_locator",
        "resolved_source_kind",
        "resolved_source_modality",
        "deliverable_form",
        "delivery_target",
        "output_modality",
        "acquisition_path_kind",
        "admissibility_status",
        "primary_execution_regime",
        "max_automation_level",
        "candidate_sources",
        "capability_matches",
        "required_subgoals",
        "graph_feasibility",
        "selected_blueprint_type",
    }
)

# Fields that must NEVER appear on an input closure candidate because they
# belong to output / admissibility / capability layers.
_FORBIDDEN_ON_INPUT_CLOSURE = frozenset(
    {
        "deliverable_form",
        "delivery_target",
        "output_modality",
        "admissibility_status",
        "primary_execution_regime",
        "max_automation_level",
        "capability_matches",
        "required_subgoals",
        "graph_feasibility",
        "selected_blueprint_type",
        "problem_status",
        "current_system_fit",
    }
)

# Fields that must NEVER appear on an output closure candidate because they
# belong to input / capability / admissibility layers.
_FORBIDDEN_ON_OUTPUT_CLOSURE = frozenset(
    {
        "resolved_source_locator",
        "resolved_source_kind",
        "resolved_source_modality",
        "candidate_sources",
        "acquisition_path_kind",
        "capability_matches",
        "required_subgoals",
        "graph_feasibility",
        "admissibility_status",
        "primary_execution_regime",
        "selected_blueprint_type",
        "problem_status",
        "current_system_fit",
    }
)


@dataclass
class PartitionValidationIssue:
    layer: str
    axis: str
    kind: str  # "forbidden_field" | "duplicate_decision_axis" | ...
    detail: str


@dataclass
class PartitionValidationResult:
    problem_formulation: ProblemFormulationContract
    input_closure: InputClosureContract
    output_closure: OutputClosureContract
    issues: list[PartitionValidationIssue] = field(default_factory=list)
    validator_version: str = VALIDATOR_VERSION


class ContractPartitionValidator:
    """Deterministic validator that turns candidates into contracts.

    The validator:
        1. Strips forbidden fields from each candidate and records issues.
        2. Assigns pooled decisions/assumptions to the correct contract by
           axis name (if already referenced in a candidate's blocking_axes or
           review_axes it goes there; otherwise it stays in problem
           formulation as the default layer).
        3. Builds the final contracts with a deterministic
           ``planning_intent`` derived from the problem candidate.
    """

    def validate_and_partition(
        self, llm_output: FrontChainInterpreterLLMOutput
    ) -> PartitionValidationResult:
        issues: list[PartitionValidationIssue] = []

        problem_candidate = llm_output.problem_formulation_candidate
        input_candidate = llm_output.input_closure_candidate
        output_candidate = llm_output.output_closure_candidate

        # Strip forbidden fields (we work on model_dump copies to avoid
        # mutating the envelope).
        problem_payload = _strip_forbidden(
            problem_candidate.model_dump(exclude_none=True),
            _FORBIDDEN_ON_PROBLEM_FORMULATION,
            layer="problem_formulation",
            issues=issues,
        )
        input_payload = _strip_forbidden(
            input_candidate.model_dump(exclude_none=True),
            _FORBIDDEN_ON_INPUT_CLOSURE,
            layer="input_closure",
            issues=issues,
        )
        output_payload = _strip_forbidden(
            output_candidate.model_dump(exclude_none=True),
            _FORBIDDEN_ON_OUTPUT_CLOSURE,
            layer="output_closure",
            issues=issues,
        )

        # Route pooled decisions and assumptions by axis.
        (
            problem_decisions,
            input_decisions,
            output_decisions,
        ) = _route_decisions(
            pool=llm_output.decision_candidates,
            problem_blocking=problem_payload.get("blocking_axes", []) or [],
            problem_review=problem_payload.get("review_axes", []) or [],
            input_blocking=input_payload.get("blocking_axes", []) or [],
            input_review=input_payload.get("review_axes", []) or [],
            output_blocking=output_payload.get("blocking_axes", []) or [],
            output_review=output_payload.get("review_axes", []) or [],
            issues=issues,
        )

        assumption_pool = list(llm_output.assumption_candidates)

        # Build planning intent from the problem candidate.
        planning_intent = _build_planning_intent(problem_candidate)

        problem_contract = ProblemFormulationContract(
            interpreted_problem_kind=problem_payload.get("interpreted_problem_kind"),
            target_object=problem_payload.get("target_object"),
            problem_status=problem_payload.get("problem_status", "underdetermined"),
            current_system_fit=problem_payload.get("current_system_fit", "partially_in_scope"),
            planning_intent=planning_intent,
            required_decisions=problem_decisions,
            recommended_assumptions=assumption_pool,
            blocking_axes=list(problem_payload.get("blocking_axes", []) or []),
            review_axes=list(problem_payload.get("review_axes", []) or []),
            blocking_reasons=list(problem_payload.get("blocking_reasons", []) or []),
            clarification_plan=[d.question for d in problem_decisions],
            reasoning_summary=problem_payload.get("reasoning_summary"),
            # PF semantic-gate — passed through verbatim from the
            # interpreter candidate.
            intent_clarity=_normalise_intent_clarity(
                problem_payload.get("intent_clarity")
            ),
            interpreted_goal=_coerce_optional_str(
                problem_payload.get("interpreted_goal")
            ),
            clarification_question=_coerce_optional_str(
                problem_payload.get("clarification_question")
            ),
        )

        input_contract = InputClosureContract(
            target_object=input_payload.get("target_object") or problem_payload.get("target_object"),
            planning_intent_id=planning_intent.intent_id if planning_intent else None,
            candidate_sources=list(input_candidate.candidate_sources),
            resolved_source_kind=input_payload.get("resolved_source_kind"),
            resolved_source_locator=input_payload.get("resolved_source_locator"),
            resolved_source_modality=input_payload.get("resolved_source_modality"),
            acquisition_path_kind=input_payload.get("acquisition_path_kind", "unknown"),
            closure_requirements=list(input_candidate.closure_requirements),
            blocking_axes=list(input_payload.get("blocking_axes", []) or []),
            review_axes=list(input_payload.get("review_axes", []) or []),
            closure_status=input_payload.get("closure_status", "input_clarification_required"),
            reasoning_summary=input_payload.get("reasoning_summary"),
        )

        output_contract = OutputClosureContract(
            deliverable_form=output_payload.get("deliverable_form"),
            delivery_target=output_payload.get("delivery_target"),
            output_modality=output_payload.get("output_modality"),
            closure_requirements=list(output_candidate.closure_requirements),
            blocking_axes=list(output_payload.get("blocking_axes", []) or []),
            review_axes=list(output_payload.get("review_axes", []) or []),
            closure_status=output_payload.get("closure_status", "output_clarification_required"),
            reasoning_summary=output_payload.get("reasoning_summary"),
        )

        # Cross-layer derivation: if the interpreter already landed the
        # task in the synthesis / generation family on turn 1, collapse
        # input_closure to not_applicable up-front so the user is not asked
        # for a data source that doesn't exist. The rule is the same one
        # enforced by the update engine on later turns — kept in sync by
        # sharing ``is_synthesis_problem_kind`` from the axis registry.
        # Importing here (not at module top) avoids adding a service-layer
        # import cycle for the validator (module keeps its purely
        # deterministic footprint).
        from app.services.front_chain.closure_axis_registry import (
            is_synthesis_problem_kind,
        )
        from app.services.front_chain.contract_update_engine import (
            _collapse_input_to_not_applicable,
            _ensure_ic_completeness,
            _ensure_oc_completeness,
            _ensure_pf_completeness,
        )

        if is_synthesis_problem_kind(problem_contract.interpreted_problem_kind):
            input_contract = _collapse_input_to_not_applicable(input_contract)

        # Contract completeness guard on turn 1 — same semantics as in
        # contract_update_engine.apply_resolutions (see its comment). The
        # interpreter LLM occasionally emits a layer with
        # closure_status != closed AND empty blocking/review AND no
        # resolved field; re-inject the canonical required axis so the
        # policy engine always has something to surface.
        problem_contract = _ensure_pf_completeness(problem_contract)
        input_contract = _ensure_ic_completeness(input_contract)
        output_contract = _ensure_oc_completeness(output_contract)

        # Attach routed decisions directly to the input/output contracts via
        # the clarification list on the problem formulation (they're linked by
        # axis so the clarification policy engine can still emit per-layer
        # questions). We also return the ``input_decisions`` / ``output_decisions``
        # via the issues pipeline so Phase 1B can consume them if needed.
        if input_decisions:
            issues.append(
                PartitionValidationIssue(
                    layer="input_closure",
                    axis=",".join(d.axis for d in input_decisions),
                    kind="decisions_routed",
                    detail=f"Routed {len(input_decisions)} decisions to input_closure layer.",
                )
            )
        if output_decisions:
            issues.append(
                PartitionValidationIssue(
                    layer="output_closure",
                    axis=",".join(d.axis for d in output_decisions),
                    kind="decisions_routed",
                    detail=f"Routed {len(output_decisions)} decisions to output_closure layer.",
                )
            )

        return PartitionValidationResult(
            problem_formulation=problem_contract,
            input_closure=input_contract,
            output_closure=output_contract,
            issues=issues,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_intent_clarity(value: Any) -> str:
    """Coerce any LLM-emitted value into the three-valued clarity enum.

    The interpreter is instructed to return ``clear`` / ``ambiguous`` /
    ``missing_goal`` verbatim, but LLMs occasionally paraphrase (``"unclear"``,
    ``"partial"``, ``"complete"``, etc.). We accept a small synonym set and
    fall back to ``ambiguous`` for anything unknown — never ``clear``, so
    the PF gate is on the safe side.
    """
    if value is None:
        return "ambiguous"
    text = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if text in {"clear", "fully_clear", "complete", "goal_clear"}:
        return "clear"
    if text in {"missing_goal", "no_goal", "goal_missing", "objective_missing"}:
        return "missing_goal"
    if text in {"ambiguous", "partial", "unclear", "underdetermined"}:
        return "ambiguous"
    return "ambiguous"


def _coerce_optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _strip_forbidden(
    payload: dict[str, Any],
    forbidden: Iterable[str],
    *,
    layer: str,
    issues: list[PartitionValidationIssue],
) -> dict[str, Any]:
    forbidden_set = set(forbidden)
    cleaned: dict[str, Any] = {}
    for key, value in payload.items():
        if key in forbidden_set:
            issues.append(
                PartitionValidationIssue(
                    layer=layer,
                    axis=key,
                    kind="forbidden_field",
                    detail=(
                        f"Field {key!r} is not allowed on layer {layer!r}; stripped to enforce layer boundaries."
                    ),
                )
            )
            continue
        cleaned[key] = value
    return cleaned


def _route_decisions(
    *,
    pool: list[ProblemDecision],
    problem_blocking: list[str],
    problem_review: list[str],
    input_blocking: list[str],
    input_review: list[str],
    output_blocking: list[str],
    output_review: list[str],
    issues: list[PartitionValidationIssue],
) -> tuple[list[ProblemDecision], list[ProblemDecision], list[ProblemDecision]]:
    """Deterministically assign each pooled decision to exactly one layer."""
    problem_axes = set(problem_blocking) | set(problem_review)
    input_axes = set(input_blocking) | set(input_review)
    output_axes = set(output_blocking) | set(output_review)

    problem_list: list[ProblemDecision] = []
    input_list: list[ProblemDecision] = []
    output_list: list[ProblemDecision] = []
    seen_axes: set[str] = set()

    for decision in pool:
        if decision.axis in seen_axes:
            issues.append(
                PartitionValidationIssue(
                    layer="problem_formulation",
                    axis=decision.axis,
                    kind="duplicate_decision_axis",
                    detail=f"Duplicate decision for axis {decision.axis!r}; ignored.",
                )
            )
            continue
        seen_axes.add(decision.axis)

        # Priority: input > output > problem (closure layers are concrete
        # authority; formulation is the fallback layer for unlabelled axes).
        if decision.axis in input_axes:
            input_list.append(decision)
        elif decision.axis in output_axes:
            output_list.append(decision)
        elif decision.axis in problem_axes:
            problem_list.append(decision)
        else:
            # Unlabelled axis: default to problem formulation.
            problem_list.append(decision)
    return problem_list, input_list, output_list


def _build_planning_intent(
    problem_candidate: FrontChainInterpreterProblemCandidate,
) -> PlanningIntentContract | None:
    if not problem_candidate.planning_intent_label:
        return None
    target_object = problem_candidate.target_object
    objective_family = problem_candidate.planning_intent_objective_family or "generic"
    intent_id = problem_candidate.interpreted_problem_kind or "front_chain_intent"
    return PlanningIntentContract(
        intent_id=intent_id,
        label=problem_candidate.planning_intent_label,
        target_object=target_object,
        objective_family=objective_family,
        preferred_graph_shape="unknown",
        parallelism_hint="unknown",
        acquisition_strategy="unknown",
        evidence_strategy="generic",
        side_effect_budget="unknown",
        reasoning_summary=problem_candidate.reasoning_summary,
    )
