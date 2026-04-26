"""Shadow comparison for typed-node planner Pass 1.

The shadow comparator structurally diffs two ``PlannerPass1Output``
objects produced for the same input. Its purpose is TELEMETRY, not
authority: it tells you "the LLM-driven plan and the deterministic
plan agreed on N nodes, disagreed on M edges" — it does NOT decide
which one wins.

Why this exists in 8.0.11:
    Before promoting LLM-driven Pass 1 from "experimental" to
    "primary", we want production runs that EXECUTE the deterministic
    output but RECORD what the LLM would have done. Once the diff
    metrics show acceptable agreement, the authority can be flipped
    via a config flag in 8.0.16 (build / run dispatch). This is the
    same shadow→authority pattern the legacy WorkflowPlanner LLM
    integration used.

Comparisons performed:
    * step count
    * node_type multiset (Jaccard + per-type counts)
    * topology — canonicalized edge set (frozenset of (from, to))
    * entry_step_ids set / exit_step_ids set
    * artifact wiring (which artifacts each plan claims to consume /
      produce)
    * unresolved_items count + kinds

Comparisons we DO NOT do:
    * step_id match — IDs are LLM-chosen and rarely identical
    * intent text similarity — out of scope for v0; could add a
      prompt-eval metric later
    * config-level fidelity — that's Pass 2's territory

The result is a flat dataclass that serializes cleanly to JSON for
audit sidecars.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from app.services.typed_nodes.planner_pass1.schema import (
    PlannerPass1Output,
    TypedNodeStepDraft,
)


# ---------------------------------------------------------------------------
# Result envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowComparison:
    """One-shot structural diff of two Pass 1 outputs.

    Frozen and field-flat so it serializes via ``dataclasses.asdict``
    into manifest sidecars without any custom encoder.
    """

    # Step count
    primary_step_count: int
    shadow_step_count: int
    step_count_match: bool

    # node_type usage
    node_type_overlap: list[str]              # types both plans used
    node_type_only_in_primary: list[str]
    node_type_only_in_shadow: list[str]
    node_type_jaccard: float                  # 0.0–1.0

    # Topology
    edge_count_primary: int
    edge_count_shadow: int
    edge_overlap_count: int
    edge_only_in_primary_count: int
    edge_only_in_shadow_count: int
    topology_match: bool                      # exact edge set equality

    # Entry / exit
    entry_set_match: bool
    exit_set_match: bool

    # Artifact wiring
    consumed_artifact_overlap: list[str]
    produced_artifact_overlap: list[str]
    consumed_artifact_match: bool
    produced_artifact_match: bool

    # Unresolved items
    primary_unresolved_count: int
    shadow_unresolved_count: int
    primary_unresolved_kinds: list[str]
    shadow_unresolved_kinds: list[str]

    # Quick verdict — derived but stored for easy audit reads
    structural_match: bool                    # step_count_match AND topology_match AND node_types match
    summary: str

    def to_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


# ---------------------------------------------------------------------------
# Comparator helpers (kept module-level so they're individually testable)
# ---------------------------------------------------------------------------


def _edges_of(steps: list[TypedNodeStepDraft]) -> set[tuple[str, str]]:
    """Edge set as (upstream_step_id, step_id) tuples.

    Note: the comparator uses step_id-based edges, which means two
    plans with different node naming will rarely match topology
    even when they're isomorphic. This is intentional — comparing
    isomorphism would require canonical labelling, which is overkill
    for shadow telemetry. Disagreement here just means "the LLM
    picked different node names", not "the LLM is wrong".
    """
    return {(up, s.step_id) for s in steps for up in s.upstream_step_ids}


def _node_types_multiset(steps: list[TypedNodeStepDraft]) -> Counter:
    return Counter(s.node_type for s in steps)


def _consumed_artifact_set(steps: list[TypedNodeStepDraft]) -> set[str]:
    return {a for s in steps for a in s.consumes_artifact_ids}


def _produced_artifact_set(steps: list[TypedNodeStepDraft]) -> set[str]:
    return {a for s in steps for a in s.produces_artifact_ids}


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity. Empty/empty = 1.0 (perfect agreement on
    "no node_types used", which only happens for empty plans)."""
    if not a and not b:
        return 1.0
    if not (a | b):
        return 1.0
    return len(a & b) / len(a | b)


def _summary_text(c: "ShadowComparisonBuild") -> str:
    """Human-readable one-liner for the audit log.

    Implemented as a free function over the build-time intermediate
    so we can tweak summaries without rewriting the dataclass.
    """
    if c.step_count_match and c.topology_match and not c.node_type_only_in_primary and not c.node_type_only_in_shadow:
        return (
            f"structural match: {c.primary_step_count} steps, "
            f"{c.edge_count_primary} edges, identical node_type "
            f"distribution"
        )
    parts = [
        f"steps {c.primary_step_count} vs {c.shadow_step_count}",
        f"edges {c.edge_count_primary} vs {c.edge_count_shadow}",
    ]
    if c.node_type_only_in_primary:
        parts.append(f"primary-only types: {', '.join(c.node_type_only_in_primary)}")
    if c.node_type_only_in_shadow:
        parts.append(f"shadow-only types: {', '.join(c.node_type_only_in_shadow)}")
    return "structural diff: " + "; ".join(parts)


