"""Closure gap merger (Phase 1A).

Takes the three authoritative contracts produced by the partition validator
and produces a unified ``MergedGapSet`` ordered by the priority:

    problem_formulation.blocking
    > input_closure.blocking
    > output_closure.blocking
    > any.review

The merger is deterministic and does NOT call the LLM.
"""
from __future__ import annotations

from typing import Optional

from app.schemas.front_chain import (
    FrontChainLayerName,
    MergedGapItem,
    MergedGapSet,
)
from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)

MERGER_VERSION = "front_chain_closure_gap_merger_v1"


# Layer priority — lower number = higher priority.
_LAYER_PRIORITY: dict[FrontChainLayerName, int] = {
    "problem_formulation": 0,
    "input_closure": 1,
    "output_closure": 2,
}


class ClosureGapMerger:
    """Deterministic merger producing a single prioritised gap set."""

    def merge(
        self,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
    ) -> MergedGapSet:
        blocking: list[MergedGapItem] = []
        review: list[MergedGapItem] = []

        # Problem formulation — blocking axes
        blocking_reasons_by_axis = _pair_axes_with_reasons(
            axes=problem_formulation.blocking_axes,
            reasons=problem_formulation.blocking_reasons,
        )
        for axis in problem_formulation.blocking_axes:
            blocking.append(
                MergedGapItem(
                    axis=axis,
                    layer="problem_formulation",
                    severity="blocking",
                    reason=blocking_reasons_by_axis.get(axis),
                    priority=self._compute_priority("problem_formulation", "blocking"),
                )
            )
        for axis in problem_formulation.review_axes:
            review.append(
                MergedGapItem(
                    axis=axis,
                    layer="problem_formulation",
                    severity="review",
                    reason=None,
                    priority=self._compute_priority("problem_formulation", "review"),
                )
            )

        # Input closure
        for axis in input_closure.blocking_axes:
            reason = _first_requirement_reason_for_axis(input_closure, axis)
            blocking.append(
                MergedGapItem(
                    axis=axis,
                    layer="input_closure",
                    severity="blocking",
                    reason=reason,
                    priority=self._compute_priority("input_closure", "blocking"),
                )
            )
        for axis in input_closure.review_axes:
            review.append(
                MergedGapItem(
                    axis=axis,
                    layer="input_closure",
                    severity="review",
                    reason=None,
                    priority=self._compute_priority("input_closure", "review"),
                )
            )

        # Output closure
        for axis in output_closure.blocking_axes:
            reason = _first_requirement_reason_for_axis(output_closure, axis)
            blocking.append(
                MergedGapItem(
                    axis=axis,
                    layer="output_closure",
                    severity="blocking",
                    reason=reason,
                    priority=self._compute_priority("output_closure", "blocking"),
                )
            )
        for axis in output_closure.review_axes:
            review.append(
                MergedGapItem(
                    axis=axis,
                    layer="output_closure",
                    severity="review",
                    reason=None,
                    priority=self._compute_priority("output_closure", "review"),
                )
            )

        # Deterministic ordering: priority ASC, then stable by input order.
        blocking.sort(key=lambda g: (g.priority, g.layer, g.axis))
        review.sort(key=lambda g: (g.priority, g.layer, g.axis))

        prioritized_decisions = [item.axis for item in blocking] + [
            item.axis for item in review
        ]

        return MergedGapSet(
            blocking=blocking,
            review=review,
            prioritized_decisions=prioritized_decisions,
        )

    @staticmethod
    def _compute_priority(layer: FrontChainLayerName, severity: str) -> int:
        # Priority is a single integer used only for sorting: blocking always
        # beats review, and within the same severity the layer order wins.
        base = _LAYER_PRIORITY[layer]
        if severity == "blocking":
            return base  # 0, 1, 2
        return base + 100  # 100, 101, 102


def _pair_axes_with_reasons(
    *, axes: list[str], reasons: list[str]
) -> dict[str, Optional[str]]:
    paired: dict[str, Optional[str]] = {}
    for idx, axis in enumerate(axes):
        paired[axis] = reasons[idx] if idx < len(reasons) else None
    return paired


def _first_requirement_reason_for_axis(
    contract: InputClosureContract | OutputClosureContract, axis: str
) -> Optional[str]:
    for requirement in contract.closure_requirements:
        if requirement.axis == axis:
            return requirement.reason
    return None
