"""Capability mismatch validator — surfaces planner-binder vs spec
disagreements about what kind of work each node is doing.

Built in response to a real failure on the spare-parts scenario: the
LLM planner picked ``filesystem_read`` for nodes whose semantic_goal
was clearly compute work (analyze inventory risk, forecast demand,
generate decisions). A "binder fallback to coding_agent" would paper
over the symptom and silently make the planner's selection meaningless.
This validator instead **surfaces** the mismatch so the upstream layer
(planner re-run or human review) can fix it at the source.

Compatibility table — capability_id → set of allowed runtime classes:

    filesystem_read    → {bounded_acquire}
    filesystem_write   → {bounded_persist}
    validate_schema    → {bounded_verify}
    coding_agent       → {bounded_transform, bounded_agentic, bounded_acquire,
                          bounded_persist}   ← intentionally permissive: this is
                          the generalist; planner is allowed to use it for
                          any class. Mismatch is only when the OPPOSITE
                          direction holds (a specialised capability claims
                          a class outside its primitive purpose).
    human_review_gate  → not validated (skipped node, no spec).

Rationale:
    A specialised capability (filesystem_read, filesystem_write,
    validate_schema) MUST match its declared primitive class. If the
    spec says "this node is bounded_transform" and the planner picked
    filesystem_read, the planner is wrong — fs_read can only acquire,
    not transform. Block.

    coding_agent in the other direction (planner picked coding_agent
    when spec says bounded_acquire) is acceptable because it's a
    generalist; we're not penalising the planner for being conservative
    or for choosing the universal compute primitive.
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import Literal

from app.schemas.node_execution_spec import NodeExecutionSpec
from app.schemas.workflow_planner import WorkflowPlannerOutput

logger = logging.getLogger(__name__)


# Specialised capabilities — each ONLY supports the runtime classes listed.
# Anything not in this map is treated as "unknown specialisation" and the
# validator can't enforce — it logs a note but does not raise.
_SPECIALISED_RUNTIME_CLASSES: dict[str, set] = {
    "filesystem_read": {"bounded_acquire"},
    "filesystem_write": {"bounded_persist"},
    "validate_schema": {"bounded_verify"},
}

# Generalist capability — accepts any runtime class. Planner choosing
# this is never a structural mismatch.
_GENERALIST_CAPABILITY_IDS = {"coding_agent", "general_agentic_reasoning"}


class CapabilityMismatchIssue(BaseModel):
    """A single planner-vs-spec disagreement on a node's compute class."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    selected_capability_id: Optional[str]
    expected_runtime_class: str
    allowed_runtime_classes: list[str] = Field(default_factory=list)
    kind: Literal[
        "specialised_class_mismatch",  # specialised cap can't carry spec's class
        "missing_capability_selection",  # planner left selection empty
    ]
    detail: str


class CapabilityMismatchReport(BaseModel):
    """Aggregated validator result."""

    model_config = ConfigDict(extra="forbid")

    checked_nodes: int = 0
    skipped_nodes: int = 0
    issues: list[CapabilityMismatchIssue] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.issues


def validate_capability_mismatch(
    *,
    planner_output: WorkflowPlannerOutput,
    specs: list[NodeExecutionSpec],
    skipped_node_ids: list[str],
) -> CapabilityMismatchReport:
    """Walk every spec'd node, compare planner pick vs spec runtime class."""
    spec_by_id = {s.node_id: s for s in specs}
    skipped = set(skipped_node_ids)
    report = CapabilityMismatchReport()

    for node in planner_output.node_drafts:
        if node.draft_node_id in skipped:
            report.skipped_nodes += 1
            continue
        spec = spec_by_id.get(node.draft_node_id)
        if spec is None:
            # Edge contract validator already flagged spec_missing for any
            # node that has an inbound edge. Don't double-report here.
            report.skipped_nodes += 1
            continue
        report.checked_nodes += 1

        bound = node.planner_metadata.get("bound_capability") or {}
        selected = None
        if isinstance(bound, dict):
            selected = bound.get("selected_capability_id")
        if not selected and node.preferred_capability_ids:
            selected = node.preferred_capability_ids[0]

        expected_class = spec.execution_constraints.expected_runtime_class

        if not selected:
            report.issues.append(
                CapabilityMismatchIssue(
                    node_id=node.draft_node_id,
                    selected_capability_id=None,
                    expected_runtime_class=expected_class,
                    allowed_runtime_classes=[],
                    kind="missing_capability_selection",
                    detail=(
                        f"Node {node.draft_node_id} has spec "
                        f"(expected_runtime_class={expected_class}) but "
                        f"planner did not bind any capability."
                    ),
                )
            )
            continue

        if selected in _GENERALIST_CAPABILITY_IDS:
            # Generalist accepts any class — no mismatch possible.
            continue

        allowed = _SPECIALISED_RUNTIME_CLASSES.get(selected)
        if allowed is None:
            # Capability we don't have a primitive-class table for —
            # don't enforce. Log so we notice growth in capability set.
            logger.info(
                "capability_mismatch_validator.unknown_capability_class node=%s selected=%s expected=%s",
                node.draft_node_id,
                selected,
                expected_class,
            )
            continue

        if expected_class not in allowed:
            report.issues.append(
                CapabilityMismatchIssue(
                    node_id=node.draft_node_id,
                    selected_capability_id=selected,
                    expected_runtime_class=expected_class,
                    allowed_runtime_classes=sorted(allowed),
                    kind="specialised_class_mismatch",
                    detail=(
                        f"Node {node.draft_node_id} planner picked "
                        f"capability '{selected}' which only supports runtime "
                        f"classes {sorted(allowed)}, but spec says this node "
                        f"is '{expected_class}'. The planner needs to pick a "
                        f"capability that can carry '{expected_class}' work "
                        f"(e.g. coding_agent for compute, or a future "
                        f"specialised primitive)."
                    ),
                )
            )

    if report.issues:
        logger.info(
            "capability_mismatch_validator.issues_found count=%d checked=%d skipped=%d",
            len(report.issues),
            report.checked_nodes,
            report.skipped_nodes,
        )
    return report


__all__ = [
    "CapabilityMismatchIssue",
    "CapabilityMismatchReport",
    "validate_capability_mismatch",
]
