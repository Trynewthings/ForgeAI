"""Phase C — evidence_flow_validator.

Single source of truth for edges + payload_mapping + initial
``semantic_role``. Consumes:

    * ``node_drafts`` (1:1 with subgoals)
    * ``subgoals`` (carries ``input_evidence_kinds`` / ``output_evidence_kinds``)
    * optional ``shape_dependency_hints`` from Shape-LLM (Phase B-Shape)
    * optional ``fallback_dependency_map`` — legacy linear/DAG map used
      when neither evidence kinds nor shape hints exist.

Produces:

    * a merged, de-duplicated list of ``WorkflowPlannerEdgeDraft``
    * each edge carries ``planner_metadata[META_EDGE_SOURCE]`` telling
      downstream modules why it exists
    * a ``EvidenceFlowReport`` that lists rejected shape hints

Design:
    1. Materialise "candidate edges" from Shape-LLM dependency hints.
    2. For each (consumer, upstream) pair, evaluate whether the
       upstream's ``output_evidence_kinds`` intersect the consumer's
       ``input_evidence_kinds``. If yes → record as evidence-backed.
    3. For consumers whose input_evidence_kinds are non-empty but have
       no evidence-backed producer, scan the node order for the nearest
       upstream that emits any matching kind. Add an evidence edge.
    4. Shape-LLM hints without evidence backing survive as
       ``ordering_hint`` edges (semantic_role=``control_sequence``).
    5. When NEITHER evidence kinds NOR shape hints are available, fall
       back to ``fallback_dependency_map`` to preserve legacy behaviour.

Edges are keyed by ``(from_node_id, to_node_id)``; duplicates merge
payload_mapping entries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.schemas.solvability import SolvabilitySubgoal
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
)
from app.services.workflow_planner.runtime_field_enricher import (
    META_EDGE_SOURCE,
    META_EVIDENCE_KIND,
    enrich_edge,
)


@dataclass
class ShapeDependencyHint:
    """Shape-LLM dependency hint — subgoal-to-subgoal ordering suggestion."""

    from_subgoal_id: str
    to_subgoal_id: str
    reason: Optional[str] = None


@dataclass
class EvidenceFlowReport:
    accepted_hint_count: int = 0
    rejected_hints: list[dict] = field(default_factory=list)
    unbacked_consumers: list[str] = field(default_factory=list)
    fallback_used: bool = False


class EvidenceFlowValidator:
    """Authoritative edge producer for the planner pipeline."""

    def wire_edges(
        self,
        *,
        node_drafts: list[WorkflowPlannerNodeDraft],
        subgoals: list[SolvabilitySubgoal],
        subgoal_id_to_node_id: dict[str, str],
        shape_dependency_hints: Optional[Iterable[ShapeDependencyHint]] = None,
        fallback_dependency_map: Optional[dict[str, list[str]]] = None,
    ) -> tuple[list[WorkflowPlannerEdgeDraft], EvidenceFlowReport]:
        hints = list(shape_dependency_hints or [])
        report = EvidenceFlowReport()

        if not node_drafts:
            return [], report

        subgoal_by_id = {sg.subgoal_id: sg for sg in subgoals}
        # Node position for "nearest upstream" heuristic.
        node_positions = {
            nd.draft_node_id: idx for idx, nd in enumerate(node_drafts)
        }

        # ``edges_by_key`` merges duplicates. Keyed by (from_id, to_id).
        edges_by_key: dict[tuple[str, str], WorkflowPlannerEdgeDraft] = {}

        def _ensure_edge(
            *,
            from_node_id: str,
            to_node_id: str,
            evidence_kind: Optional[str],
            edge_source: str,
            semantic_role: str,
        ) -> WorkflowPlannerEdgeDraft:
            key = (from_node_id, to_node_id)
            edge = edges_by_key.get(key)
            if edge is None:
                edge = WorkflowPlannerEdgeDraft(
                    draft_edge_id=f"planner_edge_{len(edges_by_key) + 1}",
                    edge_type="data_flow",
                    from_node_id=from_node_id,
                    to_node_id=to_node_id,
                    semantic_role=semantic_role,
                    payload_mapping={},
                )
                enrich_edge(edge=edge, edge_source=edge_source, evidence_kind=evidence_kind)
                edges_by_key[key] = edge
            else:
                # Promote semantic_role if we found an evidence-backed reason.
                existing_source = (edge.planner_metadata or {}).get(META_EDGE_SOURCE)
                if edge_source == "evidence" and existing_source != "evidence":
                    edge.semantic_role = semantic_role
                    enrich_edge(edge=edge, edge_source="evidence", evidence_kind=evidence_kind)
            if evidence_kind:
                edge.payload_mapping[evidence_kind] = evidence_kind
            elif not edge.payload_mapping:
                edge.payload_mapping = {"primary_output": "primary_input"}
            return edge

        # 1. Expand shape hints → candidate edges.
        for hint in hints:
            from_id = subgoal_id_to_node_id.get(hint.from_subgoal_id)
            to_id = subgoal_id_to_node_id.get(hint.to_subgoal_id)
            if not from_id or not to_id or from_id == to_id:
                report.rejected_hints.append(
                    {
                        "reason": "invalid_subgoal_id",
                        "from": hint.from_subgoal_id,
                        "to": hint.to_subgoal_id,
                    }
                )
                continue
            upstream_sg = subgoal_by_id.get(hint.from_subgoal_id)
            downstream_sg = subgoal_by_id.get(hint.to_subgoal_id)
            overlap = _evidence_overlap(upstream_sg, downstream_sg)
            if overlap:
                for kind in overlap:
                    _ensure_edge(
                        from_node_id=from_id,
                        to_node_id=to_id,
                        evidence_kind=kind,
                        edge_source="evidence",
                        semantic_role="evidence_dependency",
                    )
                report.accepted_hint_count += 1
            else:
                _ensure_edge(
                    from_node_id=from_id,
                    to_node_id=to_id,
                    evidence_kind=None,
                    edge_source="ordering_hint",
                    semantic_role="control_sequence",
                )
                report.accepted_hint_count += 1

        # 2. Augment with evidence flow: for each consumer with required
        # input_evidence_kinds that are not yet satisfied, scan earlier
        # nodes for the nearest producer.
        for idx, node in enumerate(node_drafts):
            subgoal = _subgoal_for_node(subgoals=subgoals, node=node, subgoal_id_to_node_id=subgoal_id_to_node_id)
            if subgoal is None:
                continue
            needed = [k for k in (subgoal.input_evidence_kinds or []) if k]
            if not needed:
                continue
            satisfied: set[str] = {
                edge.planner_metadata.get(META_EVIDENCE_KIND)  # type: ignore[union-attr]
                for edge in edges_by_key.values()
                if edge.to_node_id == node.draft_node_id
                and edge.planner_metadata
                and edge.planner_metadata.get(META_EVIDENCE_KIND)
            }
            for kind in needed:
                if kind in satisfied:
                    continue
                producer_node_id = _nearest_producer(
                    target_idx=idx,
                    node_drafts=node_drafts,
                    subgoals=subgoals,
                    subgoal_id_to_node_id=subgoal_id_to_node_id,
                    evidence_kind=kind,
                )
                if producer_node_id:
                    _ensure_edge(
                        from_node_id=producer_node_id,
                        to_node_id=node.draft_node_id,
                        evidence_kind=kind,
                        edge_source="evidence",
                        semantic_role="evidence_dependency",
                    )
                else:
                    if node.draft_node_id not in report.unbacked_consumers:
                        report.unbacked_consumers.append(node.draft_node_id)

        # 3. Fallback: no hints and no evidence pulled anything in → rely
        # on the legacy linear/DAG dependency map so the planner never
        # regresses to an edgeless graph for simple cases.
        if not edges_by_key and fallback_dependency_map:
            report.fallback_used = True
            edge_counter = 0
            for consumer_sg_id, upstreams in fallback_dependency_map.items():
                consumer_id = subgoal_id_to_node_id.get(consumer_sg_id)
                if not consumer_id:
                    continue
                for up_sg_id in upstreams:
                    up_id = subgoal_id_to_node_id.get(up_sg_id)
                    if not up_id or up_id == consumer_id:
                        continue
                    edge_counter += 1
                    edge = WorkflowPlannerEdgeDraft(
                        draft_edge_id=f"planner_edge_{edge_counter}",
                        edge_type="data_flow",
                        from_node_id=up_id,
                        to_node_id=consumer_id,
                        semantic_role="primary_data_flow",
                        payload_mapping={"primary_output": "primary_input"},
                    )
                    enrich_edge(edge=edge, edge_source="ordering_hint")
                    edges_by_key[(up_id, consumer_id)] = edge

        # Stable ordering: group by to_node_id's position then from_node_id's position.
        edges = sorted(
            edges_by_key.values(),
            key=lambda e: (
                node_positions.get(e.to_node_id, 1 << 30),
                node_positions.get(e.from_node_id, 1 << 30),
            ),
        )
        # Re-id edges after sort so the draft_edge_id order matches traversal order.
        for i, edge in enumerate(edges, start=1):
            edge.draft_edge_id = f"planner_edge_{i}"
        return edges, report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _evidence_overlap(
    upstream: Optional[SolvabilitySubgoal], downstream: Optional[SolvabilitySubgoal]
) -> list[str]:
    if upstream is None or downstream is None:
        return []
    produced = {k for k in (upstream.output_evidence_kinds or []) if k}
    consumed = {k for k in (downstream.input_evidence_kinds or []) if k}
    return sorted(produced & consumed)


def _subgoal_for_node(
    *,
    subgoals: list[SolvabilitySubgoal],
    node: WorkflowPlannerNodeDraft,
    subgoal_id_to_node_id: dict[str, str],
) -> Optional[SolvabilitySubgoal]:
    for sg in subgoals:
        if subgoal_id_to_node_id.get(sg.subgoal_id) == node.draft_node_id:
            return sg
    return None


def _nearest_producer(
    *,
    target_idx: int,
    node_drafts: list[WorkflowPlannerNodeDraft],
    subgoals: list[SolvabilitySubgoal],
    subgoal_id_to_node_id: dict[str, str],
    evidence_kind: str,
) -> Optional[str]:
    for back in range(target_idx - 1, -1, -1):
        node = node_drafts[back]
        sg = _subgoal_for_node(
            subgoals=subgoals, node=node, subgoal_id_to_node_id=subgoal_id_to_node_id
        )
        if sg and evidence_kind in (sg.output_evidence_kinds or []):
            return node.draft_node_id
    return None


__all__ = [
    "EvidenceFlowReport",
    "EvidenceFlowValidator",
    "ShapeDependencyHint",
]
