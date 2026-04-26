"""Tests for TypedNodeExecutor base, registry, and error types."""
from __future__ import annotations

from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from app.services.typed_nodes.base import TypedNodeExecutor
from app.services.typed_nodes.errors import (
    ClassifyEvalError,
    ConfigError,
    ExecutionError,
    FormulaEvalError,
    MergeError,
    NegativeValueError,
    NoRuleMatchedError,
    SchemaCheckError,
    SchemaValidationFailure,
    StaticAnalysisFailure,
    TypedNodeError,
)
from app.services.typed_nodes.registry import (
    get_catalog_summary,
    get_executor_class,
    has_node_type,
    list_node_types,
    register_typed_node,
    render_catalog_summary_for_prompt,
    reset_registry_for_tests,
)


# ---------------------------------------------------------------------------
# Mock executor for testing
# ---------------------------------------------------------------------------


class MockConfig(BaseModel):
    multiplier: int = 1


class MockExecutor(TypedNodeExecutor):
    node_type: ClassVar[str] = "mock_test_node"
    config_class: ClassVar[type[BaseModel]] = MockConfig

    def execute(self, config: MockConfig, inputs: list[Any]) -> Any:
        if not inputs:
            return []
        return [{"v": r["v"] * config.multiplier} for r in inputs[0]]

    @staticmethod
    def output_schema(config: MockConfig, *upstream: set[str]) -> set[str]:
        return upstream[0] if upstream else {"v"}


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty registry to avoid order coupling."""
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


# ---------------------------------------------------------------------------
# Registry behavior
# ---------------------------------------------------------------------------


def test_register_and_lookup():
    register_typed_node(MockExecutor, summary="mock test node")
    assert has_node_type("mock_test_node")
    assert get_executor_class("mock_test_node") is MockExecutor


def test_get_unknown_node_type_raises():
    with pytest.raises(KeyError) as exc:
        get_executor_class("does_not_exist")
    assert "does_not_exist" in str(exc.value)


def test_register_idempotent_for_same_class():
    register_typed_node(MockExecutor, summary="mock test node")
    register_typed_node(MockExecutor, summary="mock test node")  # no-op
    assert list_node_types() == ["mock_test_node"]


def test_register_different_class_under_same_name_raises():
    register_typed_node(MockExecutor, summary="x")

    class Other(TypedNodeExecutor):
        node_type = "mock_test_node"
        config_class = MockConfig

        def execute(self, config, inputs):
            return None

        @staticmethod
        def output_schema(config, *upstream):
            return set()

    with pytest.raises(ValueError) as exc:
        register_typed_node(Other, summary="x")
    assert "already registered" in str(exc.value)


def test_register_executor_without_node_type_raises():
    class Bad(TypedNodeExecutor):
        # node_type intentionally left as default ""
        config_class = MockConfig

        def execute(self, config, inputs):
            return None

        @staticmethod
        def output_schema(config, *upstream):
            return set()

    with pytest.raises(ValueError) as exc:
        register_typed_node(Bad, summary="x")
    assert "no node_type" in str(exc.value)


def test_register_non_executor_subclass_raises():
    class NotAnExecutor:
        node_type = "x"
        config_class = MockConfig

    with pytest.raises(TypeError):
        register_typed_node(NotAnExecutor, summary="x")  # type: ignore


# ---------------------------------------------------------------------------
# Catalog summary
# ---------------------------------------------------------------------------


def test_catalog_summary_returns_entries_sorted():
    register_typed_node(MockExecutor, summary="mock test node")

    class Another(TypedNodeExecutor):
        node_type = "another_node"
        config_class = MockConfig

        def execute(self, config, inputs):
            return None

        @staticmethod
        def output_schema(config, *upstream):
            return set()

    register_typed_node(Another, summary="another mock")
    entries = get_catalog_summary()
    assert [e.node_type for e in entries] == ["another_node", "mock_test_node"]


def test_render_catalog_summary_for_prompt_includes_escape_hatch():
    register_typed_node(MockExecutor, summary="mock test node")
    rendered = render_catalog_summary_for_prompt()
    assert "node_type" in rendered
    assert "mock_test_node" in rendered
    assert "mock test node" in rendered
    # Escape-hatch row always appended
    assert "coding_agent" in rendered
    assert "escape hatch" in rendered


# ---------------------------------------------------------------------------
# Base class behavior
# ---------------------------------------------------------------------------


def test_validate_config_accepts_dict():
    cfg = MockExecutor.validate_config({"multiplier": 5})
    assert isinstance(cfg, MockConfig)
    assert cfg.multiplier == 5


def test_validate_config_passes_already_validated_instance():
    instance = MockConfig(multiplier=3)
    cfg = MockExecutor.validate_config(instance)
    assert cfg is instance  # no re-construction


def test_validate_config_invalid_raises_schema_validation_failure():
    with pytest.raises(SchemaValidationFailure) as exc:
        MockExecutor.validate_config({"multiplier": "not an int"})
    assert exc.value.node_type == "mock_test_node"
    assert exc.value.pydantic_errors  # at least one error recorded


def test_executor_execute_basic():
    config = MockConfig(multiplier=3)
    out = MockExecutor().execute(config, [[{"v": 2}, {"v": 4}]])
    assert out == [{"v": 6}, {"v": 12}]


def test_executor_output_schema():
    config = MockConfig(multiplier=1)
    schema = MockExecutor.output_schema(config, {"v", "x"})
    assert schema == {"v", "x"}


# ---------------------------------------------------------------------------
# Error classes — structure and serialization
# ---------------------------------------------------------------------------


def test_typed_node_error_to_dict():
    err = TypedNodeError("boom", node_type="x", details={"k": 1})
    d = err.to_dict()
    assert d["error_class"] == "TypedNodeError"
    assert d["node_type"] == "x"
    assert d["details"] == {"k": 1}
    assert d["message"] == "boom"


def test_schema_validation_failure_carries_pydantic_errors():
    err = SchemaValidationFailure(
        node_type="merge",
        pydantic_errors=[{"loc": ["on"], "type": "missing"}],
    )
    assert err.node_type == "merge"
    assert err.pydantic_errors[0]["type"] == "missing"
    assert "merge" in str(err)


def test_static_analysis_failure_carries_available_columns():
    err = StaticAnalysisFailure(
        node_type="formula_eval",
        location="assignments[2].expression",
        cause="references unknown column 'foo'",
        available_columns=["a", "b"],
    )
    assert err.location == "assignments[2].expression"
    assert err.available_columns == ["a", "b"]
    assert "foo" in err.cause


def test_formula_eval_error_carries_full_context():
    err = FormulaEvalError(
        assignment_index=2,
        target="estimated_cost",
        expression="qty * price",
        row_index=5,
        row_inputs_subset={"qty": 10, "price": "bad"},
        cause="TypeError: ...",
    )
    assert err.assignment_index == 2
    assert err.target == "estimated_cost"
    assert err.row_index == 5
    assert err.row_inputs_subset == {"qty": 10, "price": "bad"}
    d = err.to_dict()
    assert d["details"]["target"] == "estimated_cost"
    assert d["details"]["row_index"] == 5


def test_classify_eval_error_carries_rule_index():
    err = ClassifyEvalError(
        rule_index=1,
        when="x > 0",
        row_index=3,
        cause="TypeError",
    )
    assert err.rule_index == 1
    assert err.when == "x > 0"
    assert err.row_index == 3


def test_no_rule_matched_error_includes_target():
    err = NoRuleMatchedError(row_index=7, target_field="approval_mode")
    assert err.target_field == "approval_mode"
    assert err.row_index == 7
    assert "approval_mode" in str(err)


def test_negative_value_error_is_specific():
    err = NegativeValueError(column="estimated_cost", row_index=2, value=-50)
    assert err.column == "estimated_cost"
    assert err.value == -50
    assert isinstance(err, ExecutionError)


def test_schema_check_error_aggregates_violations():
    err = SchemaCheckError(violations=[
        {"kind": "missing_column", "name": "x"},
        {"kind": "null_in_required", "name": "y", "row_index": 3},
    ])
    assert len(err.violations) == 2
    assert "2 violation" in str(err)


# ---------------------------------------------------------------------------
# Error hierarchy (defensive — make sure the ABCs are wired right)
# ---------------------------------------------------------------------------


def test_error_hierarchy():
    assert issubclass(SchemaValidationFailure, ConfigError)
    assert issubclass(SchemaValidationFailure, TypedNodeError)
    assert issubclass(MergeError, ExecutionError)
    assert issubclass(FormulaEvalError, ExecutionError)
    assert issubclass(ConfigError, TypedNodeError)
    assert issubclass(ExecutionError, TypedNodeError)
