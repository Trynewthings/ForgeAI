"""Task C, Layer 1 — `output_closure.required_columns` extraction.

The interpreter LLM is taught to surface explicit column listings
from the user's raw_intent. This test pins:

  - ContractPartitionValidator carries them through to OutputClosureContract
  - source coercion: an empty list overrides the LLM's claim of
    "user_explicit_listing"
  - happy-path verbatim list propagation

The downstream consumer (planner / specify) reading
``compile_context.output_closure.required_columns`` is covered in
adjacent tests once Layer 2 lands; Layer 1 only proves extraction
flows through the partitioner.
"""
from __future__ import annotations

from app.schemas.front_chain import (
    FrontChainInterpreterInputClosureCandidate,
    FrontChainInterpreterLLMOutput,
    FrontChainInterpreterOutputClosureCandidate,
    FrontChainInterpreterProblemCandidate,
)
from app.services.front_chain.contract_partition_validator import (
    ContractPartitionValidator,
)


def _interp_output(*, oc_kwargs: dict) -> FrontChainInterpreterLLMOutput:
    return FrontChainInterpreterLLMOutput(
        problem_formulation_candidate=FrontChainInterpreterProblemCandidate(
            interpreted_problem_kind="local_dataset_task",
            target_object="local_workspace",
            problem_status="solvable",
            current_system_fit="in_scope",
            interpreted_goal="goal",
            planning_intent_label="t",
            planning_intent_objective_family="data_processing",
        ),
        input_closure_candidate=FrontChainInterpreterInputClosureCandidate(
            target_object="local_workspace",
            resolved_source_kind="directory",
            resolved_source_locator="/tmp/in",
            resolved_source_modality="csv",
            acquisition_path_kind="direct_read",
            closure_status="input_closed",
        ),
        output_closure_candidate=FrontChainInterpreterOutputClosureCandidate(
            **oc_kwargs,
        ),
    )


def test_required_columns_propagate_when_llm_extracts_user_explicit_listing():
    cols = [
        "queue_id",
        "sku_id",
        "part_name",
        "supplier_id",
        "supplier_name",
        "trigger_type",
        "risk_before",
        "recommended_qty",
        "order_qty_rounded",
        "estimated_cost",
        "approval_mode",
        "expected_arrival_date",
        "status",
        "confidence",
        "calculation_note",
    ]
    quote = (
        "字段必含 queue_id sku_id part_name supplier_id supplier_name "
        "trigger_type risk_before recommended_qty order_qty_rounded "
        "estimated_cost approval_mode expected_arrival_date status "
        "confidence calculation_note"
    )
    interp = _interp_output(
        oc_kwargs={
            "deliverable_form": "dataset",
            "delivery_target": "/tmp/out.csv",
            "output_modality": "csv",
            "closure_status": "output_closed",
            "required_columns": cols,
            "required_columns_source": "user_explicit_listing",
            "required_columns_verbatim_quote": quote,
        }
    )
    partition = ContractPartitionValidator().validate_and_partition(interp)
    oc = partition.output_closure
    assert oc.required_columns == cols, "verbatim columns must propagate 1:1"
    assert oc.required_columns_source == "user_explicit_listing"
    assert oc.required_columns_verbatim_quote == quote


def test_required_columns_source_coerced_to_none_when_list_is_empty():
    """An LLM that emits ``required_columns_source = "user_explicit_listing"``
    but ``required_columns = []`` is contradicting itself. Coerce
    source to "none"."""
    interp = _interp_output(
        oc_kwargs={
            "deliverable_form": "dataset",
            "delivery_target": "/tmp/out.csv",
            "output_modality": "csv",
            "closure_status": "output_closed",
            "required_columns": [],
            "required_columns_source": "user_explicit_listing",
        }
    )
    partition = ContractPartitionValidator().validate_and_partition(interp)
    oc = partition.output_closure
    assert oc.required_columns == []
    assert oc.required_columns_source == "none"


def test_required_columns_default_to_empty_when_llm_omits():
    """Backward-compat: existing intents that don't list columns flow
    through without surprise."""
    interp = _interp_output(
        oc_kwargs={
            "deliverable_form": "report",
            "delivery_target": "/tmp/out.md",
            "output_modality": "markdown",
            "closure_status": "output_closed",
        }
    )
    partition = ContractPartitionValidator().validate_and_partition(interp)
    oc = partition.output_closure
    assert oc.required_columns == []
    assert oc.required_columns_source == "none"
    assert oc.required_columns_verbatim_quote is None


def test_required_columns_strip_blank_entries():
    """Defensive: an LLM that emits whitespace-only entries (or
    accidental empty strings) should not pollute the column list."""
    interp = _interp_output(
        oc_kwargs={
            "deliverable_form": "dataset",
            "delivery_target": "/tmp/out.csv",
            "output_modality": "csv",
            "closure_status": "output_closed",
            "required_columns": ["sku_id", "  ", "", "part_name"],
            "required_columns_source": "user_explicit_listing",
        }
    )
    partition = ContractPartitionValidator().validate_and_partition(interp)
    oc = partition.output_closure
    assert oc.required_columns == ["sku_id", "part_name"]
    assert oc.required_columns_source == "user_explicit_listing"


def test_required_columns_inferred_source_propagates_with_list():
    """When the LLM is unsure if the user's listing was explicit or
    inferred, it can mark source as ``inferred``. The validator passes
    that through unchanged so downstream consumers can decide whether
    to honor it as a hard constraint or just a hint."""
    interp = _interp_output(
        oc_kwargs={
            "deliverable_form": "dataset",
            "delivery_target": "/tmp/out.csv",
            "output_modality": "csv",
            "closure_status": "output_closed",
            "required_columns": ["a", "b", "c"],
            "required_columns_source": "inferred",
        }
    )
    partition = ContractPartitionValidator().validate_and_partition(interp)
    oc = partition.output_closure
    assert oc.required_columns == ["a", "b", "c"]
    assert oc.required_columns_source == "inferred"