# Internal build-time intermediate so the immutable ShadowComparison
# can be assembled without re-computing its derived fields.
@dataclass
class ShadowComparisonBuild:
    primary_step_count: int = 0
    shadow_step_count: int = 0
    step_count_match: bool = False
    node_type_overlap: list[str] = field(default_factory=list)
    node_type_only_in_primary: list[str] = field(default_factory=list)
    node_type_only_in_shadow: list[str] = field(default_factory=list)
    node_type_jaccard: float = 0.0
    edge_count_primary: int = 0
    edge_count_shadow: int = 0
    edge_overlap_count: int = 0
    edge_only_in_primary_count: int = 0
    edge_only_in_shadow_count: int = 0
    topology_match: bool = False
    entry_set_match: bool = False
    exit_set_match: bool = False
    consumed_artifact_overlap: list[str] = field(default_factory=list)
    produced_artifact_overlap: list[str] = field(default_factory=list)
    consumed_artifact_match: bool = False
    produced_artifact_match: bool = False
    primary_unresolved_count: int = 0
    shadow_unresolved_count: int = 0
    primary_unresolved_kinds: list[str] = field(default_factory=list)
    shadow_unresolved_kinds: list[str] = field(default_factory=list)
    structural_match: bool = False


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def compare_pass1_outputs(
    primary: PlannerPass1Output,
    shadow: PlannerPass1Output,
) -> ShadowComparison:
    """Structural diff of two Pass 1 outputs for the same input.

    Order of arguments matters for the "only_in_*" lists: ``primary``
    is the candidate plan you want to evaluate; ``shadow`` is the
    reference you're comparing against. Convention: primary = LLM,
    shadow = deterministic baseline.
    """
    b = ShadowComparisonBuild()

    # Step count
    b.primary_step_count = len(primary.steps)
    b.shadow_step_count = len(shadow.steps)
    b.step_count_match = b.primary_step_count == b.shadow_step_count

    # Node-type sets (we expose the SET, not the multiset, in the
    # final result — counts can be derived from the raw outputs if
    # callers need them. This keeps the audit dict shallow.)
    pt_set = set(_node_types_multiset(primary.steps).keys())
    st_set = set(_node_types_multiset(shadow.steps).keys())
    b.node_type_overlap = sorted(pt_set & st_set)
    b.node_type_only_in_primary = sorted(pt_set - st_set)
    b.node_type_only_in_shadow = sorted(st_set - pt_set)
    b.node_type_jaccard = _jaccard(pt_set, st_set)

    # Topology
    p_edges = _edges_of(primary.steps)
    s_edges = _edges_of(shadow.steps)
    b.edge_count_primary = len(p_edges)
    b.edge_count_shadow = len(s_edges)
    b.edge_overlap_count = len(p_edges & s_edges)
    b.edge_only_in_primary_count = len(p_edges - s_edges)
    b.edge_only_in_shadow_count = len(s_edges - p_edges)
    b.topology_match = p_edges == s_edges

    # Entry / exit
    b.entry_set_match = set(primary.entry_step_ids) == set(shadow.entry_step_ids)
    b.exit_set_match = set(primary.exit_step_ids) == set(shadow.exit_step_ids)

    # Artifact wiring
    p_consumed = _consumed_artifact_set(primary.steps)
    s_consumed = _consumed_artifact_set(shadow.steps)
    p_produced = _produced_artifact_set(primary.steps)
    s_produced = _produced_artifact_set(shadow.steps)
    b.consumed_artifact_overlap = sorted(p_consumed & s_consumed)
    b.produced_artifact_overlap = sorted(p_produced & s_produced)
    b.consumed_artifact_match = p_consumed == s_consumed
    b.produced_artifact_match = p_produced == s_produced

    # Unresolved items
    b.primary_unresolved_count = len(primary.unresolved_items)
    b.shadow_unresolved_count = len(shadow.unresolved_items)
    b.primary_unresolved_kinds = sorted({u.item_kind for u in primary.unresolved_items})
    b.shadow_unresolved_kinds = sorted({u.item_kind for u in shadow.unresolved_items})

    # Verdict
    b.structural_match = (
        b.step_count_match
        and b.topology_match
        and not b.node_type_only_in_primary
        and not b.node_type_only_in_shadow
    )
    summary = _summary_text(b)

    return ShadowComparison(
        primary_step_count=b.primary_step_count,
        shadow_step_count=b.shadow_step_count,
        step_count_match=b.step_count_match,
        node_type_overlap=b.node_type_overlap,
        node_type_only_in_primary=b.node_type_only_in_primary,
        node_type_only_in_shadow=b.node_type_only_in_shadow,
        node_type_jaccard=b.node_type_jaccard,
        edge_count_primary=b.edge_count_primary,
        edge_count_shadow=b.edge_count_shadow,
        edge_overlap_count=b.edge_overlap_count,
        edge_only_in_primary_count=b.edge_only_in_primary_count,
        edge_only_in_shadow_count=b.edge_only_in_shadow_count,
        topology_match=b.topology_match,
        entry_set_match=b.entry_set_match,
        exit_set_match=b.exit_set_match,
        consumed_artifact_overlap=b.consumed_artifact_overlap,
        produced_artifact_overlap=b.produced_artifact_overlap,
        consumed_artifact_match=b.consumed_artifact_match,
        produced_artifact_match=b.produced_artifact_match,
        primary_unresolved_count=b.primary_unresolved_count,
        shadow_unresolved_count=b.shadow_unresolved_count,
        primary_unresolved_kinds=b.primary_unresolved_kinds,
        shadow_unresolved_kinds=b.shadow_unresolved_kinds,
        structural_match=b.structural_match,
        summary=summary,
    )


__all__ = [
    "ShadowComparison",
    "compare_pass1_outputs",
]
