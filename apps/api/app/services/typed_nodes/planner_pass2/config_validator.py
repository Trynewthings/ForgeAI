"""Config-level validator for typed-node planner Pass 2.

The service already validates every synthesized config through its
Pydantic model. This module adds the next static layers:

* column references must be available from upstream schema flow
* light artifact/config consistency for file source and sink nodes
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import app.services.typed_nodes.executors  # noqa: F401  register executors
from app.services.typed_nodes.configs import (
    ClassifyStateSpaceConfig,
    FilterConfig,
    FormatConvertConfig,
    FormulaEvalConfig,
    GroupByAggregateConfig,
    MergeConfig,
    ProjectColumn,
    ProjectConfig,
    ReadCsvConfig,
    SchemaValidateConfig,
    WriteCsvConfig,
)
from app.services.typed_nodes.expr_engine import (
    ExprError,
    SafeExprEngine,
    UnknownNameError,
)
from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    TypedNodeStepDraft,
)
from app.services.typed_nodes.registry import get_executor_class


@dataclass(frozen=True)
class ValidationIssue:
    """One Pass 2 config validation issue."""

    code: str
    severity: Literal["error", "warning"]
    message: str
    step_id: str
    node_type: str
    location: str
    details: dict[str, Any] = field(default_factory=dict)


def validate_step_config(
    step: TypedNodeStepDraft,
    config: Any,
    *,
    upstream_columns: list[set[str]],
    input_artifacts: list[PlannerInputArtifact],
    output_artifacts: list[PlannerOutputArtifact],
) -> list[ValidationIssue]:
    """Validate one synthesized config against Pass 2 static context.

    ``config`` may be a raw dict or an already validated Pydantic config.
    The function returns issues rather than raising so the service can
    decide whether to retry, block, or downgrade in later phases.
    """
    if step.node_type == "coding_agent":
        return []
    try:
        validated = get_executor_class(step.node_type).validate_config(config)
    except Exception as exc:  # noqa: BLE001
        return [_issue(
            step,
            "pydantic_validation_failed",
            "error",
            f"typed_config failed Pydantic validation: {exc}",
            "config",
            {"cause": str(exc)},
        )]

    issues: list[ValidationIssue] = []
    issues.extend(_validate_arity(step, upstream_columns))
    if issues:
        return issues

    node_type = step.node_type
    if node_type == "read_csv":
        issues.extend(_validate_read_csv(step, validated, input_artifacts))
    elif node_type == "write_csv":
        issues.extend(_validate_write_csv(step, validated, upstream_columns, output_artifacts))
    elif node_type == "schema_validate":
        issues.extend(_validate_schema_validate(step, validated, upstream_columns))
    elif node_type == "merge":
        issues.extend(_validate_merge(step, validated, upstream_columns))
    elif node_type == "filter":
        issues.extend(_validate_filter(step, validated, upstream_columns))
    elif node_type == "project":
        issues.extend(_validate_project(step, validated, upstream_columns))
    elif node_type == "formula_eval":
        issues.extend(_validate_formula_eval(step, validated, upstream_columns))
    elif node_type == "classify_state_space":
        issues.extend(_validate_classify(step, validated, upstream_columns))
    elif node_type == "groupby_aggregate":
        issues.extend(_validate_groupby(step, validated, upstream_columns))
    elif node_type == "format_convert":
        issues.extend(_validate_format_convert(step, validated, input_artifacts, output_artifacts))
    return issues


def has_blocking_issues(issues: list[ValidationIssue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


def render_validation_issues(issues: list[ValidationIssue]) -> str:
    return "; ".join(
        f"{i.step_id}:{i.location}:{i.code}: {i.message}" for i in issues
    )


def _issue(
    step: TypedNodeStepDraft,
    code: str,
    severity: Literal["error", "warning"],
    message: str,
    location: str,
    details: dict[str, Any] | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        code=code,
        severity=severity,
        message=message,
        step_id=step.step_id,
        node_type=step.node_type,
        location=location,
        details=dict(details or {}),
    )


def _union(upstream_columns: list[set[str]]) -> set[str]:
    out: set[str] = set()
    for cols in upstream_columns:
        out.update(cols)
    return out


def _as_list(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _missing(
    step: TypedNodeStepDraft,
    *,
    location: str,
    referenced: list[str],
    available: set[str],
) -> list[ValidationIssue]:
    missing = sorted(set(referenced) - set(available))
    if not missing:
        return []
    return [_issue(
        step,
        "unknown_column_reference",
        "error",
        f"references missing column(s): {', '.join(missing)}",
        location,
        {
            "missing_columns": missing,
            "available_columns": sorted(available),
            "referenced_columns": sorted(set(referenced)),
        },
    )]


def _expr_issues(
    step: TypedNodeStepDraft,
    *,
    expression: str,
    available: set[str],
    location: str,
) -> list[ValidationIssue]:
    engine = SafeExprEngine()
    try:
        engine.static_check(expression, available=available)
    except UnknownNameError as exc:
        return [_issue(
            step,
            "unknown_column_reference",
            "error",
            f"expression references missing column {exc.name!r}",
            location,
            {
                "expression": expression,
                "missing_column": exc.name,
                "available_columns": sorted(available),
            },
        )]
    except ExprError as exc:
        return [_issue(
            step,
            "unsafe_expression",
            "error",
            f"expression failed static analysis: {exc}",
            location,
            {"expression": expression, "cause": str(exc)},
        )]
    return []


def _validate_arity(
    step: TypedNodeStepDraft,
    upstream_columns: list[set[str]],
) -> list[ValidationIssue]:
    expected: int | None
    if step.node_type in {"read_csv", "format_convert"}:
        expected = 0
    elif step.node_type == "merge":
        expected = 2
    else:
        expected = 1
    if len(upstream_columns) == expected:
        return []
    return [_issue(
        step,
        "upstream_arity_mismatch",
        "error",
        f"{step.node_type} expects {expected} upstream input(s), got {len(upstream_columns)}",
        "upstream_step_ids",
        {"expected": expected, "actual": len(upstream_columns)},
    )]


def _find_input(
    artifacts: list[PlannerInputArtifact],
    artifact_id: str | None,
) -> PlannerInputArtifact | None:
    return next((a for a in artifacts if a.artifact_id == artifact_id), None)


def _find_output(
    artifacts: list[PlannerOutputArtifact],
    artifact_id: str | None,
) -> PlannerOutputArtifact | None:
    return next((a for a in artifacts if a.artifact_id == artifact_id), None)


def _validate_read_csv(
    step: TypedNodeStepDraft,
    config: ReadCsvConfig,
    input_artifacts: list[PlannerInputArtifact],
) -> list[ValidationIssue]:
    artifact_id = step.consumes_artifact_ids[0] if step.consumes_artifact_ids else None
    artifact = _find_input(input_artifacts, artifact_id)
    if artifact is None:
        return [_issue(step, "artifact_binding_missing", "error", "read_csv must consume a declared input artifact", "consumes_artifact_ids")]
    issues: list[ValidationIssue] = []
    if config.path != artifact.path:
        issues.append(_issue(step, "artifact_path_mismatch", "error", "read_csv.path does not match consumed artifact path", "config.path", {"expected": artifact.path, "actual": config.path}))
    expected_delim = "\t" if artifact.format == "tsv" else ","
    if artifact.format in {"csv", "tsv"} and config.delimiter != expected_delim:
        issues.append(_issue(step, "artifact_format_mismatch", "warning", "read_csv delimiter does not match artifact format", "config.delimiter", {"format": artifact.format, "expected": expected_delim, "actual": config.delimiter}))
    if artifact.declared_columns:
        issues.extend(_missing(step, location="config.dtype_overrides", referenced=list(config.dtype_overrides.keys()), available=set(artifact.declared_columns)))
    return issues


def _validate_write_csv(
    step: TypedNodeStepDraft,
    config: WriteCsvConfig,
    upstream_columns: list[set[str]],
    output_artifacts: list[PlannerOutputArtifact],
) -> list[ValidationIssue]:
    available = _union(upstream_columns)
    artifact_id = step.produces_artifact_ids[0] if step.produces_artifact_ids else None
    artifact = _find_output(output_artifacts, artifact_id)
    issues = _missing(step, location="config.columns_in_order", referenced=config.columns_in_order, available=available)
    if artifact is None:
        issues.append(_issue(step, "artifact_binding_missing", "error", "write_csv must produce a declared output artifact", "produces_artifact_ids"))
        return issues
    if config.path != artifact.path:
        issues.append(_issue(step, "artifact_path_mismatch", "error", "write_csv.path does not match produced artifact path", "config.path", {"expected": artifact.path, "actual": config.path}))
    if artifact.required_columns and config.columns_in_order != artifact.required_columns:
        issues.append(_issue(step, "artifact_columns_mismatch", "warning", "write_csv columns_in_order differs from output artifact required_columns", "config.columns_in_order", {"expected": artifact.required_columns, "actual": config.columns_in_order}))
    return issues


def _validate_schema_validate(
    step: TypedNodeStepDraft,
    config: SchemaValidateConfig,
    upstream_columns: list[set[str]],
) -> list[ValidationIssue]:
    available = _union(upstream_columns)
    refs = list(config.expected_columns)
    refs.extend(config.required_non_null)
    refs.extend(config.value_ranges.keys())
    refs.extend(config.enum_constraints.keys())
    return _missing(step, location="config", referenced=refs, available=available)


def _validate_merge(
    step: TypedNodeStepDraft,
    config: MergeConfig,
    upstream_columns: list[set[str]],
) -> list[ValidationIssue]:
    left, right = upstream_columns
    issues: list[ValidationIssue] = []
    if config.on is not None:
        keys = _as_list(config.on)
        issues.extend(_missing(step, location="config.on[left]", referenced=keys, available=left))
        issues.extend(_missing(step, location="config.on[right]", referenced=keys, available=right))
    else:
        issues.extend(_missing(step, location="config.left_on", referenced=_as_list(config.left_on), available=left))
        issues.extend(_missing(step, location="config.right_on", referenced=_as_list(config.right_on), available=right))
    if config.require_no_nulls_after:
        output_cols = get_executor_class("merge").output_schema(config, left, right)
        issues.extend(_missing(step, location="config.require_no_nulls_after", referenced=config.require_no_nulls_after, available=output_cols))
    return issues


def _validate_filter(
    step: TypedNodeStepDraft,
    config: FilterConfig,
    upstream_columns: list[set[str]],
) -> list[ValidationIssue]:
    return _expr_issues(step, expression=config.predicate, available=_union(upstream_columns), location="config.predicate")


def _validate_project(
    step: TypedNodeStepDraft,
    config: ProjectConfig,
    upstream_columns: list[set[str]],
) -> list[ValidationIssue]:
    refs = [
        entry.source if isinstance(entry, ProjectColumn) else entry
        for entry in config.columns
    ]
    return _missing(step, location="config.columns", referenced=refs, available=_union(upstream_columns))


def _validate_formula_eval(
    step: TypedNodeStepDraft,
    config: FormulaEvalConfig,
    upstream_columns: list[set[str]],
) -> list[ValidationIssue]:
    available = _union(upstream_columns)
    issues: list[ValidationIssue] = []
    for idx, assignment in enumerate(config.assignments):
        if assignment.target in available:
            issues.append(_issue(step, "target_overwrites_upstream", "error", f"assignment target {assignment.target!r} overwrites an upstream column", f"config.assignments[{idx}].target", {"target": assignment.target}))
        issues.extend(_expr_issues(step, expression=assignment.expression, available=available, location=f"config.assignments[{idx}].expression"))
        available.add(assignment.target)
    issues.extend(_missing(step, location="config.validate_non_negative", referenced=config.validate_non_negative, available=available))
    return issues


def _validate_classify(
    step: TypedNodeStepDraft,
    config: ClassifyStateSpaceConfig,
    upstream_columns: list[set[str]],
) -> list[ValidationIssue]:
    available = _union(upstream_columns)
    issues: list[ValidationIssue] = []
    if config.target_field in available:
        issues.append(_issue(step, "target_overwrites_upstream", "error", f"target_field {config.target_field!r} overwrites an upstream column", "config.target_field", {"target": config.target_field}))
    if config.evidence_field and config.evidence_field in available:
        issues.append(_issue(step, "target_overwrites_upstream", "error", f"evidence_field {config.evidence_field!r} overwrites an upstream column", "config.evidence_field", {"target": config.evidence_field}))
    for idx, rule in enumerate(config.decision_list):
        issues.extend(_expr_issues(step, expression=rule.when, available=available, location=f"config.decision_list[{idx}].when"))
    return issues


def _validate_groupby(
    step: TypedNodeStepDraft,
    config: GroupByAggregateConfig,
    upstream_columns: list[set[str]],
) -> list[ValidationIssue]:
    refs = list(config.group_keys)
    refs.extend(op.source for op in config.aggregations)
    return _missing(step, location="config.groupby_aggregate", referenced=refs, available=_union(upstream_columns))


def _validate_format_convert(
    step: TypedNodeStepDraft,
    config: FormatConvertConfig,
    input_artifacts: list[PlannerInputArtifact],
    output_artifacts: list[PlannerOutputArtifact],
) -> list[ValidationIssue]:
    in_id = step.consumes_artifact_ids[0] if step.consumes_artifact_ids else None
    out_id = step.produces_artifact_ids[0] if step.produces_artifact_ids else None
    in_art = _find_input(input_artifacts, in_id)
    out_art = _find_output(output_artifacts, out_id)
    issues: list[ValidationIssue] = []
    if in_art is None:
        issues.append(_issue(step, "artifact_binding_missing", "error", "format_convert must consume a declared input artifact", "consumes_artifact_ids"))
    else:
        if config.in_path != in_art.path:
            issues.append(_issue(step, "artifact_path_mismatch", "error", "format_convert.in_path does not match input artifact path", "config.in_path", {"expected": in_art.path, "actual": config.in_path}))
        if config.from_format != in_art.format:
            issues.append(_issue(step, "artifact_format_mismatch", "error", "format_convert.from_format does not match input artifact format", "config.from_format", {"expected": in_art.format, "actual": config.from_format}))
    if out_art is None:
        issues.append(_issue(step, "artifact_binding_missing", "error", "format_convert must produce a declared output artifact", "produces_artifact_ids"))
    else:
        if config.out_path != out_art.path:
            issues.append(_issue(step, "artifact_path_mismatch", "error", "format_convert.out_path does not match output artifact path", "config.out_path", {"expected": out_art.path, "actual": config.out_path}))
        if config.to_format != out_art.format:
            issues.append(_issue(step, "artifact_format_mismatch", "error", "format_convert.to_format does not match output artifact format", "config.to_format", {"expected": out_art.format, "actual": config.to_format}))
        if out_art.required_columns and config.csv_columns_in_order and config.csv_columns_in_order != out_art.required_columns:
            issues.append(_issue(step, "artifact_columns_mismatch", "warning", "format_convert csv_columns_in_order differs from output artifact required_columns", "config.csv_columns_in_order", {"expected": out_art.required_columns, "actual": config.csv_columns_in_order}))
    return issues


__all__ = [
    "ValidationIssue",
    "has_blocking_issues",
    "render_validation_issues",
    "validate_step_config",
]
