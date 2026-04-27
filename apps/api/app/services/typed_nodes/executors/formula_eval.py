"""``formula_eval`` executor — declarative column derivation.

The biggest typed-node carrier of business logic. For each row in
the upstream tabular_records, evaluate ``config.assignments`` in
order; each assignment writes a new column. Subsequent assignments
can reference targets defined earlier in the same execute call —
this is what gives the node its "spreadsheet-style" semantics.

Three orthogonal axes of behavior:

    * ``null_propagation`` (per-assignment): how to handle ``None``
      in any referenced column.
        - ``sql``   — result is ``None``
        - ``zero``  — substitute 0 for ``None``
        - ``raise`` — fail the row immediately

    * ``on_error`` (per-assignment): what to do when evaluation
      itself fails (typo, type mismatch, division by zero, etc.).
        - ``raise``        — abort the whole node (default)
        - ``fill_null``    — write None and continue
        - ``fill_default`` — write ``default`` and continue

    * ``validate_non_negative`` (post-pass): any column listed here
      must be ≥ 0 across every row at the end. Triggers
      ``NegativeValueError``. Useful for ``recommended_qty``,
      ``estimated_cost``, etc.

Defense in depth:
    Each assignment's expression is parsed once at the top of
    ``execute`` so unsafe expressions (lambdas, attribute access)
    fail before any data flows through. Per-row failures carry the
    row index, the target column, and the actual values of every
    referenced column — so when 1 row of 10k fails, you don't have
    to re-derive what the inputs were.
"""
from __future__ import annotations

from typing import Any, ClassVar

from app.services.typed_nodes.base import TypedNodeExecutor
from app.services.typed_nodes.configs import FormulaEvalConfig
from app.services.typed_nodes.errors import (
    FormulaEvalError,
    NegativeValueError,
)
from app.services.typed_nodes.expr_engine import (
    EvaluationError,
    ExprError,
    NullInputError,
    SafeExprEngine,
    UnknownNameError,
    UnsafeExpressionError,
)
from app.services.typed_nodes.registry import register_typed_node


def _try_numerify_value(v: Any) -> Any:
    """Coerce string-encoded numbers to int / float; leave anything
    else alone. Booleans must be left as bools (bool is an int
    subclass — accidental coercion would break filters that test
    ``isinstance(x, bool)`` later)."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return v
        # Prefer int when possible (no decimal / exponent / sign-only)
        try:
            if "." not in s and "e" not in s.lower():
                return int(s)
            return float(s)
        except ValueError:
            return v
    return v


def _try_numerify_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: _try_numerify_value(v) for k, v in row.items()}


class FormulaEvalExecutor(TypedNodeExecutor):
    node_type: ClassVar[str] = "formula_eval"
    config_class: ClassVar[type] = FormulaEvalConfig

    def execute(
        self,
        config: FormulaEvalConfig,
        inputs: list[Any],
    ) -> list[dict[str, Any]]:
        if len(inputs) != 1:
            raise FormulaEvalError(
                assignment_index=-1,
                target="",
                expression="",
                row_index=-1,
                row_inputs_subset={},
                cause=f"formula_eval expects exactly 1 input, got {len(inputs)}",
            )
        records = inputs[0]
        if not isinstance(records, list):
            raise FormulaEvalError(
                assignment_index=-1,
                target="",
                expression="",
                row_index=-1,
                row_inputs_subset={},
                cause=f"input must be list[dict], got {type(records).__name__}",
            )

        engine = SafeExprEngine()

        # Pre-flight: parse every expression once. Unsafe expressions
        # fail before we touch any data.
        parsed_refs: list[set[str]] = []
        for idx, asn in enumerate(config.assignments):
            try:
                refs = engine.referenced_names(asn.expression)
            except UnsafeExpressionError as exc:
                raise FormulaEvalError(
                    assignment_index=idx,
                    target=asn.target,
                    expression=asn.expression,
                    row_index=-1,
                    row_inputs_subset={},
                    cause=f"unsafe expression: {exc}",
                ) from exc
            parsed_refs.append(refs)

        out: list[dict[str, Any]] = []
        for row_idx, row in enumerate(records):
            if not isinstance(row, dict):
                raise FormulaEvalError(
                    assignment_index=-1,
                    target="",
                    expression="",
                    row_index=row_idx,
                    row_inputs_subset={},
                    cause=f"row {row_idx} is not a dict: {type(row).__name__}",
                )

            new_row = dict(row)  # don't mutate caller's records
            if config.coerce_string_to_number:
                new_row = _try_numerify_row(new_row)

            for asn_idx, asn in enumerate(config.assignments):
                try:
                    value = engine.eval_row(
                        asn.expression,
                        new_row,
                        null_propagation=asn.null_propagation,
                    )
                except (UnknownNameError, EvaluationError, NullInputError,
                        UnsafeExpressionError, ExprError) as exc:
                    if asn.on_error == "raise":
                        # Build row_inputs_subset: only the columns
                        # this expression actually references, with
                        # their actual values.
                        from app.services.typed_nodes.expr_engine import (
                            DOMAIN_FUNCTIONS,
                        )
                        ref_cols = parsed_refs[asn_idx] - set(DOMAIN_FUNCTIONS.keys())
                        subset = {c: new_row.get(c) for c in ref_cols}
                        raise FormulaEvalError(
                            assignment_index=asn_idx,
                            target=asn.target,
                            expression=asn.expression,
                            row_index=row_idx,
                            row_inputs_subset=subset,
                            cause=str(exc),
                        ) from exc
                    elif asn.on_error == "fill_default":
                        value = asn.default
                    elif asn.on_error == "fill_null":
                        value = None
                    else:
                        # Pydantic Literal would already have rejected
                        # this, but be defensive.
                        raise

                new_row[asn.target] = value

            out.append(new_row)

        # Post-pass validation: validate_non_negative
        for col in config.validate_non_negative:
            for row_idx, row in enumerate(out):
                v = row.get(col)
                if v is None:
                    continue
                # bool is a subclass of int — exclude
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)) and v < 0:
                    raise NegativeValueError(
                        column=col,
                        row_index=row_idx,
                        value=v,
                    )

        return out

    @staticmethod
    def output_schema(
        config: FormulaEvalConfig, *upstream_schemas: set[str]
    ) -> set[str]:
        """Output schema = upstream columns ∪ all assignment targets.
        formula_eval never drops columns; it only adds (or overwrites,
        though the FormulaEvalConfig validator forbids overwriting
        upstream by checking for unique targets)."""
        upstream = upstream_schemas[0] if upstream_schemas else set()
        targets = {asn.target for asn in config.assignments}
        return upstream | targets


register_typed_node(
    FormulaEvalExecutor,
    summary="Derive new columns from upstream rows via SafeExprEngine expressions",
    when_to_use=(
        "When the requirement says 'compute / calculate X from Y', "
        "'derive Z = A * B', or lists per-row metrics (available_qty, "
        "coverage_days, reorder_point, gap_qty, recommended_qty). One "
        "formula_eval can emit multiple new columns in a single step."
    ),
)
