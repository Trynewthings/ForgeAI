"""Tests for the 10 typed-node config schemas.

These configs are the public contract between planner / synthesis
(which generate them) and executors (which consume them). The
critical property is that **invalid configs are rejected at
load time, not at run time** — that's what makes the typed-node
catalog reliable in a way prompt-rule-driven coding_agent can't be.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.services.typed_nodes.configs import (
    AggregationOp,
    ClassificationRule,
    ClassifyStateSpaceConfig,
    FilterConfig,
    FormatConvertConfig,
    FormulaAssignment,
    FormulaEvalConfig,
    GroupByAggregateConfig,
    MergeConfig,
    ProjectColumn,
    ProjectConfig,
    ReadCsvConfig,
    SchemaValidateConfig,
    WriteCsvConfig,
)


# ---------------------------------------------------------------------------
# read_csv
# ---------------------------------------------------------------------------


def test_read_csv_minimum_valid():
    cfg = ReadCsvConfig(path="/tmp/x.csv")
    assert cfg.delimiter == ","
    assert cfg.encoding == "utf-8"
    assert cfg.dtype_overrides == {}
    assert "" in cfg.null_markers


def test_read_csv_with_dtype_overrides():
    cfg = ReadCsvConfig(
        path="/tmp/x.csv",
        dtype_overrides={"sku_id": "str", "qty": "int", "price": "float"},
    )
    assert cfg.dtype_overrides["qty"] == "int"


def test_read_csv_rejects_unknown_dtype():
    with pytest.raises(ValidationError):
        ReadCsvConfig(path="/tmp/x.csv", dtype_overrides={"x": "weird_type"})


def test_read_csv_rejects_extra_field():
    with pytest.raises(ValidationError):
        ReadCsvConfig(path="/tmp/x.csv", made_up_field=True)


def test_read_csv_rejects_empty_path():
    with pytest.raises(ValidationError):
        ReadCsvConfig(path="")


# ---------------------------------------------------------------------------
# write_csv
# ---------------------------------------------------------------------------


def test_write_csv_minimum_valid():
    cfg = WriteCsvConfig(path="/tmp/out.csv", columns_in_order=["a", "b"])
    assert cfg.columns_in_order == ["a", "b"]
    assert cfg.quoting == "minimal"


def test_write_csv_rejects_empty_columns():
    with pytest.raises(ValidationError):
        WriteCsvConfig(path="/tmp/out.csv", columns_in_order=[])


# ---------------------------------------------------------------------------
# schema_validate
# ---------------------------------------------------------------------------


def test_schema_validate_minimum():
    cfg = SchemaValidateConfig(expected_columns=["a", "b"])
    assert cfg.required_non_null == []
    assert cfg.forbid_extra_columns is False


def test_schema_validate_full():
    cfg = SchemaValidateConfig(
        expected_columns=["sku_id", "qty"],
        required_non_null=["sku_id"],
        value_ranges={"qty": [0, 1000]},
        enum_constraints={"sku_id": ["P-001", "P-002"]},
        forbid_extra_columns=True,
    )
    assert cfg.value_ranges["qty"] == [0, 1000]


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


def test_merge_with_on_only():
    cfg = MergeConfig(on="sku_id")
    assert cfg.how == "left"
    assert cfg.suffixes == ("_x", "_y")


def test_merge_with_left_on_right_on():
    cfg = MergeConfig(left_on="left_id", right_on="right_id", how="inner")
    assert cfg.left_on == "left_id"
    assert cfg.right_on == "right_id"


def test_merge_rejects_no_keys():
    with pytest.raises(ValidationError) as exc:
        MergeConfig()
    assert "left_on" in str(exc.value) or "on" in str(exc.value)


def test_merge_rejects_on_with_left_on():
    with pytest.raises(ValidationError):
        MergeConfig(on="x", left_on="y", right_on="z")


def test_merge_with_require_no_nulls_after():
    cfg = MergeConfig(on="sku_id", require_no_nulls_after=["available_qty"])
    assert cfg.require_no_nulls_after == ["available_qty"]


def test_merge_composite_key():
    cfg = MergeConfig(on=["sku_id", "warehouse"])
    assert cfg.on == ["sku_id", "warehouse"]


# ---------------------------------------------------------------------------
# filter
# ---------------------------------------------------------------------------


def test_filter_minimum():
    cfg = FilterConfig(predicate="x > 0")
    assert cfg.keep_or_drop == "keep"
    assert cfg.require_non_empty is False


def test_filter_rejects_empty_predicate():
    with pytest.raises(ValidationError):
        FilterConfig(predicate="")


def test_filter_drop_mode():
    cfg = FilterConfig(predicate="status == 'archived'", keep_or_drop="drop")
    assert cfg.keep_or_drop == "drop"


# ---------------------------------------------------------------------------
# project
# ---------------------------------------------------------------------------


def test_project_with_string_columns():
    cfg = ProjectConfig(columns=["sku_id", "qty"])
    # Strings are normalized to ProjectColumn instances
    assert all(isinstance(c, ProjectColumn) for c in cfg.columns)
    assert cfg.output_names() == ["sku_id", "qty"]


def test_project_with_explicit_renames():
    cfg = ProjectConfig(
        columns=[
            ProjectColumn(source="sku_id"),
            ProjectColumn(source="qty", alias="quantity"),
        ]
    )
    assert cfg.output_names() == ["sku_id", "quantity"]


def test_project_mixed_strings_and_renames():
    cfg = ProjectConfig(
        columns=[
            "sku_id",
            ProjectColumn(source="part_name", alias="name"),
            "qty",
        ]
    )
    assert cfg.output_names() == ["sku_id", "name", "qty"]


def test_project_rejects_duplicate_aliases():
    with pytest.raises(ValidationError) as exc:
        ProjectConfig(
            columns=[
                ProjectColumn(source="a", alias="x"),
                ProjectColumn(source="b", alias="x"),
            ]
        )
    assert "duplicate" in str(exc.value)


# ---------------------------------------------------------------------------
# formula_eval
# ---------------------------------------------------------------------------


def test_formula_eval_minimum():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="y", expression="x * 2")]
    )
    assert len(cfg.assignments) == 1
    assert cfg.coerce_string_to_number is True


def test_formula_eval_multiple_assignments():
    cfg = FormulaEvalConfig(
        assignments=[
            FormulaAssignment(target="safety", expression="base * lead"),
            FormulaAssignment(target="reorder", expression="safety + 10"),
        ]
    )
    assert cfg.assignments[1].target == "reorder"


def test_formula_eval_fill_default_requires_default_value():
    with pytest.raises(ValidationError) as exc:
        FormulaAssignment(target="x", expression="a + b", on_error="fill_default")
    assert "default" in str(exc.value).lower()


def test_formula_eval_fill_default_with_default_ok():
    asn = FormulaAssignment(
        target="x", expression="a + b",
        on_error="fill_default", default=0,
    )
    assert asn.default == 0


def test_formula_eval_rejects_duplicate_targets():
    with pytest.raises(ValidationError) as exc:
        FormulaEvalConfig(assignments=[
            FormulaAssignment(target="x", expression="a"),
            FormulaAssignment(target="x", expression="b"),
        ])
    assert "duplicate" in str(exc.value).lower()


def test_formula_eval_validate_non_negative():
    cfg = FormulaEvalConfig(
        assignments=[FormulaAssignment(target="qty", expression="a")],
        validate_non_negative=["qty"],
    )
    assert "qty" in cfg.validate_non_negative


# ---------------------------------------------------------------------------
# classify_state_space — the most security-critical config
# ---------------------------------------------------------------------------


def test_classify_minimum_valid():
    cfg = ClassifyStateSpaceConfig(
        target_field="risk",
        allowed_values=["low", "high"],
        decision_list=[ClassificationRule(when="x > 5", then="high")],
        default_value="low",
    )
    assert cfg.target_field == "risk"


def test_classify_rejects_then_outside_allowed_values():
    """The 7.6E-rule-as-physical-impossibility test."""
    with pytest.raises(ValidationError) as exc:
        ClassifyStateSpaceConfig(
            target_field="risk_before",
            allowed_values=["Stockout Risk", "Normal", "Overstock Risk"],
            decision_list=[
                ClassificationRule(when="x > 0", then="Critical"),  # NOT in allowed
            ],
            default_value="Normal",
        )
    assert "Critical" in str(exc.value)
    assert "allowed_values" in str(exc.value)


def test_classify_rejects_default_outside_allowed_values():
    with pytest.raises(ValidationError) as exc:
        ClassifyStateSpaceConfig(
            target_field="x",
            allowed_values=["a", "b"],
            decision_list=[ClassificationRule(when="x > 0", then="a")],
            default_value="z",
        )
    assert "default_value" in str(exc.value)


def test_classify_rejects_duplicate_allowed_values():
    with pytest.raises(ValidationError) as exc:
        ClassifyStateSpaceConfig(
            target_field="x",
            allowed_values=["a", "a"],
            decision_list=[ClassificationRule(when="x > 0", then="a")],
            default_value="a",
        )
    assert "duplicate" in str(exc.value).lower()


def test_classify_with_evidence_field_and_notes():
    cfg = ClassifyStateSpaceConfig(
        target_field="risk_before",
        allowed_values=["Stockout Risk", "Normal"],
        decision_list=[
            ClassificationRule(
                when="available_qty < reorder_point",
                then="Stockout Risk",
                note="below reorder threshold",
            ),
        ],
        default_value="Normal",
        evidence_field="risk_evidence",
    )
    assert cfg.decision_list[0].note == "below reorder threshold"


def test_classify_require_match():
    cfg = ClassifyStateSpaceConfig(
        target_field="x",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="z > 0", then="a")],
        default_value="b",
        require_match=True,
    )
    assert cfg.require_match is True


def test_classify_rejects_empty_allowed_values():
    with pytest.raises(ValidationError):
        ClassifyStateSpaceConfig(
            target_field="x",
            allowed_values=["a"],   # min_length=2
            decision_list=[ClassificationRule(when="x > 0", then="a")],
            default_value="a",
        )


# ---------------------------------------------------------------------------
# groupby_aggregate
# ---------------------------------------------------------------------------


def test_groupby_aggregate_minimum():
    cfg = GroupByAggregateConfig(
        group_keys=["sku_id"],
        aggregations=[AggregationOp(target="total_qty", source="qty", fn="sum")],
    )
    assert cfg.sort_by_group is True


def test_groupby_aggregate_rejects_unknown_fn():
    with pytest.raises(ValidationError):
        AggregationOp(target="x", source="y", fn="madeup_fn")


def test_groupby_aggregate_rejects_target_collision_with_group_key():
    with pytest.raises(ValidationError) as exc:
        GroupByAggregateConfig(
            group_keys=["sku_id"],
            aggregations=[AggregationOp(target="sku_id", source="qty", fn="sum")],
        )
    assert "collides" in str(exc.value)


def test_groupby_aggregate_rejects_duplicate_targets():
    with pytest.raises(ValidationError):
        GroupByAggregateConfig(
            group_keys=["sku_id"],
            aggregations=[
                AggregationOp(target="x", source="a", fn="sum"),
                AggregationOp(target="x", source="b", fn="mean"),
            ],
        )


def test_groupby_aggregate_rejects_duplicate_group_keys():
    with pytest.raises(ValidationError):
        GroupByAggregateConfig(
            group_keys=["sku_id", "sku_id"],
            aggregations=[AggregationOp(target="x", source="qty", fn="sum")],
        )


# ---------------------------------------------------------------------------
# format_convert
# ---------------------------------------------------------------------------


def test_format_convert_basic():
    cfg = FormatConvertConfig(
        in_path="/tmp/x.json",
        out_path="/tmp/x.csv",
        from_format="json",
        to_format="csv",
        csv_columns_in_order=["a", "b"],
    )
    assert cfg.to_format == "csv"


def test_format_convert_rejects_same_format():
    with pytest.raises(ValidationError):
        FormatConvertConfig(
            in_path="/tmp/x", out_path="/tmp/y",
            from_format="csv", to_format="csv",
            csv_columns_in_order=["a"],
        )


def test_format_convert_csv_requires_columns_in_order():
    with pytest.raises(ValidationError) as exc:
        FormatConvertConfig(
            in_path="/tmp/x", out_path="/tmp/y",
            from_format="json", to_format="csv",
        )
    assert "csv_columns_in_order" in str(exc.value)


def test_format_convert_json_target_no_columns_required():
    cfg = FormatConvertConfig(
        in_path="/tmp/x", out_path="/tmp/y",
        from_format="csv", to_format="json",
        csv_columns_in_order=None,
    )
    assert cfg.csv_columns_in_order is None


def test_format_convert_unsupported_pair_rejected():
    """Parquet support is deferred — using it should raise."""
    with pytest.raises(ValidationError):
        FormatConvertConfig(
            in_path="/tmp/x", out_path="/tmp/y",
            from_format="parquet", to_format="csv",
            csv_columns_in_order=["a"],
        )


# ---------------------------------------------------------------------------
# JSON Schema generation — every config produces a usable schema
# (used by the planner Pass 2 prompt builder)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("config_class", [
    ReadCsvConfig, WriteCsvConfig, SchemaValidateConfig,
    MergeConfig, FilterConfig, ProjectConfig,
    FormulaEvalConfig, ClassifyStateSpaceConfig,
    GroupByAggregateConfig, FormatConvertConfig,
])
def test_each_config_emits_json_schema(config_class):
    schema = config_class.model_json_schema()
    assert schema["type"] == "object"
    assert "properties" in schema
    # title must exist (LLM prompt readability)
    assert "title" in schema
