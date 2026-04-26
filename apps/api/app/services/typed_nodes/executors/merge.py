"""``merge`` executor — SQL-style join of two tabular_records inputs.

Inputs (in order): [left, right]
Output: merged tabular_records

Implementation strategy:
    Convert both inputs to pandas DataFrames, run pd.merge(), then
    convert back to list[dict] with NaN/NaT/<NA> mapped to Python None.

Why pandas: we already depend on it for read_csv. The alternative
(stdlib hash-join) would be ~150 LOC for less correctness. Pandas
also gives us validate="one_to_one" for free, which is exactly the
m:n explosion guard the spare_parts case wanted.

Two safety guards beyond pandas:
    * ``require_no_nulls_after``: after merge, any null in these
      columns triggers MergeError. Catches the spare_parts-style
      "forgot to join inventory_rt → available_qty stays None"
      class of bug at the JOIN step itself, not 5 nodes downstream.
    * ``require_one_to_one``: maps to pandas validate=. Detects
      data shape violations (an "id" column that's secretly
      duplicated on the right side) at run time.
"""
from __future__ import annotations

from typing import Any, ClassVar

import pandas as pd

from app.services.typed_nodes.base import TypedNodeExecutor
from app.services.typed_nodes.configs import MergeConfig
from app.services.typed_nodes.errors import MergeError
from app.services.typed_nodes.executors.read_csv import _normalize_value
from app.services.typed_nodes.registry import register_typed_node


def _as_key_list(k: str | list[str] | None) -> list[str]:
    if k is None:
        return []
    if isinstance(k, str):
        return [k]
    return list(k)


