"""Tests for the ``classify_state_space`` typed-node executor."""
from __future__ import annotations

import pytest

from app.services.typed_nodes.configs import (
    ClassificationRule,
    ClassifyStateSpaceConfig,
)
from app.services.typed_nodes.errors import (
    ClassifyEvalError,
    NoRuleMatchedError,
)
from app.services.typed_nodes.executors.classify_state_space import (
    ClassifyStateSpaceExecutor,
)
from app.services.typed_nodes.registry import (
    get_executor_class,
    has_node_type,
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_classify_is_registered():
    import app.services.typed_nodes.executors  # noqa: F401

    assert has_node_type("classify_state_space")
    assert get_executor_class("classify_state_space") is ClassifyStateSpaceExecutor


# ---------------------------------------------------------------------------
# Basic decision-list evaluation
# ---------------------------------------------------------------------------


def test_first_match_wins():
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["high", "medium", "low"],
        decision_list=[
            ClassificationRule(when="x > 100", then="high"),
            ClassificationRule(when="x > 50", then="medium"),
        ],
        default_value="low",
    )
    out = ClassifyStateSpaceExecutor().execute(cfg, [[
        {"x": 200},   # → high (rule 0)
        {"x": 75},    # → medium (rule 1)
        {"x": 10},    # → low (default)
    ]])
    assert [r["bucket"] for r in out] == ["high", "medium", "low"]


def test_default_when_no_match():
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["high", "low"],
        decision_list=[ClassificationRule(when="x > 100", then="high")],
        default_value="low",
    )
    out = ClassifyStateSpaceExecutor().execute(cfg, [[{"x": 5}]])
    assert out[0]["bucket"] == "low"


def test_rule_order_matters():
    """If rule 0 matches, rule 1 isn't evaluated even if it would
    also match — first-match semantics."""
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["a", "b"],
        decision_list=[
            ClassificationRule(when="x > 0", then="a"),
            ClassificationRule(when="x > 0", then="b"),  # never reached
        ],
        default_value="a",
    )
    out = ClassifyStateSpaceExecutor().execute(cfg, [[{"x": 5}]])
    assert out[0]["bucket"] == "a"


def test_input_columns_preserved():
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 0", then="a")],
        default_value="b",
    )
    out = ClassifyStateSpaceExecutor().execute(cfg, [[
        {"x": 5, "y": "extra", "z": None}
    ]])
    assert out[0] == {"x": 5, "y": "extra", "z": None, "bucket": "a"}


# ---------------------------------------------------------------------------
# Evidence field
# ---------------------------------------------------------------------------


def test_evidence_field_uses_note_when_present():
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["a", "b"],
        decision_list=[
            ClassificationRule(
                when="x > 0", then="a",
                note="x is positive",
            ),
        ],
        default_value="b",
        evidence_field="bucket_reason",
    )
    out = ClassifyStateSpaceExecutor().execute(cfg, [[{"x": 5}]])
    assert out[0]["bucket_reason"] == "rule[0]: x is positive"


def test_evidence_field_falls_back_to_when_text():
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["a", "b"],
        decision_list=[
            ClassificationRule(when="x > 0", then="a"),  # no note
        ],
        default_value="b",
        evidence_field="reason",
    )
    out = ClassifyStateSpaceExecutor().execute(cfg, [[{"x": 5}]])
    assert out[0]["reason"] == "rule[0]: x > 0"


def test_evidence_field_for_default_says_default():
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 100", then="a")],
        default_value="b",
        evidence_field="reason",
    )
    out = ClassifyStateSpaceExecutor().execute(cfg, [[{"x": 5}]])
    assert out[0]["reason"] == "default"


def test_evidence_field_not_set_omitted_from_output():
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 0", then="a")],
        default_value="b",
        # evidence_field=None
    )
    out = ClassifyStateSpaceExecutor().execute(cfg, [[{"x": 5}]])
    assert "evidence_field" not in out[0]


# ---------------------------------------------------------------------------
# Null handling — SQL semantics
# ---------------------------------------------------------------------------


