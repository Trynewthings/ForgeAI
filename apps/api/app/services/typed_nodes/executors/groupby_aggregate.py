"""``groupby_aggregate`` executor — group rows + reduce to one row per group.

Input: 1 ``tabular_records`` (list[dict])
Output: 1 ``tabular_records`` with shape:
    one row per distinct (group_keys...) tuple
    columns = group_keys + [op.target for op in aggregations]

Implementation strategy:
    Convert input to pandas DataFrame, run df.groupby(group_keys).agg(...),
    flatten the multi-index back to plain columns, normalize NaN/NaT/<NA>
    to Python None.

Arithmetic-function semantics:
    sum / mean / median / min / max require numeric source columns.
    Strings that LOOK numeric (read_csv with dtype="str" path, or
    formula_eval producing string outputs) are coerced via pd.to_numeric
    with ``errors="coerce"`` — bad cells become NaN and pandas' default
    skipna=True drops them. This matches formula_eval's defensive
    coercion and keeps the typed-node story consistent.

Count semantics:
    ``count`` counts non-null entries in ``source`` per group (matches
    SQL COUNT(col)). To count rows regardless of nullness, use any
    non-null column (e.g. a group key).

NULL handling on group keys:
    pandas groupby drops rows with NaN in any group key by default.
    We follow that — typed-node group keys are expected to be non-null
    (use a schema_validate upstream if you need the guarantee). Rows
    with NULL group keys are silently excluded; this matches SQL
    ``GROUP BY``.

Empty input:
    Returns []. No fabricated empty group.
"""
from __future__ import annotations

from typing import Any, ClassVar

import pandas as pd

from app.services.typed_nodes.base import TypedNodeExecutor
from app.services.typed_nodes.configs import GroupByAggregateConfig
from app.services.typed_nodes.configs.groupby_aggregate_config import (
    AggregationOp,
)
from app.services.typed_nodes.errors import GroupByError
from app.services.typed_nodes.executors.read_csv import _normalize_value
from app.services.typed_nodes.registry import register_typed_node


# pandas-aggregation function names. Keep this in lockstep with
# AggFunction in groupby_aggregate_config.py — the Pydantic Literal
# already enforces the set, but having the dispatch table here makes
# the executor self-contained.
_ARITHMETIC_FUNCS = {"sum", "mean", "median", "min", "max"}
_COUNT_FUNCS = {"count", "nunique"}
_FIRST_LAST_FUNCS = {"first", "last"}
_ALL_FUNCS = _ARITHMETIC_FUNCS | _COUNT_FUNCS | _FIRST_LAST_FUNCS


def _coerce_numeric_series(s: pd.Series) -> pd.Series:
    """Best-effort numeric coercion for arithmetic aggregations.

    String columns flowing in from read_csv (or formula_eval string
    outputs) get converted; truly non-numeric strings become NaN
    and pandas' skipna=True silently excludes them from sum/mean/etc.
    """
    return pd.to_numeric(s, errors="coerce")


class GroupByAggregateExecutor(TypedNodeExecutor):
    node_type: ClassVar[str] = "groupby_aggregate"
    config_class: ClassVar[type] = GroupByAggregateConfig

    def execute(
        self,
        config: GroupByAggregateConfig,
        inputs: list[Any],
    ) -> list[dict[str, Any]]:
        if len(inputs) != 1:
            raise GroupByError(
                f"groupby_aggregate expects exactly 1 input, got {len(inputs)}",
                node_type="groupby_aggregate",
                details={"got_inputs": len(inputs)},
            )
        records = inputs[0]
        if not isinstance(records, list):
            raise GroupByError(
                f"input must be list[dict], got {type(records).__name__}",
                node_type="groupby_aggregate",
                details={"input_type": type(records).__name__},
            )

        if not records:
            return []

        df = pd.DataFrame(records)

        # Validate that every group key + every aggregation source is
        # present in the input columns. We check this BEFORE any
        # aggregation so the error points at the offending name and
        # lists what was actually available.
        missing_keys = [k for k in config.group_keys if k not in df.columns]
        if missing_keys:
            raise GroupByError(
                f"group_keys missing from input: {missing_keys}",
                node_type="groupby_aggregate",
                details={
                    "missing_keys": missing_keys,
                    "available_columns": sorted(df.columns.tolist()),
                },
            )
        missing_sources = [
            op.source for op in config.aggregations if op.source not in df.columns
        ]
        if missing_sources:
            raise GroupByError(
                f"aggregation source(s) missing from input: {missing_sources}",
                node_type="groupby_aggregate",
                details={
                    "missing_sources": missing_sources,
                    "available_columns": sorted(df.columns.tolist()),
                },
            )

        # For arithmetic aggregations, defensively coerce the source
        # column to numeric so the path works regardless of whether
        # upstream emitted real numbers or numeric-looking strings.
        # We mutate a copy so we don't disturb the caller's records.
        work = df.copy()
        # Track sources we've already coerced — different aggregations
        # over the same source column shouldn't re-coerce.
        coerced: set[str] = set()
        for op in config.aggregations:
            if op.fn in _ARITHMETIC_FUNCS and op.source not in coerced:
                work[op.source] = _coerce_numeric_series(work[op.source])
                coerced.add(op.source)

        # Build the agg dict: source_column -> list of (target, fn) we
        # need from it. We then call groupby().agg() with named
        # aggregations to get well-defined output columns.
        try:
            grouped = work.groupby(
                list(config.group_keys),
                sort=config.sort_by_group,
                dropna=True,  # rows with NULL in a group key are excluded
            )
            named_aggs: dict[str, tuple[str, str]] = {
                op.target: (op.source, op.fn) for op in config.aggregations
            }
            agg_df = grouped.agg(**{
                target: pd.NamedAgg(column=src, aggfunc=fn)
                for target, (src, fn) in named_aggs.items()
            })
        except Exception as exc:  # noqa: BLE001
            raise GroupByError(
                f"groupby aggregation failed: {exc}",
                node_type="groupby_aggregate",
                details={
                    "group_keys": list(config.group_keys),
                    "aggregations": [op.model_dump() for op in config.aggregations],
                    "cause": str(exc),
                },
            ) from exc

        # Move group keys back into columns (they're in the index).
        agg_df = agg_df.reset_index()

        # Convert to list[dict], normalizing NaN/NaT/<NA> → None.
        out: list[dict[str, Any]] = []
        for _, row in agg_df.iterrows():
            rec: dict[str, Any] = {}
            for col in agg_df.columns:
                rec[col] = _normalize_value(row[col])
            out.append(rec)
        return out

    @staticmethod
    def output_schema(
        config: GroupByAggregateConfig,
        *upstream_schemas: set[str],
    ) -> set[str]:
        """Output schema = group_keys ∪ {op.target for op in aggregations}.

        Group keys flow through; non-key columns from upstream are
        DROPPED unless they appear as an aggregation source (and even
        then only the aggregated form survives, not the raw column).
        This matches SQL GROUP BY semantics: post-aggregation, a
        column either is a key, is an aggregate, or is gone.
        """
        out: set[str] = set(config.group_keys)
        for op in config.aggregations:
            out.add(op.target)
        return out


register_typed_node(
    GroupByAggregateExecutor,
    summary=(
        "Group rows by one or more keys and reduce each group via "
        "sum / mean / median / min / max / count / nunique / first / last"
    ),
)


__all__ = ["GroupByAggregateExecutor", "AggregationOp"]
