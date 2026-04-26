"""Deterministic per-node-type config factory for Pass 2 mock mode.

Each function in here takes the per-step context (the Pass 1 step,
upstream column sets, relevant artifacts, requirement text) and
returns a dict that's a VALID input to the corresponding Pydantic
Config. "Valid" means it round-trips through
``executor.config_class.model_validate(dict)`` without raising.

Why deterministic mocks matter:
    8.0.12 ships before the real LLM prompt builder (8.0.13). We need
    end-to-end testability of the topology + schema-flow tracker
    NOW, so the rest of Pass 2 can iterate against a known-good
    config source. The mock factory is also a useful baseline for
    future shadow comparisons of LLM-synthesized configs.

Design notes:
    * The factory is intentionally NOT trying to produce smart configs
      — it produces SAFE configs. A read_csv that declares every
      column as ``str``, a filter with predicate "True", a project
      that passes through every upstream column. Smart configs are
      Pass 2's real (LLM) job.
    * For nodes whose Pydantic validators are very strict
      (ClassifyStateSpaceConfig requiring then ∈ allowed_values),
      we generate a single trivial rule that satisfies the cross-
      field invariants.
    * coding_agent has no Pydantic Config — it gets a stub config
      payload that just records the escape_hatch_reason.
"""
from __future__ import annotations

from typing import Any

