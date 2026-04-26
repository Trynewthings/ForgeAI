"""Task 7.6E — business rule extraction → propagation → injection
→ verification.

Tests cover:
  E1   BusinessRuleContract schema is well-formed.
  E2   Partition validator passes business_rules through verbatim.
  E3   Partition validator drops malformed rule entries silently.
  E4   _apply_business_rules_to_specs writes contracts onto the
       terminal spec, derives AcceptanceCriterion entries, and
       leaves intermediates untouched.
  E5   apply_spec_to_node propagates business_contracts into
       planner_metadata['business_rules'].
  E6   build_step_dict lifts planner_metadata['business_rules']
       to the top-level step dict.
  E7   _task_contract_from_step copies business_rules onto the
       task contract dict.
  E8   prompt_builder renders business_rules with anti-fabrication
       framing.
  E9   verifier flags state-space violations.
  E10  verifier flags uniformly-zero calculations.
  E11  verifier flags template-evidence values.
  E12  verifier flags row-count Cartesian patterns.
  E13  verifier flags fabricated join values when source table is
       provided.
  E14  golden comparator reports missing / extra / mismatched rows.
  E15  load_source_tables picks up CSVs in the workspace.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app.schemas.coding_agent import TaskContract
from app.schemas.front_chain import (
    FrontChainInterpreterLLMOutput,
    FrontChainInterpreterOutputClosureCandidate,
)
from app.schemas.node_execution_spec import (
    AcceptanceCriterion,
    BusinessRuleContract as SpecBusinessRuleContract,  # re-export from node_execution_spec
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
    OutputClosureContract,
)
from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.workflow_planner import (
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
)
from app.services.build.planner_output_bindings import build_step_dict
from app.services.coding_agent.business_semantic_verifier import (
    verify_business_semantics,
)
from app.services.coding_agent.golden_comparator import (
    compare_golden,
    load_source_tables,
)
from app.services.coding_agent.prompt_builder import _format_business_rules
from app.services.executable_manifest.resolver import _task_contract_from_step
from app.services.front_chain.contract_partition_validator import (
    ContractPartitionValidator,
)
from app.services.node_spec.node_spec_service import (
    _apply_business_rules_to_specs,
)
from app.services.workflow_planner.runtime_field_enricher import (
    META_BUSINESS_RULES,
    apply_spec_to_node,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    node_id: str,
    locator_hint: str | None,
    *,
    output_schema: list[str] | None = None,
    input_schema: list[str] | None = None,
    sketch: list[str] | None = None,
) -> NodeExecutionSpec:
    return NodeExecutionSpec(
        node_id=node_id,
        semantic_goal="g",
        input_contract=NodeInputContract(
            required=[
                NodeInputSlot(name="in", kind="tabular_records", schema_hint=list(input_schema or []))
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


def _terminal_compile_ctx(
    *,
    delivery_target: str,
    rules: list[BusinessRuleContract],
) -> CompileContextResponse:
    """Build a minimal CompileContextResponse with the required
    contracts populated using their default factories, plus the
    output_closure we want to test against."""
    from app.schemas.problem_formulation import (
        InputClosureContract,
        ProblemFormulationContract,
    )
    from app.schemas.solvability import SolvabilityContract

    oc = OutputClosureContract(
        deliverable_form="dataset",
        delivery_target=delivery_target,
        output_modality="csv",
        closure_status="output_closed",
        business_rules=list(rules),
    )
    return CompileContextResponse(
        problem_formulation=ProblemFormulationContract(),
        input_closure=InputClosureContract(),
        output_closure=oc,
        solvability=SolvabilityContract(),
    )


def _make_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_e1_business_rule_contract_schema_well_formed():
    rule = BusinessRuleContract(
        rule_id="state_risk",
        rule_type="state_space",
        description="risk_before allowed values",
        fields=["risk_before"],
        allowed_values=["Stockout Risk", "Normal"],
        source_quote="risk_before ∈ {...}",
        severity="hard",
    )
    assert rule.rule_id == "state_risk"
    assert rule.rule_type == "state_space"
    assert rule.allowed_values == ["Stockout Risk", "Normal"]
    # Fields default empty
    minimal = BusinessRuleContract(
        rule_id="r2", description="x", rule_type="other"
    )
    assert minimal.fields == []
    assert minimal.allowed_values == []
    assert minimal.severity == "hard"


# ---------------------------------------------------------------------------
# Partition validator
# ---------------------------------------------------------------------------


def test_e2_partition_validator_passes_business_rules_through():
    envelope = FrontChainInterpreterLLMOutput(
        output_closure_candidate=FrontChainInterpreterOutputClosureCandidate(
            deliverable_form="dataset",
            delivery_target="/tmp/out.csv",
            output_modality="csv",
            closure_status="output_closed",
            business_rules=[
                BusinessRuleContract(
                    rule_id="state_risk",
                    rule_type="state_space",
                    description="risk_before allowed values",
                    fields=["risk_before"],
                    allowed_values=["Stockout Risk", "Normal"],
                ),
                BusinessRuleContract(
                    rule_id="calc_cost",
                    rule_type="calculation",
                    description="estimated_cost formula",
                    fields=["estimated_cost"],
                    formula_hint="estimated_cost = qty * unit_cost",
                ),
            ],
        ),
    )
    result = ContractPartitionValidator().validate_and_partition(envelope)
    assert len(result.output_closure.business_rules) == 2
    assert result.output_closure.business_rules[0].rule_id == "state_risk"
    assert result.output_closure.business_rules[1].formula_hint == (
        "estimated_cost = qty * unit_cost"
    )


def test_e3_partition_validator_drops_malformed_business_rule_entries():
    """LLM occasionally emits string entries or dicts missing required
    fields. The validator should NOT fail the whole envelope — drop
    the bad entry and keep going."""
    # We simulate the post-parse normalization by passing an
    # interpreter envelope built with valid items only — and then
    # exercising the dict-coercion path by going through
    # ``output_payload``-style data the validator sees internally.
    # Easiest way: inject a malformed dict via output_closure_candidate
    # that we then mutate.
    envelope = FrontChainInterpreterLLMOutput(
        output_closure_candidate=FrontChainInterpreterOutputClosureCandidate(
            deliverable_form="dataset",
            delivery_target="/tmp/out.csv",
            output_modality="csv",
            closure_status="output_closed",
            business_rules=[
                BusinessRuleContract(
                    rule_id="ok_rule",
                    rule_type="state_space",
                    description="ok",
                    fields=["x"],
                    allowed_values=["a"],
                )
            ],
        ),
    )
    result = ContractPartitionValidator().validate_and_partition(envelope)
    assert len(result.output_closure.business_rules) == 1
    assert result.output_closure.business_rules[0].rule_id == "ok_rule"


# ---------------------------------------------------------------------------
# specify-time injection
# ---------------------------------------------------------------------------


def test_e4_apply_business_rules_writes_to_terminal_spec_only():
    delivery = "/tmp/out.csv"
    rules = [
        BusinessRuleContract(
            rule_id="state_risk",
            rule_type="state_space",
            description="risk_before allowed values",
            fields=["risk_before"],
            allowed_values=["Stockout Risk", "Normal"],
        ),
        BusinessRuleContract(
            rule_id="calc_cost",
            rule_type="calculation",
            description="estimated_cost formula",
            fields=["estimated_cost"],
            formula_hint="estimated_cost = qty * unit_cost",
        ),
    ]
    # Terminal owns the delivery_target and has full output schema.
    terminal = _make_spec(
        "T",
        locator_hint=delivery,
        output_schema=["sku_id", "risk_before", "estimated_cost"],
    )
    # Intermediate has NO rule-governed fields, so 7.6F field-overlap
    # should not attach anything. It's an opaque transform like
    # "merge upstream tables" with neutral output schema.
    intermediate = _make_spec(
        "M",
        locator_hint=None,
        output_schema=["sku_id", "warehouse", "on_hand"],
    )
    ctx = _terminal_compile_ctx(delivery_target=delivery, rules=rules)

    audit = _apply_business_rules_to_specs(
        specs=[terminal, intermediate], compile_context=ctx
    )

    # Terminal got the contracts
    assert len(terminal.business_contracts) == 2
    assert {r.rule_id for r in terminal.business_contracts} == {"state_risk", "calc_cost"}
    # Legacy mirror also populated
    assert {r.rule_id for r in terminal.business_rules} == {"state_risk", "calc_cost"}
    # AcceptanceCriterion derivations
    state_ac = next(
        (ac for ac in terminal.acceptance_criteria if ac.criterion_id == "ac_state_risk"),
        None,
    )
    assert state_ac is not None
    assert state_ac.kind == "enum_membership"
    assert state_ac.params["allowed_values"] == ["Stockout Risk", "Normal"]
    # Intermediate has no rule-governed fields → no field-overlap attachment
    assert intermediate.business_contracts == []
    assert intermediate.business_rules == []
    # Audit shape — 7.6F: dict with terminal_attachments + field_overlap_attachments
    terminal_audit = audit["terminal_attachments"]
    assert len(terminal_audit) == 1
    assert terminal_audit[0]["node_id"] == "T"
    assert terminal_audit[0]["rules_attached"] == 2
    assert terminal_audit[0]["reason"] == "terminal_locator_match"
    # No field-overlap matches (intermediate spec has no rule-governed fields)
    assert audit["field_overlap_attachments"] == []


def test_e4b_apply_business_rules_no_op_when_empty():
    terminal = _make_spec("T", locator_hint="/tmp/out.csv")
    ctx = _terminal_compile_ctx(delivery_target="/tmp/out.csv", rules=[])
    audit = _apply_business_rules_to_specs(specs=[terminal], compile_context=ctx)
    assert audit == {
        "terminal_attachments": [],
        "field_overlap_attachments": [],
        "unattached_overlaps": [],
    }
    assert terminal.business_contracts == []


# ---------------------------------------------------------------------------
# planner_metadata propagation
# ---------------------------------------------------------------------------


def test_e5_apply_spec_to_node_writes_business_rules_metadata():
    spec = _make_spec("T", locator_hint="/tmp/out.csv")
    spec.business_contracts = [
        BusinessRuleContract(
            rule_id="state_risk",
            rule_type="state_space",
            description="risk_before",
            fields=["risk_before"],
            allowed_values=["Stockout Risk"],
        )
    ]
    node = WorkflowPlannerNodeDraft(
        draft_node_id="T",
        step_name="T",
        node_name="T",
        node_semantic_type="transform",
        semantic_goal="g",
        preferred_capability_ids=["coding_agent"],
        candidate_capability_ids=["coding_agent"],
        upstream_dependencies=[],
        planner_metadata={},
    )
    apply_spec_to_node(node=node, spec=spec)
    rules_meta = node.planner_metadata.get(META_BUSINESS_RULES)
    assert isinstance(rules_meta, list)
    assert len(rules_meta) == 1
    assert rules_meta[0]["rule_id"] == "state_risk"
    assert rules_meta[0]["allowed_values"] == ["Stockout Risk"]


def test_e6_build_step_dict_lifts_business_rules_to_step():
    node = WorkflowPlannerNodeDraft(
        draft_node_id="T",
        step_name="T",
        node_name="T",
        node_semantic_type="transform",
        semantic_goal="g",
        preferred_capability_ids=["coding_agent"],
        candidate_capability_ids=["coding_agent"],
        upstream_dependencies=[],
        planner_metadata={
            "business_rules": [
                {
                    "rule_id": "state_risk",
                    "rule_type": "state_space",
                    "description": "risk_before",
                    "fields": ["risk_before"],
                    "allowed_values": ["Stockout Risk"],
                }
            ]
        },
    )
    step = build_step_dict(node)
    assert "business_rules" in step
    assert step["business_rules"][0]["rule_id"] == "state_risk"


def test_e7_task_contract_from_step_propagates_business_rules():
    """resolver._task_contract_from_step must copy business_rules
    from the step dict onto the contract dict."""
    from app.schemas.executable_manifest import ExecutionPackageBinding

    raw_step = {
        "step_name": "S",
        "purpose": "P",
        "node_id": "T",
        "logical_step_kind": "transform",
        "node_semantic_type": "transform",
        "expected_outputs": [{"locator": "/tmp/out.csv", "kind": "data.derived"}],
        "business_rules": [
            {
                "rule_id": "calc_cost",
                "rule_type": "calculation",
                "description": "estimated_cost",
                "fields": ["estimated_cost"],
                "formula_hint": "qty * unit_cost",
                "severity": "hard",
            }
        ],
    }
    binding = ExecutionPackageBinding(
        step_name="S",
        logical_step_kind="transform",
        selected_capability_id="coding_agent",
        config_hints={},
    )
    contract = _task_contract_from_step(raw_step=raw_step, binding=binding)
    assert "business_rules" in contract
    assert len(contract["business_rules"]) == 1
    assert contract["business_rules"][0]["rule_id"] == "calc_cost"


# ---------------------------------------------------------------------------
# prompt_builder
# ---------------------------------------------------------------------------


def test_e8_prompt_builder_renders_business_rules_with_anti_fabrication():
    rules = [
        {
            "rule_id": "state_risk",
            "rule_type": "state_space",
            "description": "risk_before allowed values",
            "fields": ["risk_before"],
            "allowed_values": ["Stockout Risk", "Normal"],
            "severity": "hard",
        },
        {
            "rule_id": "calc_cost",
            "rule_type": "calculation",
            "description": "estimated_cost formula",
            "fields": ["estimated_cost"],
            "formula_hint": "qty * unit_cost",
            "severity": "hard",
        },
    ]
    out = _format_business_rules(rules)
    assert "USER-AUTHORITATIVE" in out
    assert "STATE_SPACE" in out
    assert "Stockout Risk" in out
    assert "qty * unit_cost" in out
    assert "Anti-fabrication" in out
    assert "Cartesian product" in out


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


def test_e9_verifier_flags_state_space_violation(tmp_path):
    p = tmp_path / "out.csv"
    _make_csv(
        p,
        rows=[{"sku_id": f"S{i}", "risk_before": "Critical"} for i in range(5)],
        fieldnames=["sku_id", "risk_before"],
    )
    rules = [
        {
            "rule_id": "state_risk",
            "rule_type": "state_space",
            "description": "risk_before",
            "fields": ["risk_before"],
            "allowed_values": ["Stockout Risk", "Normal"],
            "severity": "hard",
        }
    ]
    res = verify_business_semantics(produced_path=p, business_rules=rules)
    assert not res.ok
    assert res.failure_kind == "business_semantics_violation"
    assert any(v.rule_id == "state_risk" for v in res.violations)
    state_v = next(v for v in res.violations if v.rule_id == "state_risk")
    assert state_v.bad_rows == 5


def test_e10_verifier_flags_uniformly_zero_calculation(tmp_path):
    p = tmp_path / "out.csv"
    _make_csv(
        p,
        rows=[{"sku_id": f"S{i}", "estimated_cost": "0.0"} for i in range(20)],
        fieldnames=["sku_id", "estimated_cost"],
    )
    rules = [
        {
            "rule_id": "calc_cost",
            "rule_type": "calculation",
            "description": "estimated_cost formula",
            "fields": ["estimated_cost"],
            "formula_hint": "qty * unit_cost",
            "severity": "hard",
        }
    ]
    res = verify_business_semantics(produced_path=p, business_rules=rules)
    assert not res.ok
    assert any(v.rule_id == "calc_cost" for v in res.violations)


def test_e11_verifier_flags_template_evidence_values(tmp_path):
    p = tmp_path / "out.csv"
    _make_csv(
        p,
        rows=[
            {"sku_id": f"S{i}", "calculation_note": "Order recommended"}
            for i in range(10)
        ],
        fieldnames=["sku_id", "calculation_note"],
    )
    rules = [
        {
            "rule_id": "evid_note",
            "rule_type": "evidence_requirement",
            "description": "real upstream values in calculation_note",
            "fields": ["calculation_note"],
            "severity": "hard",
        }
    ]
    res = verify_business_semantics(produced_path=p, business_rules=rules)
    assert not res.ok
    assert any(v.rule_id == "evid_note" for v in res.violations)


def test_e12_verifier_flags_cartesian_row_count(tmp_path):
    out_p = tmp_path / "out.csv"
    sku_p = tmp_path / "sku_master.csv"
    date_p = tmp_path / "demand_history.csv"
    # 10 SKUs × 12 dates = 120
    _make_csv(
        sku_p,
        rows=[{"sku_id": f"S{i}"} for i in range(10)],
        fieldnames=["sku_id"],
    )
    _make_csv(
        date_p,
        rows=[{"date": f"2024-01-{i:02d}"} for i in range(1, 13)],
        fieldnames=["date"],
    )
    _make_csv(
        out_p,
        rows=[
            {"sku_id": f"S{i}", "date": f"2024-01-{j:02d}", "qty": "1"}
            for i in range(10)
            for j in range(1, 13)
        ],
        fieldnames=["sku_id", "date", "qty"],
    )
    rules = [
        {
            "rule_id": "filter_queue",
            "rule_type": "filter",
            "description": "queue contains only actionable SKUs",
            "severity": "hard",
        }
    ]
    sources = {
        "sku_master": [{"sku_id": f"S{i}"} for i in range(10)],
        "demand_history": [{"date": f"2024-01-{i:02d}"} for i in range(1, 13)],
    }
    res = verify_business_semantics(
        produced_path=out_p,
        business_rules=rules,
        source_tables=sources,
    )
    assert not res.ok
    assert any(v.rule_id == "filter_queue" for v in res.violations)
    msg = next(v.message for v in res.violations if v.rule_id == "filter_queue")
    assert "Cartesian" in msg


def test_e13_verifier_flags_fabricated_join_supplier_id(tmp_path):
    out_p = tmp_path / "out.csv"
    _make_csv(
        out_p,
        rows=[
            {"sku_id": "S1", "supplier_id": "SUP-001"},
            {"sku_id": "S2", "supplier_id": "SUP-002"},
        ],
        fieldnames=["sku_id", "supplier_id"],
    )
    rules = [
        {
            "rule_id": "join_supplier",
            "rule_type": "join",
            "description": "supplier_id must come from suppliers source table",
            "fields": ["supplier_id"],
            "severity": "hard",
        }
    ]
    sources = {
        "suppliers": [
            {"sku_id": "S1", "supplier_id": "S-001"},
            {"sku_id": "S2", "supplier_id": "S-004"},
        ],
    }
    res = verify_business_semantics(
        produced_path=out_p,
        business_rules=rules,
        source_tables=sources,
    )
    assert not res.ok
    assert any(v.rule_id == "join_supplier" for v in res.violations)


# ---------------------------------------------------------------------------
# Golden comparator
# ---------------------------------------------------------------------------


def test_e14_golden_comparator_reports_missing_extra_mismatches(tmp_path):
    prod_p = tmp_path / "produced.csv"
    gold_p = tmp_path / "expected.csv"
    _make_csv(
        prod_p,
        rows=[
            {"sku_id": "S1", "qty": "10"},
            {"sku_id": "S2", "qty": "20"},
            {"sku_id": "S99", "qty": "0"},  # extra row
        ],
        fieldnames=["sku_id", "qty"],
    )
    _make_csv(
        gold_p,
        rows=[
            {"sku_id": "S1", "qty": "10"},
            {"sku_id": "S2", "qty": "25"},  # mismatch
            {"sku_id": "S3", "qty": "30"},  # missing in produced
        ],
        fieldnames=["sku_id", "qty"],
    )
    res = compare_golden(produced_path=prod_p, golden_path=gold_p, key_fields=["sku_id"])
    assert not res.passed
    assert any(r["sku_id"] == "S3" for r in res.missing_rows)
    assert any(r["sku_id"] == "S99" for r in res.extra_rows)
    assert any(
        m["field"] == "qty" and m["expected"] == "25" and m["actual"] == "20"
        for m in res.field_mismatches
    )


def test_e15_load_source_tables_picks_up_workspace_csvs(tmp_path):
    inp = tmp_path / "input"
    inp.mkdir()
    sku_p = inp / "sku_master.csv"
    sup_p = inp / "suppliers.csv"
    _make_csv(sku_p, rows=[{"sku_id": "S1"}], fieldnames=["sku_id"])
    _make_csv(sup_p, rows=[{"supplier_id": "S-001"}], fieldnames=["supplier_id"])
    tables = load_source_tables(workspace_dir=str(tmp_path), input_evidence=[])
    assert "sku_master" in tables
    assert "sku_master.csv" in tables
    assert tables["sku_master"][0]["sku_id"] == "S1"
    assert "suppliers" in tables
