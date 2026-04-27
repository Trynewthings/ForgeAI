"""``schema_validate`` executor.

Pass-through validator. Aggregates ALL violations across all rows
(capped at MAX_VIOLATIONS_REPORTED to prevent runaway in pathological
inputs) and raises a single ``SchemaCheckError`` with structured
violation list. On success, returns the input unchanged.

This is the typed-node analog of declarative table contracts — it's
explicit about what shape downstream nodes can rely on, and it
fails loud when reality drifts. Useful as a guard between joins
that should produce known shapes and downstream classifiers that
need those shapes.
"""
from __future__ import annotations

from typing import Any, ClassVar

from app.services.typed_nodes.base import TypedNodeExecutor
from app.services.typed_nodes.configs import SchemaValidateConfig
from app.services.typed_nodes.errors import SchemaCheckError
from app.services.typed_nodes.registry import register_typed_node


# Cap on violations we accumulate per-execute. A 1M-row input with a
# truly broken column would otherwise produce a 1M-element error list.
# 50 is enough for human review and CI logging.
MAX_VIOLATIONS_REPORTED = 50


def _to_number(v: Any) -> float | None:
    """Best-effort numeric coercion for value-range checks. Returns
    None when not coercible (caller treats as 'not numeric')."""
    if v is None:
        return None
    if isinstance(v, bool):
        # bool is int subclass — but for range purposes, exclude
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


class SchemaValidateExecutor(TypedNodeExecutor):
    node_type: ClassVar[str] = "schema_validate"
    config_class: ClassVar[type] = SchemaValidateConfig

    def execute(
        self,
        config: SchemaValidateConfig,
        inputs: list[Any],
    ) -> list[dict[str, Any]]:
        if len(inputs) != 1:
            raise SchemaCheckError(violations=[{
                "kind": "wrong_input_count",
                "got": len(inputs),
                "expected": 1,
            }])
        records = inputs[0]
        if not isinstance(records, list):
            raise SchemaCheckError(violations=[{
                "kind": "non_list_input",
                "got_type": type(records).__name__,
            }])

        violations: list[dict[str, Any]] = []
        expected_set = set(config.expected_columns)
        required_non_null_set = set(config.required_non_null)

        # Sanity: required_non_null must be subset of expected_columns
        # (we'd never know to check a non-expected column for nulls).
        unknown_required = required_non_null_set - expected_set
        if unknown_required:
            violations.append({
                "kind": "config_inconsistency",
                "detail": (
                    f"required_non_null contains columns not in "
                    f"expected_columns: {sorted(unknown_required)}"
                ),
            })

        for row_idx, row in enumerate(records):
            if not isinstance(row, dict):
                violations.append({
                    "kind": "non_dict_row",
                    "row_index": row_idx,
                    "got_type": type(row).__name__,
                })
                if len(violations) >= MAX_VIOLATIONS_REPORTED:
                    break
                continue

            row_keys = set(row.keys())

            # Missing expected columns
            missing = expected_set - row_keys
            for col in sorted(missing):
                violations.append({
                    "kind": "missing_column",
                    "row_index": row_idx,
                    "column": col,
                })
                if len(violations) >= MAX_VIOLATIONS_REPORTED:
                    break
            if len(violations) >= MAX_VIOLATIONS_REPORTED:
                break

            # Forbidden extra columns
            if config.forbid_extra_columns:
                extras = row_keys - expected_set
                for col in sorted(extras):
                    violations.append({
                        "kind": "unexpected_column",
                        "row_index": row_idx,
                        "column": col,
                    })
                    if len(violations) >= MAX_VIOLATIONS_REPORTED:
                        break
            if len(violations) >= MAX_VIOLATIONS_REPORTED:
                break

            # Required non-null
            for col in config.required_non_null:
                if col not in row_keys:
                    # Already flagged as missing_column above; skip
                    continue
                if row[col] is None:
                    violations.append({
                        "kind": "null_in_required",
                        "row_index": row_idx,
                        "column": col,
                    })
                    if len(violations) >= MAX_VIOLATIONS_REPORTED:
                        break
            if len(violations) >= MAX_VIOLATIONS_REPORTED:
                break

            # Value ranges (numeric inclusive)
            for col, bounds in config.value_ranges.items():
                if col not in row_keys or row[col] is None:
                    continue
                if len(bounds) != 2:
                    violations.append({
                        "kind": "config_inconsistency",
                        "detail": (
                            f"value_ranges[{col!r}] must be [lo, hi], "
                            f"got {bounds}"
                        ),
                    })
                    continue
                lo, hi = bounds
                num = _to_number(row[col])
                if num is None:
                    violations.append({
                        "kind": "non_numeric_for_range",
                        "row_index": row_idx,
                        "column": col,
                        "value": row[col],
                    })
                    continue
                if not (lo <= num <= hi):
                    violations.append({
                        "kind": "value_out_of_range",
                        "row_index": row_idx,
                        "column": col,
                        "value": row[col],
                        "range": [lo, hi],
                    })
                    if len(violations) >= MAX_VIOLATIONS_REPORTED:
                        break
            if len(violations) >= MAX_VIOLATIONS_REPORTED:
                break

            # Enum membership
            for col, allowed in config.enum_constraints.items():
                if col not in row_keys or row[col] is None:
                    continue
                if row[col] not in allowed:
                    violations.append({
                        "kind": "enum_violation",
                        "row_index": row_idx,
                        "column": col,
                        "value": row[col],
                        "allowed": list(allowed),
                    })
                    if len(violations) >= MAX_VIOLATIONS_REPORTED:
                        break
            if len(violations) >= MAX_VIOLATIONS_REPORTED:
                break

        if violations:
            raise SchemaCheckError(violations=violations)
        return records

    @staticmethod
    def output_schema(
        config: SchemaValidateConfig, *upstream_schemas: set[str]
    ) -> set[str]:
        """Passthrough — input schema = output schema. The expected
        columns can be added even if upstream omits them at static
        time (defensive)."""
        upstream = upstream_schemas[0] if upstream_schemas else set()
        return upstream | set(config.expected_columns)


register_typed_node(
    SchemaValidateExecutor,
    summary="Validate tabular_records against expected schema (passthrough on success)",
    when_to_use=(
        "Use as a guard step right after read_csv when the input columns "
        "are known but uncertain — schema_validate fails fast on missing "
        "columns / type drift before downstream steps see bad data."
    ),
)
