"""Task 7.6D — complexity-aware max_iterations policy.

The default ``max_iterations=1`` from the LLM-authored spec is too
tight for complex transform nodes; raising it blanket-style for
everything wastes tokens on trivial cases. This module derives a
node-shape-aware budget and stamps it onto the spec, recording the
rationale on the spec's failure_policy / planner_metadata audit
trail so reviewers can see WHY a particular budget was chosen.

Heuristic ladder (first match wins):

    semantic_role = "verify"                       → 1
    semantic_role = "persist"                      → 2
    semantic_role = "acquire"                      → 2  (3 if
        the spec's input_contract is empty — typical "enumerate
        the input directory" pattern)
    semantic_role in {transform, analyze, decide,  → 3
        forecast, summarize, act}
    + multi-input dataset bundle (input slot       → 4
        kind=dataset_collection AND >= 4 members)
    + final node with required_columns >= 10        → 5

Capped at 5 to keep token spend bounded.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from app.schemas.node_execution_spec import NodeExecutionSpec
from app.schemas.problem_formulation import OutputClosureContract
from app.schemas.workflow_planner import WorkflowPlannerOutput
from app.services.node_spec.dataset_key_normalizer import is_dataset_bundle_kind

logger = logging.getLogger(__name__)

# Hard ceiling so a misclassification can't blow the token budget.
_MAX_ITERATION_CEILING: int = 5


@dataclass
class IterationBudgetDecision:
    """Audit record describing one node's max_iterations decision."""

    node_id: str
    semantic_role: Optional[str]
    chosen_iterations: int
    previous_iterations: int
    reason: str


def _terminal_node_required_columns_count(
    *, spec: NodeExecutionSpec, output_closure: Optional[OutputClosureContract]
) -> int:
    """If this spec writes the user's final deliverable AND the
    user explicitly listed required_columns, return that count.
    Otherwise 0 (= signal "not the terminal node, ignore this rule")."""
    if output_closure is None:
        return 0
    required = list(getattr(output_closure, "required_columns", []) or [])
    if not required:
        return 0
    primary = spec.output_contract.primary_output
    delivery_target = getattr(output_closure, "delivery_target", None)
    if not primary or not primary.locator_hint or not delivery_target:
        return 0
    if primary.locator_hint != delivery_target:
        return 0
    return len(required)


def _is_multi_input_bundle_consumer(spec: NodeExecutionSpec) -> bool:
    """True iff this spec consumes a dataset bundle (kind in
    DATASET_BUNDLE_KINDS) with >= 4 members. The classic
    "join 6 CSVs" transform pattern."""
    for slot in spec.input_contract.required:
        if not is_dataset_bundle_kind(slot.kind):
            continue
        if len(slot.schema_hint or []) >= 4:
            return True
    return False


def derive_max_iterations(
    *,
    spec: NodeExecutionSpec,
    semantic_role: Optional[str],
    output_closure: Optional[OutputClosureContract],
) -> tuple[int, str]:
    """Return ``(max_iterations, reason)`` for the given spec.

    Pure function — does NOT mutate the spec. Caller decides
    whether to apply the value.
    """
    role = (semantic_role or "").strip().lower()

    if role == "verify":
        return 1, "verify_node_default"
    if role == "human_gate":
        return 1, "human_gate_default"
    if role == "acquire":
        # Acquire often enumerates the input dir + reads files —
        # one extra turn helps when the first attempt mis-globs.
        if not spec.input_contract.required:
            return 3, "acquire_node_with_no_explicit_inputs"
        # 7.6G+ — bumped from 2 → 3. Acquire nodes typically spend
        # iter 1 on a directory enumeration / existence check (the
        # exploration_only detector accepts this for acquire nodes
        # since it's a legitimate first move). That leaves only iter 2
        # for the real read-and-write pass; if the LLM picks the
        # wrong serializer or hits an import quirk on iter 2, there's
        # no third try. Three iterations gives one repair window.
        return 3, "acquire_node_default"
    # ``persist`` is a synonym we accept from spec layers that use
    # the longer name; the planner enum itself only spells write
    # nodes as ``act``. Treat both identically — final writes
    # rarely need more than one retry.
    if role in {"act", "persist"}:
        return 2, "act_or_persist_default"

    # Compute / decision family. Floor at 3, escalate based on shape.
    decision_classes = {
        "transform",
        "analyze",
        "decide",
        "forecast",
        "summarize",
    }
    if role not in decision_classes:
        # Unknown / control / composite — be conservative.
        return 3, f"unknown_or_control_role_{role!r}_defaulting_to_3"

    required_count = _terminal_node_required_columns_count(
        spec=spec, output_closure=output_closure
    )
    multi_input = _is_multi_input_bundle_consumer(spec=spec)

    # Final terminal node with rich contract — give the most budget.
    if required_count >= 10:
        chosen = min(_MAX_ITERATION_CEILING, 5)
        return (
            chosen,
            f"terminal_node_with_required_columns_{required_count}",
        )

    if multi_input:
        return min(_MAX_ITERATION_CEILING, 4), "multi_input_dataset_bundle_transform"

    return 3, f"{role}_node_default"


def apply_iteration_budget_policy(
    *,
    specs: list[NodeExecutionSpec],
    planner_output: WorkflowPlannerOutput,
    output_closure: Optional[OutputClosureContract],
) -> list[IterationBudgetDecision]:
    """Stamp a complexity-aware ``max_iterations`` onto each spec.

    Mutates each spec's ``execution_constraints.max_iterations``
    in place. Returns audit records (one per spec touched).

    The policy NEVER lowers an LLM-authored value — only raises
    when the ladder demands more. This means a spec that already
    asked for 5 iterations stays at 5; a spec stuck at 1 gets
    bumped per the ladder.
    """
    role_by_node_id: dict[str, str] = {
        n.draft_node_id: (n.node_semantic_type or "")
        for n in planner_output.node_drafts
    }
    audit: list[IterationBudgetDecision] = []
    for spec in specs:
        role = role_by_node_id.get(spec.node_id) or ""
        previous = spec.execution_constraints.max_iterations
        chosen, reason = derive_max_iterations(
            spec=spec,
            semantic_role=role,
            output_closure=output_closure,
        )
        if chosen <= previous:
            # LLM already gave at least the policy minimum —
            # don't lower.
            continue
        spec.execution_constraints.max_iterations = chosen
        audit.append(
            IterationBudgetDecision(
                node_id=spec.node_id,
                semantic_role=role or None,
                chosen_iterations=chosen,
                previous_iterations=previous,
                reason=reason,
            )
        )
    if audit:
        logger.info(
            "iteration_budget_policy.applied count=%d decisions=%s",
            len(audit),
            [
                f"{d.node_id}:{d.previous_iterations}->{d.chosen_iterations}({d.reason})"
                for d in audit
            ],
        )
    return audit


__all__ = [
    "IterationBudgetDecision",
    "apply_iteration_budget_policy",
    "derive_max_iterations",
]
