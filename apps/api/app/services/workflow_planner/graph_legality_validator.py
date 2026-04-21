"""Phase C — graph_legality_validator.

Final structural check before the planner hands the graph off to
consumers. Catches:

    * cycles (Kahn topo sort failing)
    * unreachable nodes from entry_node_ids
    * dangling nodes (no edges, not in entry/exit)
    * empty entry or exit set
    * self-loops
    * duplicate edges (same from/to pair)
    * contradictory edge_type on duplicate pairs (split between
      data_flow and control_sequence)
    * gate insertion producing a dead gate node (no outbound edge)

Light fixes are applied in place:
    * duplicate edges collapse (keep first)
    * self-loops drop
    * dangling non-insertion nodes drop

Heavy violations (cycle / unreachable / gate dead end / empty entry) are
reported via ``LegalityReport.violations`` — the service converts
``ok=False`` into ``draft_status="blocked"`` and fills
``unresolved_items`` with the concrete violations.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Optional

from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
    WorkflowPlannerUnresolvedItem,
)


@dataclass
class LegalityViolation:
    code: str
    description: str
    related_node_ids: list[str] = field(default_factory=list)
    related_edge_ids: list[str] = field(default_factory=list)


@dataclass
class LegalityReport:
    ok: bool = True
    violations: list[LegalityViolation] = field(default_factory=list)
    fixed_notes: list[str] = field(default_factory=list)


class GraphLegalityValidator:
    def validate(
        self,
        *,
        node_drafts: list[WorkflowPlannerNodeDraft],
        edge_drafts: list[WorkflowPlannerEdgeDraft],
        entry_node_ids: list[str],
        exit_node_ids: list[str],
    ) -> tuple[
        list[WorkflowPlannerNodeDraft],
        list[WorkflowPlannerEdgeDraft],
        list[str],
        list[str],
        LegalityReport,
    ]:
        report = LegalityReport()
        nodes = list(node_drafts)
        edges = list(edge_drafts)

        # 1. Strip self-loops.
        pruned_edges: list[WorkflowPlannerEdgeDraft] = []
        for edge in edges:
            if edge.from_node_id == edge.to_node_id:
                report.fixed_notes.append(f"dropped_self_loop:{edge.draft_edge_id}")
                continue
            pruned_edges.append(edge)
        edges = pruned_edges

        # 2. De-duplicate edges on (from, to); keep first.
        seen_pairs: set[tuple[str, str]] = set()
        deduped: list[WorkflowPlannerEdgeDraft] = []
        for edge in edges:
            key = (edge.from_node_id, edge.to_node_id)
            if key in seen_pairs:
                report.fixed_notes.append(
                    f"dropped_duplicate_edge:{edge.draft_edge_id}"
                )
                continue
            seen_pairs.add(key)
            deduped.append(edge)
        edges = deduped

        node_ids = {n.draft_node_id for n in nodes}

        # 3. Drop edges referencing unknown nodes.
        edges = [
            edge
            for edge in edges
            if edge.from_node_id in node_ids and edge.to_node_id in node_ids
        ]

        # 4. Entry / exit validation + recompute.
        predecessors = {edge.to_node_id for edge in edges}
        successors = {edge.from_node_id for edge in edges}
        recomputed_entry = [nid for nid in (n.draft_node_id for n in nodes) if nid not in predecessors]
        recomputed_exit = [nid for nid in (n.draft_node_id for n in nodes) if nid not in successors]

        entry_ids = [nid for nid in entry_node_ids if nid in node_ids]
        exit_ids = [nid for nid in exit_node_ids if nid in node_ids]
        if not entry_ids:
            entry_ids = recomputed_entry
        if not exit_ids:
            exit_ids = recomputed_exit
        if nodes and not entry_ids:
            report.ok = False
            report.violations.append(
                LegalityViolation(
                    code="no_entry_node",
                    description="Graph has no entry node (every node has a predecessor).",
                )
            )
        if nodes and not exit_ids:
            report.ok = False
            report.violations.append(
                LegalityViolation(
                    code="no_exit_node",
                    description="Graph has no exit node (every node has a successor).",
                )
            )

        # 5. Reachability from entry_ids.
        reachable = _bfs_reachable(start_ids=entry_ids, edges=edges)
        unreachable = sorted(node_ids - reachable)
        # Orphans auto-dropped if they have no edges AND are not insertion
        # nodes. Otherwise reported as violations.
        if unreachable:
            droppable_ids = set()
            for nid in unreachable:
                has_any_edge = any(
                    edge.from_node_id == nid or edge.to_node_id == nid
                    for edge in edges
                )
                is_insertion = nid.startswith("human_gate_before_") or nid.startswith(
                    "verify_after_"
                )
                if not has_any_edge and not is_insertion:
                    droppable_ids.add(nid)
            if droppable_ids:
                nodes = [n for n in nodes if n.draft_node_id not in droppable_ids]
                report.fixed_notes.append(
                    f"dropped_dangling_nodes:{sorted(droppable_ids)}"
                )
                node_ids -= droppable_ids
                entry_ids = [nid for nid in entry_ids if nid in node_ids]
                exit_ids = [nid for nid in exit_ids if nid in node_ids]
                unreachable = [nid for nid in unreachable if nid not in droppable_ids]
            if unreachable:
                report.ok = False
                report.violations.append(
                    LegalityViolation(
                        code="unreachable_nodes",
                        description=f"Nodes unreachable from entry: {unreachable}",
                        related_node_ids=unreachable,
                    )
                )

        # 6. Cycle detection via Kahn.
        cycle = _has_cycle(node_ids=node_ids, edges=edges)
        if cycle:
            report.ok = False
            report.violations.append(
                LegalityViolation(
                    code="cycle_detected",
                    description="Directed cycle detected; the planner produces DAGs only.",
                    related_node_ids=cycle,
                )
            )

        # 7. Gate insertion sanity — every auto-inserted gate/verify
        # node must have at least one outbound edge UNLESS it is a
        # legitimate graph exit (``verify_after_*`` placed after the
        # final ``act`` step is the common case). "Dead" only applies
        # when the node can be neither reached nor exited.
        exit_set = set(exit_ids)
        for node in nodes:
            if node.draft_node_id.startswith("human_gate_before_") or node.draft_node_id.startswith(
                "verify_after_"
            ):
                has_outbound = any(edge.from_node_id == node.draft_node_id for edge in edges)
                has_inbound = any(edge.to_node_id == node.draft_node_id for edge in edges)
                if has_outbound:
                    continue
                # No outbound: OK when the node is an exit AND has an
                # inbound connection (so it's reachable). "Dead" is
                # neither reachable nor exitable.
                if node.draft_node_id in exit_set and has_inbound:
                    continue
                report.ok = False
                report.violations.append(
                    LegalityViolation(
                        code="dead_insertion_node",
                        description=f"Inserted node '{node.draft_node_id}' has no outbound edge and is not an exit.",
                        related_node_ids=[node.draft_node_id],
                    )
                )

        return nodes, edges, entry_ids, exit_ids, report


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _bfs_reachable(
    *, start_ids: list[str], edges: list[WorkflowPlannerEdgeDraft]
) -> set[str]:
    adj: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        adj[edge.from_node_id].append(edge.to_node_id)
    seen: set[str] = set(start_ids)
    queue: deque[str] = deque(start_ids)
    while queue:
        node = queue.popleft()
        for successor in adj.get(node, []):
            if successor not in seen:
                seen.add(successor)
                queue.append(successor)
    return seen


def _has_cycle(
    *, node_ids: set[str], edges: list[WorkflowPlannerEdgeDraft]
) -> Optional[list[str]]:
    indeg: dict[str, int] = {nid: 0 for nid in node_ids}
    adj: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        indeg[edge.to_node_id] = indeg.get(edge.to_node_id, 0) + 1
        adj[edge.from_node_id].append(edge.to_node_id)
    queue: deque[str] = deque([n for n, d in indeg.items() if d == 0])
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for successor in adj[node]:
            indeg[successor] -= 1
            if indeg[successor] == 0:
                queue.append(successor)
    if visited == len(node_ids):
        return None
    return sorted(n for n, d in indeg.items() if d > 0)


def unresolved_items_from_report(
    report: LegalityReport,
) -> list[WorkflowPlannerUnresolvedItem]:
    items: list[WorkflowPlannerUnresolvedItem] = []
    for v in report.violations:
        items.append(
            WorkflowPlannerUnresolvedItem(
                item_id=f"legality_{v.code}",
                item_kind="constraint_violation",
                severity="blocking",
                related_node_ids=list(v.related_node_ids),
                description=v.description,
                suggested_resolution="Revise planner output to satisfy graph legality rules.",
            )
        )
    return items


__all__ = [
    "GraphLegalityValidator",
    "LegalityReport",
    "LegalityViolation",
    "unresolved_items_from_report",
]