def test_null_in_predicate_falls_through_to_next_rule():
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["a", "b", "c"],
        decision_list=[
            ClassificationRule(when="x > 100", then="a"),  # null → not matched
            ClassificationRule(when="y == 'present'", then="b"),
        ],
        default_value="c",
    )
    out = ClassifyStateSpaceExecutor().execute(cfg, [[
        {"x": None, "y": "present"},  # rule 0 None → fall through; rule 1 matches
    ]])
    assert out[0]["bucket"] == "b"


def test_all_nulls_falls_through_to_default():
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 0", then="a")],
        default_value="b",
    )
    out = ClassifyStateSpaceExecutor().execute(cfg, [[{"x": None}]])
    assert out[0]["bucket"] == "b"


# ---------------------------------------------------------------------------
# require_match
# ---------------------------------------------------------------------------


def test_require_match_raises_when_no_rule_hits():
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 1000", then="a")],
        default_value="b",
        require_match=True,
    )
    with pytest.raises(NoRuleMatchedError) as exc:
        ClassifyStateSpaceExecutor().execute(cfg, [[{"x": 5}]])
    assert exc.value.row_index == 0
    assert exc.value.target_field == "bucket"


def test_require_match_passes_when_rule_hits():
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 0", then="a")],
        default_value="b",
        require_match=True,
    )
    out = ClassifyStateSpaceExecutor().execute(cfg, [[{"x": 5}]])
    assert out[0]["bucket"] == "a"


def test_require_match_reports_specific_row_index():
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 0", then="a")],
        default_value="b",
        require_match=True,
    )
    records = [{"x": 5}, {"x": 10}, {"x": -1}, {"x": 3}]
    with pytest.raises(NoRuleMatchedError) as exc:
        ClassifyStateSpaceExecutor().execute(cfg, [records])
    assert exc.value.row_index == 2


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unknown_column_in_predicate_raises():
    cfg = ClassifyStateSpaceConfig(
        target_field="b",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="missing > 0", then="a")],
        default_value="b",
    )
    with pytest.raises(ClassifyEvalError) as exc:
        ClassifyStateSpaceExecutor().execute(cfg, [[{"x": 5}]])
    assert exc.value.rule_index == 0
    assert exc.value.row_index == 0
    assert "missing" in exc.value.cause


def test_unsafe_predicate_rejected_pre_flight():
    cfg = ClassifyStateSpaceConfig(
        target_field="b",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x.__class__", then="a")],
        default_value="b",
    )
    with pytest.raises(ClassifyEvalError) as exc:
        ClassifyStateSpaceExecutor().execute(cfg, [[]])
    assert exc.value.row_index == -1   # pre-flight, no row touched
    assert "unsafe" in exc.value.cause.lower()


def test_evaluation_error_at_specific_row_and_rule():
    cfg = ClassifyStateSpaceConfig(
        target_field="b",
        allowed_values=["a", "b"],
        decision_list=[
            ClassificationRule(when="x > 0", then="a"),
            ClassificationRule(when="y / z > 1", then="a"),
        ],
        default_value="b",
    )
    records = [
        {"x": 0, "y": 10, "z": 2},   # rule 0 false, rule 1 true → 'a'
        {"x": 0, "y": 10, "z": 0},   # rule 0 false, rule 1 div0 → boom
    ]
    with pytest.raises(ClassifyEvalError) as exc:
        ClassifyStateSpaceExecutor().execute(cfg, [records])
    assert exc.value.row_index == 1
    assert exc.value.rule_index == 1
    assert "division" in exc.value.cause.lower()


def test_wrong_input_count_raises():
    cfg = ClassifyStateSpaceConfig(
        target_field="b",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 0", then="a")],
        default_value="b",
    )
    with pytest.raises(ClassifyEvalError):
        ClassifyStateSpaceExecutor().execute(cfg, [])
    with pytest.raises(ClassifyEvalError):
        ClassifyStateSpaceExecutor().execute(cfg, [[], []])


def test_non_dict_row_raises():
    cfg = ClassifyStateSpaceConfig(
        target_field="b",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 0", then="a")],
        default_value="b",
    )
    with pytest.raises(ClassifyEvalError) as exc:
        ClassifyStateSpaceExecutor().execute(cfg, [["not_a_dict"]])
    assert exc.value.row_index == 0


# ---------------------------------------------------------------------------
# output_schema
# ---------------------------------------------------------------------------


