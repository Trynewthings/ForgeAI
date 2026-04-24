"""Edge contract validator — deterministic check that each planner edge
A → B has matching contracts on either end.

The validator computes "effective data-flow edges" first: planner edges
may pass through skippable nodes (``human_gate_*``) which are
pass-throughs for data, so the real contract lives between the two
non-skippable nodes that bracket a skippable chain. E.g. on the
spare-parts scenario the planner produced:

    planner_node_5 → human_gate_before_planner_node_6 → planner_node_6

and the real contract we care about is (planner_node_5, planner_node_6).
Without the transitive bridging, the edge-level check would walk past
the Node 5 vs Node 6 schema mismatch that the spec LLM actually
emitted.

Terminator-style skippable nodes (``verify_after_*``) are not bridged —
they are observers, not consumers, so their incoming edges are simply
dropped from the effective set.

Failure modes:

    R1  ``spec_missing``
        One end of an effective edge has no spec and isn't skipped
        (spec bundle is incomplete).

    R2  ``missing_input_reference``
        B's ``input_contract.required`` has no slot that references A.
        A match is established by any of:
            * ``slot.source_hint == A.draft_node_id``           (preferred)
            * ``slot.source_hint`` resolves (via skippable) to A
            * ``slot.source_hint == A.output.name``
            * ``slot.name        == A.output.name``

    R3  ``schema_mismatch``
        B's matched slot declares a non-empty ``schema_hint`` AND A's
        primary_output declares a non-empty ``schema_hint`` AND not all
        of B's required fields appear in A's emitted fields.
        Empty hints on either side mean "unknown, assume compatible".
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Optional

from app.schemas.node_execution_spec import (
    EdgeContractIssue,
    EdgeContractReport,
    NodeExecutionSpec,
    NodeInputSlot,
)
from app.schemas.workflow_planner import WorkflowPlannerOutput

logger = logging.getLogger(__name__)


# Prefix used by the planner's constraint_enforcer for auto-inserted
# verify terminators. Edges touching a verify terminator are ignored:
# verify nodes are observers, not data consumers.
_VERIFY_PREFIX = "verify_after_"


def validate_edge_contracts(
    *,
    planner_output: WorkflowPlannerOutput,
    specs: list[NodeExecutionSpec],
    skipped_node_ids: list[str],
) -> EdgeContractReport:
    """Walk the effective data-flow edges and emit issues per mismatch."""
    spec_by_id = {s.node_id: s for s in specs}
    skipped = set(skipped_node_ids)
    report = EdgeContractReport()

    effective_edges, raw_skipped_count = _compute_effective_edges(
        planner_output=planner_output, skipped=skipped
    )
    report.skipped_edges = raw_skipped_count

    for src_id, dst_id, proxy_source_ids in effective_edges:
        report.checked_edges += 1
        src_spec = spec_by_id.get(src_id)
        dst_spec = spec_by_id.get(dst_id)

        if src_spec is None or dst_spec is None:
            missing = []
            if src_spec is None:
                missing.append(f"src={src_id}")
            if dst_spec is None:
                missing.append(f"dst={dst_id}")
            report.issues.append(
                EdgeContractIssue(
                    edge_id=f"{src_id}__{dst_id}",
                    from_node_id=src_id,
                    to_node_id=dst_id,
                    kind="spec_missing",
                    detail=f"missing spec: {', '.join(missing)}",
                )
            )
            continue

        src_output = src_spec.output_contract.primary_output
        matched_slot = _find_matching_slot(
            dst_spec=dst_spec,
            src_node_id=src_id,
            src_output_name=src_output.name,
            proxy_source_ids=proxy_source_ids,
        )
        if matched_slot is None:
            declared = ", ".join(
                f"{slot.name}(src_hint={slot.source_hint!r})"
                for slot in dst_spec.input_contract.required
            ) or "<none>"
            report.issues.append(
                EdgeContractIssue(
                    edge_id=f"{src_id}__{dst_id}",
                    from_node_id=src_id,
                    to_node_id=dst_id,
                    kind="missing_input_reference",
                    detail=(
                        f"dst {dst_id} has no required input slot "
                        f"referencing src {src_id} (src output "
                        f"name={src_output.name!r}). dst required slots: "
                        f"[{declared}]"
                    ),
                )
            )
            continue

        src_fields = [f for f in (src_output.schema_hint or []) if f]
        dst_fields = [f for f in (matched_slot.schema_hint or []) if f]
        if src_fields and dst_fields:
            missing_fields = [f for f in dst_fields if f not in set(src_fields)]
            if missing_fields:
                report.issues.append(
                    EdgeContractIssue(
                        edge_id=f"{src_id}__{dst_id}",
                        from_node_id=src_id,
                        to_node_id=dst_id,
                        kind="schema_mismatch",
                        detail=(
                            f"dst input slot {matched_slot.name!r} expects "
                            f"fields {missing_fields} that src "
                            f"{src_id} does NOT emit. "
                            f"src emits: {src_fields}. dst expects: {dst_fields}."
                        ),
                        missing_fields=missing_fields,
                    )
                )
                continue

    if report.issues:
        logger.info(
            "edge_contract_validator.issues_found count=%d checked=%d skipped=%d",
            len(report.issues),
            report.checked_edges,
            report.skipped_edges,
        )
    return report


def _compute_effective_edges(
    *,
    planner_output: WorkflowPlannerOutput,
    skipped: set,
) -> tuple[list[tuple[str, str, set]], int]:
    """Collapse planner edges that pass through pass-through skippable
    nodes (``human_gate_*``) into direct edges between the bracketing
    non-skippable specs.

    Returns ``(effective_edges, raw_skipped_count)`` where
    ``effective_edges`` is a list of ``(src, dst, proxy_src_ids)``
    tuples — ``proxy_src_ids`` is the set of skippable intermediate
    node ids the dst may have used verbatim as its ``source_hint``,
    so the validator can still match B's declared source_hint against
    the bridged skippable id.
    """
    downstream_by_node: dict[str, list[str]] = {}
    for edge in planner_output.edge_drafts:
        downstream_by_node.setdefault(edge.from_node_id, []).append(edge.to_node_id)

    # Count raw planner edges that touch a skipped terminator (for audit).
    raw_skipped_count = sum(
        1
        for edge in planner_output.edge_drafts
        if edge.to_node_id.startswith(_VERIFY_PREFIX)
        or edge.from_node_id.startswith(_VERIFY_PREFIX)
    )

    real_node_ids = [
        n.draft_node_id for n in planner_output.node_drafts if n.draft_node_id not in skipped
    ]
    effective: list[tuple[str, str, set]] = []
    for src_id in real_node_ids:
        # BFS across skippable intermediates.
        visited: set = set()
        queue: deque = deque()
        proxy_for_dst: dict[str, set] = {}
        for child in downstream_by_node.get(src_id, []):
            queue.append((child, set()))  # (node, proxy_chain_so_far)
        while queue:
            current, proxy_chain = queue.popleft()
            if current in visited:
                continue
            visited.add(current)

            # Verify terminators are observers — drop the traversal here.
            if current.startswith(_VERIFY_PREFIX):
                continue

            if current in skipped:
                # Pass-through (e.g. human_gate_*) — extend proxy chain and
                # continue walking downstream.
                new_chain = proxy_chain | {current}
                for grand in downstream_by_node.get(current, []):
                    queue.append((grand, new_chain))
                continue

            # Real spec node — record the effective edge + proxy chain.
            existing = proxy_for_dst.get(current)
            if existing is None:
                proxy_for_dst[current] = set(proxy_chain)
            else:
                existing.update(proxy_chain)

        for dst_id, proxy_ids in proxy_for_dst.items():
            effective.append((src_id, dst_id, proxy_ids))
    return effective, raw_skipped_count


def _find_matching_slot(
    *,
    dst_spec: NodeExecutionSpec,
    src_node_id: str,
    src_output_name: str,
    proxy_source_ids: set,
) -> Optional[NodeInputSlot]:
    slots = dst_spec.input_contract.required

    # Pass 1: source_hint == src node_id (the strongest signal).
    for slot in slots:
        if (slot.source_hint or "") == src_node_id:
            return slot
    # Pass 2: source_hint names a pass-through skippable proxy on the path
    # from src to dst. This is what lets us match an LLM that set
    # source_hint=human_gate_* when the real producer is the ancestor.
    for slot in slots:
        hint = (slot.source_hint or "")
        if hint and hint in proxy_source_ids:
            return slot
    # Pass 3: source_hint == src primary_output.name.
    for slot in slots:
        if (slot.source_hint or "") == src_output_name:
            return slot
    # Pass 4: slot name == src primary_output.name (weakest).
    for slot in slots:
        if slot.name == src_output_name:
            return slot
    return None


__all__ = ["validate_edge_contracts"]
