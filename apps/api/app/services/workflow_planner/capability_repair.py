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

from app.schemas.node_execution_spec import NodeExecutionSpec
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
        # Stamp a per-node audit trail so trace readers can see the
        # original pick + reason without reading the bundle's
        # repair_attempts list.
        node.planner_metadata["capability_repair_record"] = {
            "issue_kind": issue.kind,
            "original_capability": issue.selected_capability_id,
            "new_capability": new_cap,
            "expected_runtime_class": issue.expected_runtime_class,
            "repair_source": source,
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


__all__ = [
    "CapabilityRepairAttempt",
    "CapabilityRepairReport",
    "repair_capability_picks",
]