def test_output_schema_adds_target_field():
    cfg = ClassifyStateSpaceConfig(
        target_field="risk",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 0", then="a")],
        default_value="b",
    )
    schema = ClassifyStateSpaceExecutor.output_schema(
        cfg, {"x", "y", "z"}
    )
    assert schema == {"x", "y", "z", "risk"}


def test_output_schema_adds_evidence_field_when_set():
    cfg = ClassifyStateSpaceConfig(
        target_field="risk",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 0", then="a")],
        default_value="b",
        evidence_field="risk_reason",
    )
    schema = ClassifyStateSpaceExecutor.output_schema(cfg, {"x"})
    assert schema == {"x", "risk", "risk_reason"}


def test_output_schema_no_upstream():
    cfg = ClassifyStateSpaceConfig(
        target_field="risk",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 0", then="a")],
        default_value="b",
    )
    assert ClassifyStateSpaceExecutor.output_schema(cfg) == {"risk"}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_records_returns_empty():
    cfg = ClassifyStateSpaceConfig(
        target_field="b",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 0", then="a")],
        default_value="b",
    )
    out = ClassifyStateSpaceExecutor().execute(cfg, [[]])
    assert out == []


def test_does_not_mutate_input():
    cfg = ClassifyStateSpaceConfig(
        target_field="bucket",
        allowed_values=["a", "b"],
        decision_list=[ClassificationRule(when="x > 0", then="a")],
        default_value="b",
    )
    records = [{"x": 5}]
    out = ClassifyStateSpaceExecutor().execute(cfg, [records])
    assert "bucket" not in records[0]   # input untouched
    assert out[0] is not records[0]


# ---------------------------------------------------------------------------
# Realistic spare_parts scenarios
# ---------------------------------------------------------------------------


def test_spare_parts_risk_before_classifier():
    """The 5-state risk_before classifier from the (c) translation."""
    cfg = ClassifyStateSpaceConfig(
        target_field="risk_before",
        evidence_field="risk_evidence",
        allowed_values=[
            "Stockout Risk", "Imminent Shortage", "Normal",
            "Overstock Risk", "Replenishment Blocked",
        ],
        default_value="Normal",
        decision_list=[
            ClassificationRule(
                when="lifecycle in ['EOL', 'Discontinued']",
                then="Replenishment Blocked",
                note="lifecycle blocked",
            ),
            ClassificationRule(
                when="(available_qty + inbound_approved) < reorder_point_internal "
                     "and criticality == 'A'",
                then="Stockout Risk",
                note="below reorder for critical part",
            ),
            ClassificationRule(
                when="dos_days < lead_time_days",
                then="Imminent Shortage",
                note="DOS below lead time",
            ),
            ClassificationRule(
                when="available_qty > target_stock_internal * 1.35",
                then="Overstock Risk",
                note="above 1.35x target",
            ),
        ],
    )
    records = [
        # P-001: critical + stockout
        {"sku_id": "P-001", "lifecycle": "Active", "criticality": "A",
         "available_qty": 8, "inbound_approved": 0,
         "reorder_point_internal": 116, "dos_days": 1.7,
         "lead_time_days": 12, "target_stock_internal": 174},
        # P-008: overstock (way above target)
        {"sku_id": "P-008", "lifecycle": "Active", "criticality": "C",
         "available_qty": 1000, "inbound_approved": 0,
         "reorder_point_internal": 50, "dos_days": 100,
         "lead_time_days": 5, "target_stock_internal": 100},
        # P-017: EOL — replenishment blocked
        {"sku_id": "P-017", "lifecycle": "EOL", "criticality": "B",
         "available_qty": 5, "inbound_approved": 0,
         "reorder_point_internal": 30, "dos_days": 1,
         "lead_time_days": 7, "target_stock_internal": 60},
        # P-099: nothing matches → default Normal
        {"sku_id": "P-099", "lifecycle": "Active", "criticality": "B",
         "available_qty": 50, "inbound_approved": 10,
         "reorder_point_internal": 30, "dos_days": 30,
         "lead_time_days": 5, "target_stock_internal": 100},
    ]
    out = ClassifyStateSpaceExecutor().execute(cfg, [records])
    risks = {r["sku_id"]: r["risk_before"] for r in out}
    assert risks["P-001"] == "Stockout Risk"
    assert risks["P-008"] == "Overstock Risk"
    assert risks["P-017"] == "Replenishment Blocked"
    assert risks["P-099"] == "Normal"
    # Evidence
    p001_ev = next(r for r in out if r["sku_id"] == "P-001")
    assert "below reorder" in p001_ev["risk_evidence"]