from app.services.typed_nodes.planner_pass1.schema import (
    PlannerInputArtifact,
    PlannerOutputArtifact,
    TypedNodeStepDraft,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_input_artifact(
    artifacts: list[PlannerInputArtifact], artifact_id: str
) -> PlannerInputArtifact | None:
    for a in artifacts:
        if a.artifact_id == artifact_id:
            return a
    return None


def _find_output_artifact(
    artifacts: list[PlannerOutputArtifact], artifact_id: str
) -> PlannerOutputArtifact | None:
    for a in artifacts:
        if a.artifact_id == artifact_id:
            return a
    return None


def _flatten_upstream(upstream_columns: list[set[str]]) -> list[str]:
    """Sorted union of upstream column sets — handy for projecting,
    schema_validate'ing, etc."""
    out: set[str] = set()
    for cols in upstream_columns:
        out.update(cols)
    return sorted(out)


def _shared_columns(upstream_columns: list[set[str]]) -> list[str]:
    """Sorted intersection across all upstreams. Used for merge keys."""
    if not upstream_columns:
        return []
    shared = set(upstream_columns[0])
    for cols in upstream_columns[1:]:
        shared &= cols
    return sorted(shared)


# ---------------------------------------------------------------------------
# Per-node-type factories
# ---------------------------------------------------------------------------


def _config_for_read_csv(
    step: TypedNodeStepDraft,
    *,
    input_artifacts: list[PlannerInputArtifact],
    **_: Any,
) -> dict[str, Any]:
    """Use the step's first consumed artifact as the source of path /
    columns. Falls back to a placeholder when the artifact isn't
    declared (the validator would catch it; we still produce a
    type-valid dict so downstream synthesis can continue).
    """
    artifact_id = step.consumes_artifact_ids[0] if step.consumes_artifact_ids else None
    art = _find_input_artifact(input_artifacts, artifact_id) if artifact_id else None
    path = art.path if art else f"<missing:{artifact_id or 'no_artifact'}>"
    delimiter = "\t" if (art and art.format == "tsv") else ","

    declared = art.declared_columns if art else []
    if declared:
        # All columns declared as str — safe but lossy. Real LLM
        # synthesis (8.0.13) will pick smarter dtypes from the
        # requirement text.
        dtype_overrides = {col: "str" for col in declared}
    else:
        # No declared columns → empty dtype_overrides means downstream
        # static-check has nothing to verify against. Mark the step's
        # synthesis_notes so the audit log carries the warning.
        dtype_overrides = {}

    return {
        "path": path,
        "delimiter": delimiter,
        "dtype_overrides": dtype_overrides,
    }


def _config_for_write_csv(
    step: TypedNodeStepDraft,
    *,
    output_artifacts: list[PlannerOutputArtifact],
    upstream_columns: list[set[str]],
    **_: Any,
) -> dict[str, Any]:
    artifact_id = step.produces_artifact_ids[0] if step.produces_artifact_ids else None
    art = _find_output_artifact(output_artifacts, artifact_id) if artifact_id else None
    path = art.path if art else f"<missing:{artifact_id or 'no_artifact'}>"

    if art and art.required_columns:
        columns_in_order = list(art.required_columns)
    else:
        # Fall back to the upstream's union; ensure non-empty since
        # WriteCsvConfig.columns_in_order has min_length=1. If even
        # upstream is empty, ship a placeholder column — the validator
        # in 8.0.14 will surface this.
        cols = _flatten_upstream(upstream_columns)
        columns_in_order = cols or ["__placeholder__"]

    return {
        "path": path,
        "columns_in_order": columns_in_order,
    }


def _config_for_schema_validate(
    step: TypedNodeStepDraft,
    *,
    upstream_columns: list[set[str]],
    **_: Any,
) -> dict[str, Any]:
    cols = _flatten_upstream(upstream_columns)
    if not cols:
        cols = ["__placeholder__"]
    return {
        "expected_columns": cols,
    }


def _config_for_merge(
    step: TypedNodeStepDraft,
    *,
    upstream_columns: list[set[str]],
    **_: Any,
) -> dict[str, Any]:
    """Pick the first column shared across both upstreams as the key.

    When no shared column exists (or only one upstream is registered),
    fall back to a placeholder ``id``; the validator will surface
    this and the LLM (8.0.13) will get a chance to fix it.
    """
    shared = _shared_columns(upstream_columns)
    join_key = shared[0] if shared else "id"
    return {
        "on": join_key,
        "how": "left",
    }


def _config_for_filter(
    step: TypedNodeStepDraft,
    **_: Any,
) -> dict[str, Any]:
    """Trivial truthy predicate. The LLM stage replaces this with
    something meaningful; in mock mode we just want a config that
    parses."""
    return {
        "predicate": "True",
        "keep_or_drop": "keep",
    }


def _config_for_project(
    step: TypedNodeStepDraft,
    *,
    upstream_columns: list[set[str]],
    **_: Any,
) -> dict[str, Any]:
    cols = _flatten_upstream(upstream_columns)
    if not cols:
        cols = ["__placeholder__"]
    return {
        "columns": cols,           # passthrough — strings get coerced
        "drop_unselected": True,
    }


def _config_for_formula_eval(
    step: TypedNodeStepDraft,
    **_: Any,
) -> dict[str, Any]:
    """Mock mode produces a single trivial assignment so the
    min_length=1 constraint is satisfied. Audit note recorded by
    the synthesis layer."""
    return {
        "assignments": [
            {
                "target": "_mock_synth_placeholder",
                "expression": "1",
            }
        ],
    }


def _config_for_classify_state_space(
    step: TypedNodeStepDraft,
    **_: Any,
) -> dict[str, Any]:
    """Mock builds a 1-rule classifier whose only state is the
    default value. Satisfies all of:
      * decision_list non-empty (Pydantic min_length)
      * each rule.then ∈ allowed_values
      * default_value ∈ allowed_values
    """
    return {
        "target_field": "_mock_state",
        "allowed_values": ["DEFAULT", "MOCK"],
        "decision_list": [
            {
                "when": "True",
                "then": "MOCK",
                "note": "mock placeholder rule",
            }
        ],
        "default_value": "DEFAULT",
        "require_match": False,
    }


def _config_for_groupby_aggregate(
    step: TypedNodeStepDraft,
    *,
    upstream_columns: list[set[str]],
    **_: Any,
) -> dict[str, Any]:
    cols = _flatten_upstream(upstream_columns)
    group_key = cols[0] if cols else "__placeholder_group__"
    # Aggregation source must NOT collide with the group key — use
    # a different column when available, else a synthetic count of
    # the group key itself (count works on any column).
    agg_source = (cols[1] if len(cols) > 1 else group_key)
    return {
        "group_keys": [group_key],
        "aggregations": [
            {
                "target": "_mock_count",
                "source": agg_source,
                "fn": "count",
            }
        ],
    }


def _config_for_format_convert(
    step: TypedNodeStepDraft,
    *,
    input_artifacts: list[PlannerInputArtifact],
    output_artifacts: list[PlannerOutputArtifact],
    **_: Any,
) -> dict[str, Any]:
    """Pull from/to formats from the artifact contracts.

    format_convert is unusual — it's both a source AND a sink in the
    same step (file → file). The Pass 1 graph binds it via
    ``consumes_artifact_ids`` AND ``produces_artifact_ids``.
    """
    in_id = step.consumes_artifact_ids[0] if step.consumes_artifact_ids else None
    out_id = step.produces_artifact_ids[0] if step.produces_artifact_ids else None
    in_art = _find_input_artifact(input_artifacts, in_id) if in_id else None
    out_art = _find_output_artifact(output_artifacts, out_id) if out_id else None

    in_path = in_art.path if in_art else f"<missing:{in_id or 'no_input'}>"
    out_path = out_art.path if out_art else f"<missing:{out_id or 'no_output'}>"
    from_fmt = in_art.format if in_art else "json"
    to_fmt = out_art.format if out_art else "csv"

    cfg: dict[str, Any] = {
        "in_path": in_path,
        "out_path": out_path,
        "from_format": from_fmt,
        "to_format": to_fmt,
    }
    if to_fmt in ("csv", "tsv"):
        # FormatConvertConfig requires csv_columns_in_order when
        # destination is delimited. Use the artifact's required
        # columns or a placeholder.
        if out_art and out_art.required_columns:
            cfg["csv_columns_in_order"] = list(out_art.required_columns)
        else:
            cfg["csv_columns_in_order"] = ["__placeholder__"]
    if from_fmt == to_fmt:
        # Pydantic validator rejects same-format pairs. In that case,
        # nudge the to_format so the dict still parses (assigner may
        # also surface a warning via synthesis_notes).
        cfg["to_format"] = "json" if to_fmt == "csv" else "csv"
        if cfg["to_format"] in ("csv", "tsv") and "csv_columns_in_order" not in cfg:
            cfg["csv_columns_in_order"] = ["__placeholder__"]
    return cfg


def _config_for_coding_agent(
    step: TypedNodeStepDraft,
    **_: Any,
) -> dict[str, Any]:
    """Stub config — coding_agent has no Pydantic config_class.

    The audit-log dict still records the escape_hatch_reason and a
    placeholder for the future coding_agent contract.
    """
    return {
        "escape_hatch_reason": step.escape_hatch_reason or "",
        "placeholder": True,
    }


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------


_DISPATCH = {
    "read_csv": _config_for_read_csv,
    "write_csv": _config_for_write_csv,
    "schema_validate": _config_for_schema_validate,
    "merge": _config_for_merge,
    "filter": _config_for_filter,
    "project": _config_for_project,
    "formula_eval": _config_for_formula_eval,
    "classify_state_space": _config_for_classify_state_space,
    "groupby_aggregate": _config_for_groupby_aggregate,
    "format_convert": _config_for_format_convert,
    "coding_agent": _config_for_coding_agent,
}


def synthesize_mock_config(
    step: TypedNodeStepDraft,
    *,
    input_artifacts: list[PlannerInputArtifact],
    output_artifacts: list[PlannerOutputArtifact],
    upstream_columns: list[set[str]],
    **_unused: Any,
) -> dict[str, Any]:
    """Public entry: build a mock config dict for a single Pass 1 step.

    ``**_unused`` swallows any extra kwargs the service's synthesizer
    protocol may pass (currently ``requirement_text``) — keeps the
    mock factory and the LLM synthesizer signature-compatible without
    having to update both sides in lockstep.

    Raises ``KeyError`` if ``step.node_type`` isn't in the dispatch
    table — that indicates a bug in the pass-1 validator (which is
    supposed to reject unknown node types before this point).
    """
    if step.node_type not in _DISPATCH:
        raise KeyError(
            f"no mock config factory for node_type {step.node_type!r}; "
            f"registered: {sorted(_DISPATCH.keys())}"
        )
    factory = _DISPATCH[step.node_type]
    return factory(
        step,
        input_artifacts=input_artifacts,
        output_artifacts=output_artifacts,
        upstream_columns=upstream_columns,
    )


__all__ = ["synthesize_mock_config"]
