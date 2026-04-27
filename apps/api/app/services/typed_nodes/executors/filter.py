"""``filter`` executor — predicate-driven row filtering.

Uses SafeExprEngine to evaluate a single predicate per row.
``keep_or_drop`` controls polarity.

Null semantics: SafeExprEngine with ``null_propagation="sql"`` returns
None when any referenced column is None; ``bool(None)`` is False so
that row is dropped (in keep mode) or kept (in drop mode). This
matches SQL WHERE-clause semantics.

Predicate references that miss the row's actual schema (e.g. a typo)
raise FilterError carrying the row index + the offending column. We
do per-row evaluation rather than a one-shot static check because
upstream might emit heterogeneous row shapes — the per-row failure
is more precise.
"""
from __future__ import annotations

from typing import Any, ClassVar

from app.services.typed_nodes.base import TypedNodeExecutor
from app.services.typed_nodes.configs import FilterConfig
from app.services.typed_nodes.errors import EmptyAfterFilterError, FilterError
from app.services.typed_nodes.expr_engine import (
    EvaluationError,
    ExprError,
    SafeExprEngine,
    UnknownNameError,
    UnsafeExpressionError,
)
from app.services.typed_nodes.registry import register_typed_node


class FilterExecutor(TypedNodeExecutor):
    node_type: ClassVar[str] = "filter"
    config_class: ClassVar[type] = FilterConfig

    def execute(
        self,
        config: FilterConfig,
        inputs: list[Any],
    ) -> list[dict[str, Any]]:
        if len(inputs) != 1:
            raise FilterError(
                f"filter expects exactly 1 input, got {len(inputs)}",
                node_type="filter",
            )
        records = inputs[0]
        if not isinstance(records, list):
            raise FilterError(
                f"filter input must be list[dict], got {type(records).__name__}",
                node_type="filter",
            )

        engine = SafeExprEngine()

        # Sanity-parse the predicate once before the row loop. We get
        # to fail with UnsafeExpressionError before any data flows
        # through, instead of repeating the parse error 10k times.
        try:
            engine.referenced_names(config.predicate)
        except UnsafeExpressionError as exc:
            raise FilterError(
                f"predicate is unsafe / unparseable: {exc}",
                node_type="filter",
                details={"predicate": config.predicate, "cause": str(exc)},
            ) from exc

        out: list[dict[str, Any]] = []
        for idx, row in enumerate(records):
            if not isinstance(row, dict):
                raise FilterError(
                    f"row {idx} is not a dict: {type(row).__name__}",
                    node_type="filter",
                    details={"row_index": idx},
                )
            try:
                result = engine.eval_row(
                    config.predicate, row, null_propagation="sql"
                )
            except UnknownNameError as exc:
                raise FilterError(
                    f"predicate references column {exc.name!r} "
                    f"missing from row {idx}",
                    node_type="filter",
                    details={
                        "row_index": idx,
                        "missing_column": exc.name,
                        "predicate": config.predicate,
                        "available_columns": exc.available,
                    },
                ) from exc
            except EvaluationError as exc:
                raise FilterError(
                    f"predicate evaluation failed at row {idx}: {exc.cause}",
                    node_type="filter",
                    details={
                        "row_index": idx,
                        "predicate": config.predicate,
                        "cause": exc.cause,
                    },
                ) from exc
            except ExprError as exc:
                # Catch-all for any other ExprError subclass
                raise FilterError(
                    f"predicate failed at row {idx}: {exc}",
                    node_type="filter",
                    details={
                        "row_index": idx,
                        "predicate": config.predicate,
                        "cause": str(exc),
                    },
                ) from exc

            keep = bool(result)
            if config.keep_or_drop == "drop":
                keep = not keep
            if keep:
                out.append(row)

        if config.require_non_empty and not out:
            raise EmptyAfterFilterError(
                f"filter dropped all rows; require_non_empty=True",
                node_type="filter",
                details={
                    "predicate": config.predicate,
                    "keep_or_drop": config.keep_or_drop,
                    "input_row_count": len(records),
                },
            )

        return out

    @staticmethod
    def output_schema(
        config: FilterConfig, *upstream_schemas: set[str]
    ) -> set[str]:
        """Filter doesn't change the column set — passthrough."""
        return set(upstream_schemas[0]) if upstream_schemas else set()


register_typed_node(
    FilterExecutor,
    summary="Filter rows by a SafeExprEngine predicate",
    when_to_use=(
        "When the requirement says 'only include rows where X', 'exclude "
        "rows where Y', or 'final queue contains only SKUs requiring "
        "action'. Predicate is a boolean expression over upstream columns."
    ),
)
