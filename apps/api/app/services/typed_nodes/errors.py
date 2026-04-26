"""Structured errors for typed-node executors.

Every typed-node failure is one of these. They carry enough fields
that the planner / build / run audit surfaces can format actionable
feedback without re-parsing free-form text.

Hierarchy:

    TypedNodeError
    ├── ConfigError              # config loading / validation
    │   ├── SchemaValidationFailure
    │   └── StaticAnalysisFailure
    └── ExecutionError           # runtime, per-node-specific subtypes
        ├── ReadError / WriteError                   # file IO
        ├── MergeError / FilterError / ProjectError  # shape ops
        ├── FormulaEvalError / ClassifyEvalError     # compute
        ├── GroupByError                              # aggregate
        ├── SchemaCheckError                          # schema_validate
        ├── FormatConvertError                        # format_convert
        └── EmptyAfterFilterError                     # filter w/ require_non_empty
"""
from __future__ import annotations

from typing import Any, Optional


class TypedNodeError(Exception):
    """Base class for every typed-node failure mode."""

    def __init__(
        self,
        message: str,
        *,
        node_type: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.node_type = node_type
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_class": self.__class__.__name__,
            "node_type": self.node_type,
            "message": str(self),
            "details": dict(self.details),
        }


# ---------------------------------------------------------------------------
# Config-time errors (raised at planner / build, not at run)
# ---------------------------------------------------------------------------


class ConfigError(TypedNodeError):
    """Base for errors raised before any data is processed."""


class SchemaValidationFailure(ConfigError):
    """The Pydantic config failed model validation."""

    def __init__(self, *, node_type: str, pydantic_errors: list[dict]) -> None:
        super().__init__(
            f"typed_config for node_type {node_type!r} failed schema validation",
            node_type=node_type,
            details={"pydantic_errors": pydantic_errors},
        )
        self.pydantic_errors = pydantic_errors


class StaticAnalysisFailure(ConfigError):
    """An expression / referenced column does not match the upstream
    schema available at this node's input."""

    def __init__(
        self,
        *,
        node_type: str,
        location: str,
        cause: str,
        available_columns: Optional[list[str]] = None,
    ) -> None:
        super().__init__(
            f"static analysis failed at {location}: {cause}",
            node_type=node_type,
            details={
                "location": location,
                "cause": cause,
                "available_columns": list(available_columns or []),
            },
        )
        self.location = location
        self.cause = cause
        self.available_columns = list(available_columns or [])


# ---------------------------------------------------------------------------
# Runtime errors
# ---------------------------------------------------------------------------


class ExecutionError(TypedNodeError):
    """Base for errors raised during node execution against real data."""


class ReadError(ExecutionError):
    """File read failed — missing file, parse error, dtype coercion."""


class WriteError(ExecutionError):
    """File write failed — path unwritable, missing required column."""


class MergeError(ExecutionError):
    """Merge / join failed (key missing on a side, m:n explosion when
    1:1 was required, NULL after join when forbidden)."""


class FilterError(ExecutionError):
    """Filter predicate failure — runtime evaluation error."""


class EmptyAfterFilterError(ExecutionError):
    """Filter dropped every row but ``require_non_empty=True``."""


class ProjectError(ExecutionError):
    """Projection failed — column missing without ``fill_missing_with``."""


class FormulaEvalError(ExecutionError):
    """Formula evaluation failed for a specific assignment + row.

    Carries enough context to point at exactly which assignment +
    which row triggered the failure.
    """

    def __init__(
        self,
        *,
        assignment_index: int,
        target: str,
        expression: str,
        row_index: int,
        row_inputs_subset: dict[str, Any],
        cause: str,
    ) -> None:
        super().__init__(
            f"formula assignment[{assignment_index}] target={target!r} "
            f"failed at row {row_index}: {cause}",
            node_type="formula_eval",
            details={
                "assignment_index": assignment_index,
                "target": target,
                "expression": expression,
                "row_index": row_index,
                "row_inputs_subset": row_inputs_subset,
                "cause": cause,
            },
        )
        self.assignment_index = assignment_index
        self.target = target
        self.expression = expression
        self.row_index = row_index
        self.row_inputs_subset = row_inputs_subset
        self.cause = cause


class NegativeValueError(ExecutionError):
    """formula_eval validate_non_negative tripped."""

    def __init__(self, *, column: str, row_index: int, value: Any) -> None:
        super().__init__(
            f"column {column!r} got negative value {value!r} at row {row_index}",
            node_type="formula_eval",
            details={"column": column, "row_index": row_index, "value": value},
        )
        self.column = column
        self.row_index = row_index
        self.value = value


class ClassifyEvalError(ExecutionError):
    """A decision_list rule's predicate failed at runtime."""

    def __init__(
        self,
        *,
        rule_index: int,
        when: str,
        row_index: int,
        cause: str,
    ) -> None:
        super().__init__(
            f"classify rule[{rule_index}] when={when!r} failed at row "
            f"{row_index}: {cause}",
            node_type="classify_state_space",
            details={
                "rule_index": rule_index,
                "when": when,
                "row_index": row_index,
                "cause": cause,
            },
        )
        self.rule_index = rule_index
        self.when = when
        self.row_index = row_index
        self.cause = cause


class NoRuleMatchedError(ExecutionError):
    """``require_match=True`` and a row didn't hit any rule."""

    def __init__(self, *, row_index: int, target_field: str) -> None:
        super().__init__(
            f"no rule matched row {row_index} for target {target_field!r} "
            f"and require_match=True",
            node_type="classify_state_space",
            details={"row_index": row_index, "target_field": target_field},
        )
        self.row_index = row_index
        self.target_field = target_field


class GroupByError(ExecutionError):
    """groupby_aggregate failure — missing key / non-numeric for arithmetic."""


class SchemaCheckError(ExecutionError):
    """schema_validate found a violation."""

    def __init__(
        self,
        *,
        violations: list[dict[str, Any]],
    ) -> None:
        super().__init__(
            f"schema validation failed: {len(violations)} violation(s)",
            node_type="schema_validate",
            details={"violations": list(violations)},
        )
        self.violations = list(violations)


class FormatConvertError(ExecutionError):
    """format_convert pair / read / write failure."""


__all__ = [
    "TypedNodeError",
    "ConfigError",
    "SchemaValidationFailure",
    "StaticAnalysisFailure",
    "ExecutionError",
    "ReadError",
    "WriteError",
    "MergeError",
    "FilterError",
    "EmptyAfterFilterError",
    "ProjectError",
    "FormulaEvalError",
    "NegativeValueError",
    "ClassifyEvalError",
    "NoRuleMatchedError",
    "GroupByError",
    "SchemaCheckError",
    "FormatConvertError",
]
