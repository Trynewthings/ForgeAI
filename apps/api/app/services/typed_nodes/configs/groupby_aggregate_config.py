"""Config schema for the ``groupby_aggregate`` typed node."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


AggFunction = Literal[
    "sum", "mean", "median", "min", "max",
    "count", "first", "last", "nunique",
]


class AggregationOp(BaseModel):
    """One aggregation produced by groupby_aggregate."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(
        ...,
        description="Output column name in the aggregated result.",
        min_length=1,
    )
    source: str = Field(
        ...,
        description=(
            "Input column being aggregated. Must exist in the upstream "
            "tabular_records schema."
        ),
        min_length=1,
    )
    fn: AggFunction = Field(
        ...,
        description=(
            "Aggregation function. Arithmetic functions (sum/mean/median/"
            "min/max) require ``source`` to be numeric."
        ),
    )


class GroupByAggregateConfig(BaseModel):
    """Group rows by ``group_keys`` and produce one row per group."""

    model_config = ConfigDict(extra="forbid")

    group_keys: list[str] = Field(
        ...,
        description=(
            "Columns to group by. Output has one row per distinct "
            "(group_keys) tuple."
        ),
        min_length=1,
    )
    aggregations: list[AggregationOp] = Field(
        ...,
        description="One AggregationOp per output measure.",
        min_length=1,
    )
    sort_by_group: bool = Field(
        default=True,
        description=(
            "When True, output rows are sorted by group_keys for "
            "determinism. Recommended."
        ),
    )

    @model_validator(mode="after")
    def _check_unique_targets(self):
        targets = [op.target for op in self.aggregations]
        # group_keys + aggregation targets together must be unique
        # (a group key with the same name as an agg target would
        # collide in pandas output)
        seen: set[str] = set(self.group_keys)
        if len(self.group_keys) != len(seen):
            raise ValueError(
                f"group_keys contains duplicates: {self.group_keys}"
            )
        for op in self.aggregations:
            if op.target in seen:
                raise ValueError(
                    f"aggregation target {op.target!r} collides with "
                    f"group_keys or another aggregation"
                )
            seen.add(op.target)
        return self