def test_spare_parts_approval_mode_classifier():
    """The 4-mode approval_mode classifier encoding 4 hard constraints."""
    cfg = ClassifyStateSpaceConfig(
        target_field="approval_mode",
        evidence_field="approval_reason",
        allowed_values=[
            "Auto PO", "Manual Approval", "Freeze Purchase", "No Replenishment",
        ],
        default_value="Auto PO",
        decision_list=[
            ClassificationRule(
                when="risk_before == 'Replenishment Blocked'",
                then="No Replenishment",
                note="EOL — no replenishment",
            ),
            ClassificationRule(
                when="risk_before == 'Overstock Risk'",
                then="Freeze Purchase",
                note="overstock — freeze purchase",
            ),
            ClassificationRule(
                when="reliability_grade == 'D' and latest_leadtime_days >= 30",
                then="Manual Approval",
                note="unreliable supplier with long lead time",
            ),
            ClassificationRule(
                when=(
                    "unit_cost >= 5000 or criticality == 'A' or "
                    "reliability_grade in ['C', 'D']"
                ),
                then="Manual Approval",
                note="high-value or critical or unstable supplier",
            ),
        ],
    )
    records = [
        # Critical part → Manual Approval
        {"sku_id": "P-001", "risk_before": "Stockout Risk", "criticality": "A",
         "unit_cost": 5200, "reliability_grade": "B",
         "latest_leadtime_days": 12},
        # Overstock → Freeze Purchase (rule 1 hits before rule 3)
        {"sku_id": "P-008", "risk_before": "Overstock Risk", "criticality": "C",
         "unit_cost": 2000, "reliability_grade": "B",
         "latest_leadtime_days": 7},
        # EOL → No Replenishment (rule 0 hits)
        {"sku_id": "P-017", "risk_before": "Replenishment Blocked",
         "criticality": "B", "unit_cost": 100, "reliability_grade": "A",
         "latest_leadtime_days": 5},
        # Cheap, non-critical, reliable supplier → Auto PO (default)
        {"sku_id": "P-099", "risk_before": "Stockout Risk", "criticality": "C",
         "unit_cost": 50, "reliability_grade": "A",
         "latest_leadtime_days": 7},
    ]
    out = ClassifyStateSpaceExecutor().execute(cfg, [records])
    modes = {r["sku_id"]: r["approval_mode"] for r in out}
    assert modes["P-001"] == "Manual Approval"
    assert modes["P-008"] == "Freeze Purchase"
    assert modes["P-017"] == "No Replenishment"
    assert modes["P-099"] == "Auto PO"


def test_chained_classifiers_risk_then_approval():
    """Chain risk_before classifier into approval_mode classifier."""
    risk_cfg = ClassifyStateSpaceConfig(
        target_field="risk_before",
        allowed_values=["Stockout Risk", "Normal"],
        decision_list=[
            ClassificationRule(when="available_qty < reorder", then="Stockout Risk"),
        ],
        default_value="Normal",
    )
    approval_cfg = ClassifyStateSpaceConfig(
        target_field="approval_mode",
        allowed_values=["Auto PO", "Manual Approval"],
        decision_list=[
            ClassificationRule(
                when="risk_before == 'Stockout Risk' and unit_cost >= 5000",
                then="Manual Approval",
            ),
        ],
        default_value="Auto PO",
    )
    records = [
        {"sku_id": "P1", "available_qty": 5, "reorder": 100, "unit_cost": 6000},
        {"sku_id": "P2", "available_qty": 5, "reorder": 100, "unit_cost": 100},
    ]
    after_risk = ClassifyStateSpaceExecutor().execute(risk_cfg, [records])
    after_approval = ClassifyStateSpaceExecutor().execute(approval_cfg, [after_risk])
    assert after_approval[0]["approval_mode"] == "Manual Approval"
    assert after_approval[1]["approval_mode"] == "Auto PO"