class MergeExecutor(TypedNodeExecutor):
    node_type: ClassVar[str] = "merge"
    config_class: ClassVar[type] = MergeConfig

    def execute(self, config: MergeConfig, inputs: list[Any]) -> list[dict[str, Any]]:
        if len(inputs) != 2:
            raise MergeError(
                f"merge expects exactly 2 inputs (left, right), got {len(inputs)}",
                node_type="merge",
                details={"got_inputs": len(inputs)},
            )
        left_records, right_records = inputs
        if not isinstance(left_records, list) or not isinstance(right_records, list):
            raise MergeError(
                "merge inputs must be list[dict] (left, right)",
                node_type="merge",
                details={
                    "left_type": type(left_records).__name__,
                    "right_type": type(right_records).__name__,
                },
            )

        # Build DataFrames. When one side is empty, an empty
        # DataFrame has zero columns (so the join-key column doesn't
        # exist on it); we materialize a typed empty frame with the
        # join keys present so pd.merge can still work.
        if config.on is not None:
            join_keys_left = _as_key_list(config.on)
            join_keys_right = _as_key_list(config.on)
        else:
            join_keys_left = _as_key_list(config.left_on)
            join_keys_right = _as_key_list(config.right_on)

        if left_records:
            left_df = pd.DataFrame(left_records)
        else:
            left_df = pd.DataFrame(columns=join_keys_left)
        if right_records:
            right_df = pd.DataFrame(right_records)
        else:
            right_df = pd.DataFrame(columns=join_keys_right)

        # Resolve which keys we'll check for presence
        if config.on is not None:
            keys = _as_key_list(config.on)
            for k in keys:
                if not left_df.empty and k not in left_df.columns:
                    raise MergeError(
                        f"join key {k!r} missing on left side",
                        node_type="merge",
                        details={
                            "missing_key": k,
                            "side": "left",
                            "available_columns": list(left_df.columns),
                        },
                    )
                if not right_df.empty and k not in right_df.columns:
                    raise MergeError(
                        f"join key {k!r} missing on right side",
                        node_type="merge",
                        details={
                            "missing_key": k,
                            "side": "right",
                            "available_columns": list(right_df.columns),
                        },
                    )
            merge_kwargs: dict[str, Any] = {"on": config.on}
        else:
            left_keys = _as_key_list(config.left_on)
            right_keys = _as_key_list(config.right_on)
            for k in left_keys:
                if not left_df.empty and k not in left_df.columns:
                    raise MergeError(
                        f"left_on key {k!r} missing on left side",
                        node_type="merge",
                        details={
                            "missing_key": k,
                            "side": "left",
                            "available_columns": list(left_df.columns),
                        },
                    )
            for k in right_keys:
                if not right_df.empty and k not in right_df.columns:
                    raise MergeError(
                        f"right_on key {k!r} missing on right side",
                        node_type="merge",
                        details={
                            "missing_key": k,
                            "side": "right",
                            "available_columns": list(right_df.columns),
                        },
                    )
            merge_kwargs = {"left_on": config.left_on, "right_on": config.right_on}

        merge_kwargs["how"] = config.how
        merge_kwargs["suffixes"] = config.suffixes
        if config.require_one_to_one:
            merge_kwargs["validate"] = "one_to_one"

        try:
            merged = pd.merge(left_df, right_df, **merge_kwargs)
        except pd.errors.MergeError as exc:
            raise MergeError(
                f"pandas merge failed: {exc}",
                node_type="merge",
                details={"cause": str(exc)},
            ) from exc
        except ValueError as exc:
            # validate="one_to_one" raises ValueError on m:n
            raise MergeError(
                f"merge validation failed: {exc}",
                node_type="merge",
                details={"cause": str(exc)},
            ) from exc

        # require_no_nulls_after — the typed-node version of 7.6F's
        # "all available_qty are 0" pain point detector.
        for col in config.require_no_nulls_after:
            if col not in merged.columns:
                raise MergeError(
                    f"require_no_nulls_after column {col!r} not in merged result",
                    node_type="merge",
                    details={
                        "missing_column": col,
                        "available_columns": list(merged.columns),
                    },
                )
            null_mask = merged[col].isna()
            null_count = int(null_mask.sum())
            if null_count > 0:
                example_indices = merged[null_mask].index[:5].tolist()
                raise MergeError(
                    f"column {col!r} has {null_count} null(s) after merge "
                    f"— upstream may have failed to provide a key match",
                    node_type="merge",
                    details={
                        "column": col,
                        "null_count": null_count,
                        "example_row_indices": [int(i) for i in example_indices],
                    },
                )

        # Convert back to list[dict] with proper Python types
        records: list[dict[str, Any]] = []
        for _, row in merged.iterrows():
            rec: dict[str, Any] = {}
            for col in merged.columns:
                rec[col] = _normalize_value(row[col])
            records.append(rec)
        return records

    @staticmethod
    def output_schema(
        config: MergeConfig, *upstream_schemas: set[str]
    ) -> set[str]:
        """Compute output column set for the synthesis layer.

        Collision rules mirror pandas' suffixes semantics:
          * ``on``-style merge: key columns appear once. Non-key columns
            shared between left and right get suffixes appended.
          * ``left_on``/``right_on``-style: both key sets are kept
            separately; shared non-key columns get suffixes.
        """
        if len(upstream_schemas) != 2:
            return set()
        left, right = upstream_schemas
        sx, sy = config.suffixes

        if config.on is not None:
            keys = set(_as_key_list(config.on))
            non_key_left = left - keys
            non_key_right = right - keys
            shared = non_key_left & non_key_right
            out = set(keys)
            out |= non_key_left - shared
            out |= non_key_right - shared
            for col in shared:
                out.add(col + sx)
                out.add(col + sy)
            return out

        # left_on / right_on path: keys are kept separately on both sides.
        shared = left & right
        out = (left - shared) | (right - shared)
        for col in shared:
            out.add(col + sx)
            out.add(col + sy)
        return out


register_typed_node(
    MergeExecutor,
    summary="SQL-style join of two tabular_records inputs (left, right) on key column(s)",
)
