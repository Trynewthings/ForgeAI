"""Planner capability repair — rebinds a node's selected capability
when the spec/capability validator reports a mismatch.

This is the auto-repair half of Task 7. The validator surfaces three
mismatch kinds:

    * ``specialised_class_mismatch`` — planner picked a specialist
      whose primitive-class doesn't match the spec's runtime_class
      (e.g. ``filesystem_read`` for a ``bounded_transform`` node).
    * ``missing_capability_selection`` — planner left ``bound_capability``
      empty.
    * ``io_shape_mismatch`` — planner picked a single-file primitive
      (``filesystem_read`` / ``filesystem_write``) but the IC locator
      is a directory; single-file primitives can't iterate.

The repair is *deliberately deterministic* for the current slice. The
runnable_pool is small (4 capabilities) and each spec.runtime_class
maps to at most one specialised + one generalist option. An LLM
"please pick again" round trip would just rediscover the same answer
the table below already encodes — at the cost of latency and another
non-determinism source. When the slice grows we can add an LLM-
augmented strategy as a sibling function; the trace already records
``repair_source`` so callers can branch on it.

Repair priority for any spec.runtime_class:
    1. Specialised cap whose ``_SPECIALISED_RUNTIME_CLASSES`` entry
       contains the expected class AND that's not the offending pick
       AND (for io_shape_mismatch) doesn't share the same I/O-shape
       constraint that triggered the mismatch.
    2. Generalist (``coding_agent``) — universally accepts any class.

Mutates ``planner_output.node_drafts`` in place: the affected node's
``planner_metadata.bound_capability.selected_capability_id`` and
``preferred_capability_ids`` are rewritten, and ``selection_source``
is set to ``capability_repair`` so reviewers can trace the change.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from app.schemas.node_execution_spec import BusinessRule, NodeExecutionSpec
from app.schemas.workflow_planner import WorkflowPlannerOutput
from app.services.node_spec.capability_mismatch_validator import (
    _GENERALIST_CAPABILITY_IDS,
    _SINGLE_FILE_ONLY_CAPABILITY_IDS,
    _SPECIALISED_RUNTIME_CLASSES,
    CapabilityMismatchIssue,
)

logger = logging.getLogger(__name__)


# Always available as a fallback. Other generalists may exist in the
# runnable pool but coding_agent is the canonical choice for our slice.
_DEFAULT_GENERALIST = "coding_agent"

# Capabilities whose runtime contract REQUIRES the node to materialize
# its primary_output as a file on disk at the locator the binding
# advertises (``expected_outputs[0].locator``). Downstream nodes read
# that file directly — an in-memory return is not visible.
_CAPS_THAT_MUST_WRITE_FILE: frozenset[str] = frozenset(
    {
        "coding_agent",
        "filesystem_write",
    }
)

# Capabilities whose runtime contract emits its primary_output as
# in-memory state (``structured_output.content`` / runtime upstream
# pass-through). The spec author for these typically writes a
# semantic_goal like "into memory" / "return X" and an
# implementation_sketch that doesn't reference disk persistence.
_CAPS_THAT_DONT_WRITE_FILE: frozenset[str] = frozenset(
    {
        "filesystem_read",
        "validate_schema",
        "human_review_gate",
    }
)

# Minimum iteration budget after rebind. The original spec for an
# in-memory cap defaults to ``max_iterations=1``, which gives the
# now-rebound coding_agent zero chance to learn from a
# policy_violation feedback in iteration 1. We bump to 2 so the
# agent at least gets a structured second attempt.
_REBIND_MIN_ITERATIONS: int = 2


def _capability_emits_file(cap_id: Optional[str]) -> bool:
    """True iff this capability's runtime contract requires writing
    the primary_output to ``expected_outputs[0].locator`` on disk."""
    if not cap_id:
        return False
    return cap_id in _CAPS_THAT_MUST_WRITE_FILE


@dataclass
class SpecReconciliation:
    """Records that a spec was adjusted to match the new capability's
    runtime contract after a rebind.

    Reconciliation only fires when the rebind crosses the
    "in-memory cap → file-writing cap" boundary — otherwise the spec
    is left untouched.
    """

    node_id: str
    old_capability: Optional[str]
    new_capability: str
    added_business_rule_id: Optional[str] = None
    added_business_rule_text: Optional[str] = None
    added_implementation_sketch_line: Optional[str] = None
    bumped_max_iterations_from: Optional[int] = None
    bumped_max_iterations_to: Optional[int] = None
    rationale: str = ""


@dataclass
class CapabilityRepairAttempt:
    """Per-node record of a single rebind action."""

    node_id: str
    issue_kind: str
    expected_runtime_class: str
    original_capability: Optional[str]
    new_capability: Optional[str]
    repair_source: str  # "deterministic_specialist" | "deterministic_generalist" | "no_repair_possible"
    rationale: str
    spec_reconciliation: Optional[SpecReconciliation] = None


@dataclass
class CapabilityRepairReport:
    attempts: list[CapabilityRepairAttempt] = field(default_factory=list)
    unrepaired_node_ids: list[str] = field(default_factory=list)

    @property
    def made_any_change(self) -> bool:
        return any(
            a.new_capability and a.new_capability != a.original_capability
            for a in self.attempts
        )

    @property
    def spec_reconciliations(self) -> list[SpecReconciliation]:
        return [
            a.spec_reconciliation
            for a in self.attempts
            if a.spec_reconciliation is not None
        ]


def repair_capability_picks(
    *,
    planner_output: WorkflowPlannerOutput,
    specs: list[NodeExecutionSpec],
    issues: list[CapabilityMismatchIssue],
) -> CapabilityRepairReport:
    """Rebind each issue's offending node. Mutates ``planner_output``."""
    spec_by_id = {s.node_id: s for s in specs}
    nodes_by_id = {n.draft_node_id: n for n in planner_output.node_drafts}
    report = CapabilityRepairReport()

    for issue in issues:
        node = nodes_by_id.get(issue.node_id)
        spec = spec_by_id.get(issue.node_id)
        if node is None or spec is None:
            report.unrepaired_node_ids.append(issue.node_id)
            report.attempts.append(
                CapabilityRepairAttempt(
                    node_id=issue.node_id,
                    issue_kind=issue.kind,
                    expected_runtime_class=issue.expected_runtime_class,
                    original_capability=issue.selected_capability_id,
                    new_capability=None,
                    repair_source="no_repair_possible",
                    rationale=(
                        "node or spec not found in planner_output / spec bundle"
                    ),
                )
            )
            continue

        bound = dict(node.planner_metadata.get("bound_capability") or {})
        candidate_ids = list(bound.get("candidate_ids") or [])
        # Make sure the generalist is reachable as a fallback. Adding
        # it to the candidate pool isn't a planner override — it's
        # surfacing what's always slice-runnable.
        if _DEFAULT_GENERALIST not in candidate_ids:
            candidate_ids.append(_DEFAULT_GENERALIST)

        new_cap, source = _pick_compatible_capability(
            issue=issue,
            expected_class=spec.execution_constraints.expected_runtime_class,
            candidates=candidate_ids,
        )
        if new_cap is None:
            report.unrepaired_node_ids.append(issue.node_id)
            report.attempts.append(
                CapabilityRepairAttempt(
                    node_id=issue.node_id,
                    issue_kind=issue.kind,
                    expected_runtime_class=issue.expected_runtime_class,
                    original_capability=issue.selected_capability_id,
                    new_capability=None,
                    repair_source="no_repair_possible",
                    rationale=(
                        "no candidate in pool matches the expected runtime "
                        f"class {issue.expected_runtime_class!r} (pool="
                        f"{candidate_ids})"
                    ),
                )
            )
            continue

        # Rewrite the node's binding in place.
        bound["selected_capability_id"] = new_cap
        bound["candidate_ids"] = candidate_ids
        bound["selection_source"] = "capability_repair"
        node.planner_metadata = dict(node.planner_metadata or {})
        node.planner_metadata["bound_capability"] = bound

        # Reconcile the spec to match the new capability's runtime
        # contract. This is the second half of repair: the binding now
        # points at coding_agent (or another file-writer), but if the
        # spec was authored for an in-memory cap (filesystem_read /
        # validate_schema), its semantic_goal / implementation_sketch
        # / max_iterations still encode the wrong runtime mental
        # model. Without reconciliation the agent reads "into memory"
        # and silently skips the file write, then the runtime fails
        # with policy_violation and no second iteration to recover.
        reconciliation = _reconcile_spec_after_rebind(
            spec=spec,
            old_capability=issue.selected_capability_id,
            new_capability=new_cap,
        )
        # Stamp a per-node audit trail so trace readers can see the
        # original pick + reason without reading the bundle's
        # repair_attempts list.
        node.planner_metadata["capability_repair_record"] = {
            "issue_kind": issue.kind,
            "original_capability": issue.selected_capability_id,
            "new_capability": new_cap,
            "expected_runtime_class": issue.expected_runtime_class,
            "repair_source": source,
            "spec_reconciled": reconciliation is not None,
        }
        node.preferred_capability_ids = [new_cap]

        report.attempts.append(
            CapabilityRepairAttempt(
                node_id=issue.node_id,
                issue_kind=issue.kind,
                expected_runtime_class=issue.expected_runtime_class,
                original_capability=issue.selected_capability_id,
                new_capability=new_cap,
                repair_source=source,
                rationale=(
                    f"rebound from {issue.selected_capability_id!r} to "
                    f"{new_cap!r}: {source}"
                ),
                spec_reconciliation=reconciliation,
            )
        )

    if report.attempts:
        logger.info(
            "capability_repair.attempts count=%d unrepaired=%d",
            len(report.attempts),
            len(report.unrepaired_node_ids),
        )
    return report


