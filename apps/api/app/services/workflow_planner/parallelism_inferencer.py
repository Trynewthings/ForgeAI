"""Phase C — parallelism_inferencer.

Authoritative source for ``semantic_role="parallel_branch"`` tagging and
``planner_notes=["fan_out_root"]`` on the root node.

Algorithm (evidence-first):
    * Two or more nodes that share the same single upstream AND have
      non-overlapping output evidence AND either have no downstream or
      share a common join downstream → they form a fan-out group. Mark
      each root-to-branch edge with semantic_role="parallel_branch" and
      stamp the root with planner_notes=["fan_out_root"].

Shape-LLM may provide ``parallel_candidates`` as hints. When evidence
supports a hint the grouping stands; when evidence rejects a hint we log
``planner_notes=["parallel_hint_rejected:<reason>"]`` on the proposed
root but do not change the grouping.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.schemas.solvability import SolvabilitySubgoal
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
)


@dataclass
class ParallelismReport:
    fan_out_groups: list[dict] = field(default_factory=list)
    rejected_hint_groups: list[dict] = field(default_factory=list)


class ParallelismInferencer:
    """Deterministic parallelism detector."""

    def infer(
        self,
        *,
        node_drafts: list[WorkflowPlannerNodeDraft],
        edge_drafts: list[WorkflowPlannerEdgeDraft],
        subgoals: list[SolvabilitySubgoal],
        subgoal_id_to_node_id: dict[str, str],
        parallel_candidates: Optional[Iterable[list[str]]] = None,
    ) -> ParallelismReport:
        report = ParallelismReport()

        node_by_id = {n.draft_node_id: n for n in node_drafts}
        subgoal_by_node_id = {
            subgoal_id_to_node_id.get(sg.subgoal_id): sg for sg in subgoals
        }
        upstream_map: dict[str, list[str]] = {n.draft_node_id: [] for n in node_drafts}
        downstream_map: dict[str, list[str]] = {n.draft_node_id: [] for n in node_drafts}
        for edge in edge_drafts:
            upstream_map.setdefault(edge.to_node_id, []).append(edge.from_node_id)
            downstream_map.setdefault(edge.from_node_id, []).append(edge.to_node_id)

        # Evidence-driven detection
        grouped: set[tuple[str, frozenset[str]]] = set()
        for node in node_drafts:
            ups = upstream_map.get(node.draft_node_id, [])
            if len(ups) != 1:
                continue
        # Group siblings by (shared_upstream, downstream_profile).
        siblings_by_root: dict[str, list[str]] = {}
        for node in node_drafts:
            ups = upstream_map.get(node.draft_node_id, [])
            if len(ups) != 1:
                continue
            siblings_by_root.setdefault(ups[0], []).append(node.draft_node_id)

        for root_id, siblings in siblings_by_root.items():
            if len(siblings) < 2:
                continue
            if not _evidence_non_overlap(
                siblings=siblings,
                subgoal_by_node_id=subgoal_by_node_id,
            ):
                continue
            # Siblings are parallel when their downstream subtrees don't
            # intersect (independent branches all the way to their exits)
            # OR they share a join downstream. The legacy rule "shared or
            # empty downstream" was too strict — it missed the common
            # "three independent terminal writes" pattern (S1 write-heavy
            # scenario) where each branch has its own gate/verify tail.
            if not _siblings_run_independently(
                siblings=siblings, downstream_map=downstream_map
            ):
                continue

            key = (root_id, frozenset(siblings))
            if key in grouped:
                continue
            grouped.add(key)

            # Tag edges root→sibling with parallel_branch.
            for edge in edge_drafts:
                if edge.from_node_id == root_id and edge.to_node_id in siblings:
                    edge.semantic_role = "parallel_branch"
                    edge.planner_metadata = dict(edge.planner_metadata or {})
                    edge.planner_metadata["edge_source"] = "parallel_branch"
            # Tag the root node.
            root_node = node_by_id.get(root_id)
            if root_node and "fan_out_root" not in root_node.planner_notes:
                root_node.planner_notes.append("fan_out_root")
            report.fan_out_groups.append(
                {"root": root_id, "branches": sorted(siblings)}
            )

        # Shape-LLM hint reconciliation.
        for hint_group in parallel_candidates or []:
            mapped = [subgoal_id_to_node_id.get(sg_id) for sg_id in hint_group]
            mapped = [m for m in mapped if m]
            if len(mapped) < 2:
                continue
            if any(
                set(grp.get("branches", [])) >= set(mapped)
                for grp in report.fan_out_groups
            ):
                continue  # already detected
            report.rejected_hint_groups.append(
                {
                    "hint": list(hint_group),
                    "reason": "evidence_does_not_support",
                }
            )
            for nid in mapped:
                node = node_by_id.get(nid)
                if node and "parallel_hint_rejected" not in ",".join(node.planner_notes):
                    node.planner_notes.append(
                        "parallel_hint_rejected:evidence_does_not_support"
                    )

        return report


def _evidence_non_overlap(
    *,
    siblings: list[str],
    subgoal_by_node_id: dict[Optional[str], SolvabilitySubgoal],
) -> bool:
    """Return True when no two siblings produce the same evidence kind.
    Empty output_evidence is treated as 'unknown' → non-overlapping by default.
    """
    seen_kinds: set[str] = set()
    for nid in siblings:
        sg = subgoal_by_node_id.get(nid)
        if sg is None:
            continue
        for kind in sg.output_evidence_kinds or []:
            if kind in seen_kinds:
                return False
            seen_kinds.add(kind)
    return True


def _shared_or_empty_downstream(
    *, siblings: list[str], downstream_map: dict[str, list[str]]
) -> bool:
    """Legacy rule: siblings share a join OR all are terminal.

    Kept for reference / potential debugging; the authoritative check is
    ``_siblings_run_independently`` which supersedes it.
    """
    downstreams = [set(downstream_map.get(nid, [])) for nid in siblings]
    if not any(downstreams):
        return True
    non_empty = [d for d in downstreams if d]
    if not non_empty:
        return True
    shared = set.intersection(*non_empty)
    return bool(shared)


def _siblings_run_independently(
    *, siblings: list[str], downstream_map: dict[str, list[str]]
) -> bool:
    """Return True when the siblings' downstream subtrees either all
    converge at a shared join OR run as fully disjoint subtrees.

    Two siblings whose subtrees never intersect are safe to execute in
    parallel; the scheduler only needs to wait on each subtree's exit
    independently. The legacy rule rejected this case by requiring a
    shared join, which mis-classified independent-write scenarios (e.g.
    "write CSV" + "send email" + "write markdown" from the same
    upstream) as sequential.

    Rule:
        1. All siblings terminal → parallel (no further work).
        2. Compute transitive descendants for each sibling. If every
           pair of descendant sets is disjoint → parallel.
        3. If all non-empty descendant sets share at least one node
           (a join point) → parallel.
        4. Otherwise (partial overlap without full convergence) →
           reject; mixing subtrees makes scheduling fragile.
    """
    if not siblings:
        return False
    if len(siblings) < 2:
        return False

    descendants: list[set[str]] = []
    for nid in siblings:
        seen: set[str] = set()
        stack = list(downstream_map.get(nid, []))
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            stack.extend(downstream_map.get(node, []))
        descendants.append(seen)

    if not any(descendants):
        return True  # All terminal.

    # Pairwise disjoint?
    disjoint = True
    for i in range(len(descendants)):
        for j in range(i + 1, len(descendants)):
            if descendants[i] & descendants[j]:
                disjoint = False
                break
        if not disjoint:
            break
    if disjoint:
        return True

    # Fallback to legacy "shared join" check: all non-empty descendant
    # sets must share at least one node.
    non_empty = [d for d in descendants if d]
    if not non_empty:
        return True
    shared = set.intersection(*non_empty)
    return bool(shared)


__all__ = ["ParallelismInferencer", "ParallelismReport"]
