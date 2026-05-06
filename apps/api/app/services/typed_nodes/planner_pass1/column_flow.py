"""Pass 1 column-flow estimation.

A node-type-agnostic propagation of "what columns will flow out of
this step" across the Pass 1 DAG. Used by the Pass 1 validator
(8.0.24) to check that every ``merge`` step's two upstreams share at
least one column — a necessary condition for the merge to succeed
regardless of how Pass 2 configures the join key.

Why a separate module from PlannerInputArtifact.declared_columns:

    ``declared_columns`` is a property of an input ARTIFACT — the
    columns the file declares (or empty when unknown). It only
    answers "what's in this file".

    Pass 1 needs to answer a different question: "given the DAG
    shape, what columns will be available AT EACH STEP'S OUTPUT?"
    That requires propagating columns through the DAG with per-node
    semantics (read_csv pulls from artifact, merge unions, write_csv
    is a sink, etc.).

    This is also distinct from Pass 2's ``SchemaFlowTracker``, which
    runs at config-synthesis time when each step has a validated
    typed_config and its executor's ``output_schema()`` can be
    invoked precisely. Pass 1 has only the DAG shape, so propagation
    is necessarily a conservative estimate.

Conservatism is the safer direction: over-estimating a downstream
step's available columns produces FEWER false alarms in the
validator (the merge join-key check) and lets Pass 2 catch any
remaining issues with concrete column references.
"""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Mapping

from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    TypedNodeStepDraft,
)


def estimate_step_output_columns(
    step: TypedNodeStepDraft,
    *,
    upstream_columns: list[frozenset[str]],
    input_artifacts_by_id: Mapping[str, PlannerInputArtifact],
) -> frozenset[str]:
    """Best-effort estimate of the columns flowing out of ``step``.

    Args:
        step: The Pass 1 step draft (no config yet).
        upstream_columns: One frozenset per upstream, in the same
            order as ``step.upstream_step_ids``.
        input_artifacts_by_id: artifact_id → PlannerInputArtifact map
            so source steps can look up their consumed file.

    Returns:
        frozenset of column names. Empty when:
          - source step has no consumes_artifact_ids
          - source step's artifact has empty declared_columns
          - sink step (write_csv)
          - all upstreams are empty (e.g. transform fed by an
            unprofiled source)

    The implementation deliberately uses CONSERVATIVE heuristics —
    it returns the upstream union for any node type it doesn't have
    specific knowledge about, which over-estimates rather than
    under-estimates available columns.
    """
    nt = step.node_type

    # Source nodes: pull from the consumed input artifact.
    if nt in {"read_csv"}:
        cols: set[str] = set()
        for art_id in step.consumes_artifact_ids:
            art = input_artifacts_by_id.get(art_id)
            if art is not None:
                cols.update(art.declared_columns)
        return frozenset(cols)

    # format_convert can be either source or sink depending on
    # whether it consumes artifacts. Treat the source case here.
    if nt == "format_convert":
        if step.consumes_artifact_ids and not step.upstream_step_ids:
            cols = set()
            for art_id in step.consumes_artifact_ids:
                art = input_artifacts_by_id.get(art_id)
                if art is not None:
                    cols.update(art.declared_columns)
            return frozenset(cols)
        # Sink case: pass through upstream union (the file format
        # changes but the columns don't).
        return _union(upstream_columns)

    # Sink: terminal step, no downstream propagation needed.
    if nt == "write_csv":
        return frozenset()

    # Merge: union of upstream columns. (Pass 2 will dedup the join
    # key column from the right side, but at Pass 1 we don't know
    # which column is the join key — overestimating is safe.)
    if nt == "merge":
        return _union(upstream_columns)

    # filter / project / formula_eval / classify_state_space /
    # groupby_aggregate / schema_validate: at Pass 1 we can't
    # predict added or dropped columns without configs, so we keep
    # the conservative upstream union. This is correct enough for
    # downstream merge validation.
    if nt in {"filter", "project", "formula_eval",
              "classify_state_space", "groupby_aggregate",
              "schema_validate"}:
        return _union(upstream_columns)

    # coding_agent: escape hatch with unknown schema. Returning
    # upstream union is the conservative default; the real Pass 2
    # synthesizer reads ``declared_output_columns`` from the config
    # if available, but Pass 1 doesn't have that yet.
    if nt == "coding_agent":
        return _union(upstream_columns)

    # Unknown node type — fall through to upstream union. The
    # validator's separate ``unknown_node_type`` check will surface
    # this as an error; column_flow just stays out of the way.
    return _union(upstream_columns)


def _union(parts: list[frozenset[str]]) -> frozenset[str]:
    out: set[str] = set()
    for p in parts:
        out.update(p)
    return frozenset(out)


def propagate_columns_through_dag(
    steps: list[TypedNodeStepDraft],
    input_artifacts: list[PlannerInputArtifact],
) -> dict[str, frozenset[str]]:
    """Topological propagation of estimated output columns.

    Returns step_id → frozenset[column_name]. Steps reached through
    a cycle or hanging on a dangling upstream end up missing from
    the result; the validator's separate cycle / dangling checks
    surface those failures, so this propagator does not raise.

    Determinism: children are visited in sorted order so the
    propagation result is stable across runs (helpful for tests
    that snapshot the result).
    """
    by_id = {s.step_id: s for s in steps}
    art_by_id = {a.artifact_id: a for a in input_artifacts}

    indeg: dict[str, int] = defaultdict(int)
    children: dict[str, list[str]] = defaultdict(list)
    for s in steps:
        indeg.setdefault(s.step_id, 0)
        for up in s.upstream_step_ids:
            if up in by_id:
                indeg[s.step_id] += 1
                children[up].append(s.step_id)
            # If up isn't in by_id, the dangling_upstream validator
            # will flag it; we just skip the edge.

    queue: deque[str] = deque(sorted(sid for sid, d in indeg.items() if d == 0))
    result: dict[str, frozenset[str]] = {}

    while queue:
        sid = queue.popleft()
        s = by_id[sid]
        upstream_cols = [
            result.get(u, frozenset()) for u in s.upstream_step_ids
        ]
        result[sid] = estimate_step_output_columns(
            s,
            upstream_columns=upstream_cols,
            input_artifacts_by_id=art_by_id,
        )
        for c in sorted(children[sid]):
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)

    return result


__all__ = [
    "estimate_step_output_columns",
    "propagate_columns_through_dag",
]