def _pick_compatible_capability(
    *,
    issue: CapabilityMismatchIssue,
    expected_class: str,
    candidates: list[str],
) -> tuple[Optional[str], str]:
    """Return ``(new_capability_id, repair_source)``.

    Logic:
      1. Prefer a specialised capability whose primitive-class matches
         expected_class AND is not itself constrained out by the
         issue (e.g. avoid filesystem_read when the issue is
         io_shape_mismatch on a directory locator).
      2. Fall back to the generalist (coding_agent).
    """
    current = issue.selected_capability_id
    forbidden_for_io_shape: set = (
        _SINGLE_FILE_ONLY_CAPABILITY_IDS
        if issue.kind == "io_shape_mismatch"
        else set()
    )

    # Pass 1: specialised cap that matches.
    for cap in candidates:
        if cap == current:
            continue
        if cap in forbidden_for_io_shape:
            continue
        allowed = _SPECIALISED_RUNTIME_CLASSES.get(cap)
        if allowed and expected_class in allowed:
            return cap, "deterministic_specialist"

    # Pass 2: generalist fallback.
    for cap in candidates:
        if cap == current:
            continue
        if cap in _GENERALIST_CAPABILITY_IDS:
            return cap, "deterministic_generalist"

    return None, "no_repair_possible"


