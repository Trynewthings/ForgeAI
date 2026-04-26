"""Task 7.6F — field-overlap rule scoping tests.

Covers:
  F1   state_space rule attaches to intermediate spec whose output
       schema contains the rule field.
  F2   approval_policy rule attaches to spec whose output schema
       contains approval_mode.
  F3   calculation rule attaches to a node outputting estimated_cost.
  F4   join rule attaches to a node outputting part_name.
  F5   Terminal delivery-target spec still receives the FULL rule
       list (incl. field-less filter / hard_constraint / test_case).
  F6   Non-overlapping intermediate spec receives nothing.
  F7   Field matching is exact, not substring.
  F8   _attach_rules_to_spec is idempotent — calling twice with
       overlapping lists doesn't double-attach.
  F9   Scoper picks up rule fields from input schema as well.
  F10  Sketch-based Tier-B match only fires for fields in the
       OutputClosure.required_columns vocabulary.
  F11  Unattached-overlap soft audit fires when a rule that wasn't
       attached has a field name overlap with the spec.
  F12  apply_business_rules_to_specs returns the new dict shape
       with terminal_attachments / field_overlap_attachments /
       unattached_overlaps keys.
"""
from __future__ import annotations

import pytest

from app.schemas.node_execution_spec import (
    AcceptanceCriterion,
    ExecutionConstraints,
    FailurePolicy,
    NodeExecutionSpec,
    NodeInputContract,
    NodeInputSlot,
    NodeOutputContract,
    NodeOutputSlot,
)
from app.schemas.problem_formulation import (
    BusinessRuleContract,
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.solvability import SolvabilityContract
from app.services.node_spec.business_rule_scoper import (
    detect_unattached_overlaps,
    rules_for_spec,
)
from app.services.node_spec.node_spec_service import (
    _apply_business_rules_to_specs,
    _attach_rules_to_spec,
)


def _spec(
    node_id: str,
    *,
    locator_hint: str | None = None,
    output_schema: list[str] | None = None,
    input_schema: list[str] | None = None,
    sketch: list[str] | None = None,
) -> NodeExecutionSpec:
    return NodeExecutionSpec(
        node_id=node_id,
        semantic_goal="g",
        input_contract=NodeInputContract(
            required=[
                NodeInputSlot(
                    name="in", kind="tabular_records", schema_hint=list(input_schema or [])
                )
            ]
            if input_schema
            else []
        ),
        output_contract=NodeOutputContract(
            primary_output=NodeOutputSlot(
                name="out",
                kind="tabular_records",
                schema_hint=list(output_schema or ["sku_id"]),
                locator_hint=locator_hint,
            ),
        ),
        execution_constraints=ExecutionConstraints(),
        failure_policy=FailurePolicy(),
        implementation_sketch=list(sketch or []),
    )


_RISK_RULE = BusinessRuleContract(
    rule_id="state_risk",
    rule_type="state_space",
    description="risk_before",
    fields=["risk_before"],
    allowed_values=["Stockout Risk", "Imminent Shortage", "Normal"],
)
_APPROVAL_RULE = BusinessRuleContract(
    rule_id="state_approval",
    rule_type="approval_policy",
    description="approval_mode",
    fields=["approval_mode"],
    allowed_values=["Auto PO", "Manual Approval"],
)
_CALC_RULE = BusinessRuleContract(
    rule_id="calc_cost",
    rule_type="calculation",
    description="estimated_cost",
    fields=["estimated_cost"],
    formula_hint="qty * unit_cost",
)
_JOIN_RULE = BusinessRuleContract(
    rule_id="join_part_name",
    rule_type="join",
    description="part_name from sku_master",
    fields=["part_name"],
)
_FILTER_RULE = BusinessRuleContract(
    rule_id="filter_relevant_skus",
    rule_type="filter",
    description="only actionable SKUs",
    fields=[],
)
_HARDCONS_RULE = BusinessRuleContract(
    rule_id="hardcons_discontinued",
    rule_type="hard_constraint",
    description="discontinued cannot be auto-replenished",
    fields=[],
)
_TEST_CASE_RULE = BusinessRuleContract(
    rule_id="tc01",
    rule_type="test_case",
    description="P-001 stockout test",
    fields=[],
)

_ALL_RULES = [
    _RISK_RULE, _APPROVAL_RULE, _CALC_RULE, _JOIN_RULE,
    _FILTER_RULE, _HARDCONS_RULE, _TEST_CASE_RULE,
]


def _ctx(*, delivery_target: str = "/tmp/out.csv", required_columns: list[str] | None = None):
    oc = OutputClosureContract(
        deliverable_form="dataset",
        delivery_target=delivery_target,
        output_modality="csv",
        closure_status="output_closed",
        required_columns=list(required_columns or []),
        business_rules=list(_ALL_RULES),
    )
    return CompileContextResponse(
        problem_formulation=ProblemFormulationContract(),
        input_closure=InputClosureContract(),
        output_closure=oc,
        solvability=SolvabilityContract(),
    )


# ---------------------------------------------------------------------------
# F1-F4 — field-overlap matches
# ---------------------------------------------------------------------------


def test_f1_state_space_attaches_to_intermediate_with_risk_before():
    spec = _spec("n5", output_schema=["sku_id", "risk_before", "approval_mode"])
    applicable, evidence = rules_for_spec(
        spec=spec, business_rules=_ALL_RULES
    )
    rule_ids = {r.rule_id for r in applicable}
    assert "state_risk" in rule_ids
    assert "state_approval" in rule_ids
    assert "calc_cost" not in rule_ids
    assert "risk_before" in evidence["output_schema"]


def test_f2_approval_policy_attaches_when_field_in_output():
    spec = _spec("n6", output_schema=["sku_id", "approval_mode"])
    applicable, _ = rules_for_spec(spec=spec, business_rules=_ALL_RULES)
    rule_ids = {r.rule_id for r in applicable}
    assert rule_ids == {"state_approval"}


def test_f3_calculation_attaches_to_estimated_cost_node():
    spec = _spec("n7", output_schema=["sku_id", "estimated_cost"])
    applicable, _ = rules_for_spec(spec=spec, business_rules=_ALL_RULES)
    rule_ids = {r.rule_id for r in applicable}
    assert rule_ids == {"calc_cost"}


def test_f4_join_rule_attaches_to_part_name_node():
    spec = _spec("n8", output_schema=["sku_id", "part_name"])
    applicable, _ = rules_for_spec(spec=spec, business_rules=_ALL_RULES)
    rule_ids = {r.rule_id for r in applicable}
    assert rule_ids == {"join_part_name"}


# ---------------------------------------------------------------------------
# F5 — terminal still gets full attachment
# ---------------------------------------------------------------------------


def test_f5_terminal_delivery_target_receives_full_rule_list():
    delivery = "/tmp/out.csv"
    terminal = _spec(
        "T",
        locator_hint=delivery,
        output_schema=["sku_id", "risk_before", "approval_mode", "estimated_cost", "part_name"],
    )
    audit = _apply_business_rules_to_specs(specs=[terminal], compile_context=_ctx(delivery_target=delivery))
    assert {r.rule_id for r in terminal.business_contracts} == {r.rule_id for r in _ALL_RULES}
    # All field-less rules made it
    assert any(r.rule_id == "filter_relevant_skus" for r in terminal.business_contracts)
    assert any(r.rule_id == "hardcons_discontinued" for r in terminal.business_contracts)
    assert any(r.rule_id == "tc01" for r in terminal.business_contracts)
    # Audit
    assert len(audit["terminal_attachments"]) == 1
    assert audit["terminal_attachments"][0]["reason"] == "terminal_locator_match"


# ---------------------------------------------------------------------------
# F6 — non-overlapping intermediate gets nothing
# ---------------------------------------------------------------------------


def test_f6_non_overlapping_intermediate_receives_nothing():
    spec = _spec("nx", output_schema=["sku_id", "warehouse", "on_hand"])
    applicable, _ = rules_for_spec(spec=spec, business_rules=_ALL_RULES)
    assert applicable == []


# ---------------------------------------------------------------------------
# F7 — exact match, not substring
# ---------------------------------------------------------------------------


def test_f7_field_matching_is_exact_not_substring():
    # "risk_before_v2" should NOT match a rule about "risk_before"
    spec = _spec("nx", output_schema=["sku_id", "risk_before_v2", "estimated_cost_extra"])
    applicable, _ = rules_for_spec(spec=spec, business_rules=_ALL_RULES)
    rule_ids = {r.rule_id for r in applicable}
    assert "state_risk" not in rule_ids
    assert "calc_cost" not in rule_ids


# ---------------------------------------------------------------------------
# F8 — idempotence
# ---------------------------------------------------------------------------


def test_f8_attach_rules_to_spec_is_idempotent():
    spec = _spec("n", output_schema=["risk_before"])
    _attach_rules_to_spec(spec=spec, rules=[_RISK_RULE])
    _attach_rules_to_spec(spec=spec, rules=[_RISK_RULE, _APPROVAL_RULE])
    rule_ids = [r.rule_id for r in spec.business_contracts]
    legacy_ids = [r.rule_id for r in spec.business_rules]
    ac_ids = [c.criterion_id for c in spec.acceptance_criteria]
    # state_risk attached exactly once; state_approval added on the
    # second call.
    assert rule_ids == ["state_risk", "state_approval"]
    assert legacy_ids == ["state_risk", "state_approval"]
    assert "ac_state_risk" in ac_ids
    # No duplicate criterion ids
    assert len(ac_ids) == len(set(ac_ids))


# ---------------------------------------------------------------------------
# F9 — input schema also triggers a hit
# ---------------------------------------------------------------------------


def test_f9_input_schema_triggers_field_overlap():
    """A node that READS risk_before from upstream should also receive
    the rule (e.g. it might be reformatting / aggregating)."""
    spec = _spec(
        "n",
        input_schema=["sku_id", "risk_before"],
        output_schema=["sku_id", "summary"],
    )
    applicable, evidence = rules_for_spec(spec=spec, business_rules=_ALL_RULES)
    rule_ids = {r.rule_id for r in applicable}
    assert "state_risk" in rule_ids
    assert evidence["input_schema"] == ["risk_before"]


# ---------------------------------------------------------------------------
# F10 — Tier-B sketch match restricted to required_columns vocabulary
# ---------------------------------------------------------------------------


def test_f10_sketch_match_only_fires_for_required_columns_tokens():
    """Sketch-only mention of a rule field should fire ONLY when that
    field is part of the user's required_columns vocabulary. Without
    that gate, opaque LLM-renamed columns would generate false hits."""
    spec = _spec(
        "n",
        output_schema=["sku_id", "score"],  # opaque renamed
        sketch=[
            "compute risk_before classification per SKU",
            "derive approval_mode from heuristics",
        ],
    )
    oc = OutputClosureContract(
        deliverable_form="dataset",
        required_columns=["risk_before"],  # only risk_before is canonical
    )
    applicable, evidence = rules_for_spec(
        spec=spec, business_rules=_ALL_RULES, output_closure=oc
    )
    rule_ids = {r.rule_id for r in applicable}
    # risk_before is in required_columns → sketch hit fires
    assert "state_risk" in rule_ids
    # approval_mode is NOT in required_columns → sketch hit suppressed
    assert "state_approval" not in rule_ids
    assert "risk_before" in evidence["sketch"]


# ---------------------------------------------------------------------------
# F11 — soft unattached-overlap audit
# ---------------------------------------------------------------------------


def test_f11_detect_unattached_overlaps_reports_field_in_schema_without_rule():
    """When a spec touches a rule's fields but the rule wasn't
    attached, ``detect_unattached_overlaps`` should surface it."""
    spec = _spec("n", output_schema=["sku_id", "approval_mode"])
    misses = detect_unattached_overlaps(
        spec=spec,
        business_rules=_ALL_RULES,
        already_attached_rule_ids=set(),
    )
    miss_fields = [m.field_name for m in misses]
    assert "approval_mode" in miss_fields


def test_f11b_no_unattached_overlaps_when_rule_already_attached():
    spec = _spec("n", output_schema=["sku_id", "approval_mode"])
    misses = detect_unattached_overlaps(
        spec=spec,
        business_rules=_ALL_RULES,
        already_attached_rule_ids={"state_approval"},
    )
    miss_fields = [m.field_name for m in misses]
    assert "approval_mode" not in miss_fields


# ---------------------------------------------------------------------------
# F12 — full pipeline through _apply_business_rules_to_specs
# ---------------------------------------------------------------------------


def test_f12_full_pipeline_produces_expected_audit_shape():
    delivery = "/tmp/out.csv"
    terminal = _spec(
        "T",
        locator_hint=delivery,
        output_schema=[
            "sku_id", "part_name", "risk_before", "approval_mode", "estimated_cost"
        ],
    )
    intermediate_classify = _spec(
        "M_classify",
        output_schema=["sku_id", "risk_before", "approval_mode"],
    )
    intermediate_calc = _spec(
        "M_calc",
        output_schema=["sku_id", "recommended_qty", "estimated_cost"],
    )
    intermediate_neutral = _spec(
        "M_neutral",
        output_schema=["sku_id", "warehouse"],
    )

    audit = _apply_business_rules_to_specs(
        specs=[terminal, intermediate_classify, intermediate_calc, intermediate_neutral],
        compile_context=_ctx(delivery_target=delivery),
    )
    # Audit shape
    assert set(audit.keys()) == {
        "terminal_attachments", "field_overlap_attachments", "unattached_overlaps"
    }
    # Terminal attached once with all 7 rules
    assert len(audit["terminal_attachments"]) == 1
    assert audit["terminal_attachments"][0]["node_id"] == "T"
    assert audit["terminal_attachments"][0]["rules_attached"] == len(_ALL_RULES)
    # Field-overlap attachments fired for the two semantic intermediates
    overlap_node_ids = {a["node_id"] for a in audit["field_overlap_attachments"]}
    assert "M_classify" in overlap_node_ids
    assert "M_calc" in overlap_node_ids
    assert "M_neutral" not in overlap_node_ids
    # Classify intermediate got state_risk + state_approval
    classify_audit = next(
        a for a in audit["field_overlap_attachments"] if a["node_id"] == "M_classify"
    )
    assert set(classify_audit["attached_rule_ids"]) == {"state_risk", "state_approval"}
    assert "risk_before" in classify_audit["matched_fields"]
    assert "approval_mode" in classify_audit["matched_fields"]
    # Calc intermediate got calc_cost
    calc_audit = next(
        a for a in audit["field_overlap_attachments"] if a["node_id"] == "M_calc"
    )
    assert calc_audit["attached_rule_ids"] == ["calc_cost"]
    # Specs got their rules attached
    assert {r.rule_id for r in intermediate_classify.business_contracts} == {
        "state_risk", "state_approval"
    }
    assert {r.rule_id for r in intermediate_calc.business_contracts} == {"calc_cost"}
    assert intermediate_neutral.business_contracts == []