def _reconcile_spec_after_rebind(
    *,
    spec: NodeExecutionSpec,
    old_capability: Optional[str],
    new_capability: str,
) -> Optional[SpecReconciliation]:
    """If the rebind crosses 'in-memory cap → file-writing cap', mutate
    the spec so its prompt material (business_rules,
    implementation_sketch) and execution_constraints (max_iterations)
    no longer encode the old in-memory mental model.

    Specifically when:
      - old cap did NOT require disk persistence (filesystem_read,
        validate_schema, etc.), AND
      - new cap DOES require disk persistence (coding_agent,
        filesystem_write).

    we:
      1. Append a ``[persistence]`` BusinessRule that surfaces in the
         task_prompt's "Business rules (mandatory):" section. Agents
         honor this section more reliably than implementation_sketch
         (which is advisory) or semantic_goal (which the LLM may have
         written as 'into memory' / 'return as primary_output' under
         the old cap's mental model).
      2. Append a sketch line that reinforces the file-write step.
      3. Bump max_iterations to at least 2 so the agent has a chance
         to learn from a structured policy_violation feedback.

    Mutates ``spec`` in place. Returns a ``SpecReconciliation``
    describing what changed (for audit), or ``None`` if no change
    was needed.
    """
    new_emits_file = _capability_emits_file(new_capability)
    old_emits_file = _capability_emits_file(old_capability)

    if not new_emits_file:
        return None
    if old_emits_file:
        # Both old and new write files — locator_hint already
        # represents disk persistence. Nothing to reconcile.
        return None

    rule_id = f"capability_repair_persistence_{spec.node_id}"
    rule_text = (
        "You MUST persist primary_output to disk at the locator declared "
        "in output_contract.primary_output.locator_hint (also surfaced as "
        "`write to: ...` in this prompt and as expected_outputs[0].locator "
        "in the runtime contract). In-memory return is INSUFFICIENT — "
        "the runtime checks file existence after your script exits. "
        "Serialize tabular records as CSV with the exact schema_hint "
        "columns; serialize dataset_collection / structured outputs as "
        "JSON. Do NOT print to stdout in lieu of writing the file."
    )
    spec.business_rules.append(
        BusinessRule(
            rule_id=rule_id,
            rule=rule_text,
            category="persistence",
        )
    )

    sketch_line = (
        "After computing primary_output, write it to "
        "`output_contract.primary_output.locator_hint` "
        "(serialize tabular as CSV / dataset_collection as JSON)."
    )
    spec.implementation_sketch = list(spec.implementation_sketch or [])
    spec.implementation_sketch.append(sketch_line)

    bumped_from: Optional[int] = None
    bumped_to: Optional[int] = None
    if spec.execution_constraints.max_iterations < _REBIND_MIN_ITERATIONS:
        bumped_from = spec.execution_constraints.max_iterations
        spec.execution_constraints.max_iterations = _REBIND_MIN_ITERATIONS
        bumped_to = _REBIND_MIN_ITERATIONS

    return SpecReconciliation(
        node_id=spec.node_id,
        old_capability=old_capability,
        new_capability=new_capability,
        added_business_rule_id=rule_id,
        added_business_rule_text=rule_text,
        added_implementation_sketch_line=sketch_line,
        bumped_max_iterations_from=bumped_from,
        bumped_max_iterations_to=bumped_to,
        rationale=(
            f"rebind from {old_capability!r} (in-memory contract) to "
            f"{new_capability!r} (file-write contract) — appended "
            "mandatory persistence rule + sketch hint, bumped "
            f"max_iterations to {_REBIND_MIN_ITERATIONS}."
        ),
    )


__all__ = [
    "CapabilityRepairAttempt",
    "CapabilityRepairReport",
    "SpecReconciliation",
    "repair_capability_picks",
]
