"""Task 7.6D-5 — complexity-aware max_iterations policy."""
from __future__ import annotations

from app.schemas.node_execution_spec import (
    ExecutionConstraints,
    FailurePolicy,
    NodeExecutionSpec,
    NodeInputContract,
    NodeInputSlot,
    NodeOutputContract,
    NodeOutputSlot,
)
from app.schemas.problem_formulation import OutputClosureContract
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
)
from app.services.node_spec.iteration_budget_policy import (
    apply_iteration_budget_policy,
    derive_max_iterations,
)


def _spec(
    *,
    node_id: str = "n",
    inputs: list[NodeInputSlot] | None = None,
    primary_kind: str = "tabular_records",
    primary_locator: str | None = None,
    primary_schema: list[str] | None = None,
    max_iter: int = 1,
) -> NodeExecutionSpec:
    return NodeExecutionSpec(
        node_id=node_id,
        semantic_goal="g",
        input_contract=NodeInputContract(required=list(inputs or [])),
        output_contract=NodeOutputContract(
            primary_output=NodeOutputSlot(
                name="out",
                kind=primary_kind,
                locator_hint=primary_locator,
                schema_hint=list(primary_schema or []),
            ),
        ),
        execution_constraints=ExecutionConstraints(max_iterations=max_iter),
        failure_policy=FailurePolicy(),
    )


def _oc(
    *,
    delivery: str | None = None,
    required: list[str] | None = None,
) -> OutputClosureContract:
    return OutputClosureContract(
        deliverable_form="dataset",
        delivery_target=delivery,
        output_modality="csv",
        closure_status="output_closed",
        required_columns=list(required or []),
        required_columns_source=("user_explicit_listing" if required else "none"),
    )


# ---------------------------------------------------------------------------
# I1-I6: derive_max_iterations table
# ---------------------------------------------------------------------------


def test_i1_verify_role_gets_one_iteration():
    chosen, reason = derive_max_iterations(
        spec=_spec(), semantic_role="verify", output_closure=None
    )
    assert chosen == 1
    assert "verify" in reason


def test_i2_act_role_gets_two_iterations():
    """``act`` is the planner enum value for write nodes; ``persist``
    is its longer alias. Both should resolve to 2."""
    chosen_act, _ = derive_max_iterations(
        spec=_spec(), semantic_role="act", output_closure=None
    )
    chosen_persist, _ = derive_max_iterations(
        spec=_spec(), semantic_role="persist", output_closure=None
    )
    assert chosen_act == chosen_persist == 2


def test_i3_acquire_with_inputs_gets_three():
    """Bumped from 2 → 3 in 7.6G+: iter 1 is consumed by directory
    enumeration (allowed by exploration_only detector for acquire
    nodes), iter 2 is the real implementation, iter 3 is the repair
    window for typos / wrong-serializer choices."""
    spec = _spec(
        inputs=[NodeInputSlot(name="csv", kind="csv_file", source_hint="/tmp/a.csv")]
    )
    chosen, reason = derive_max_iterations(
        spec=spec, semantic_role="acquire", output_closure=None
    )
    assert chosen == 3
    assert reason == "acquire_node_default"


def test_i4_acquire_with_no_inputs_gets_three():
    """The 'enumerate the input directory' pattern."""
    chosen, reason = derive_max_iterations(
        spec=_spec(), semantic_role="acquire", output_closure=None
    )
    assert chosen == 3
    assert "no_explicit_inputs" in reason


def test_i5_simple_transform_gets_three():
    spec = _spec(
        inputs=[NodeInputSlot(name="t", kind="tabular_records", source_hint="A")]
    )
    chosen, _ = derive_max_iterations(
        spec=spec, semantic_role="transform", output_closure=None
    )
    assert chosen == 3


def test_i6_multi_input_dataset_bundle_transform_gets_four():
    """The ``join 6 CSVs into one inventory view`` pattern."""
    spec = _spec(
        inputs=[
            NodeInputSlot(
                name="bundle",
                kind="dataset_collection",
                schema_hint=["a", "b", "c", "d", "e"],
                source_hint="A",
            )
        ]
    )
    chosen, reason = derive_max_iterations(
        spec=spec, semantic_role="transform", output_closure=None
    )
    assert chosen == 4
    assert "multi_input_dataset_bundle" in reason


def test_i7_terminal_node_with_required_columns_gets_five():
    """The user listed 15 required columns AND this node writes the
    final deliverable. Give the agent room to repair through
    contract feedback."""
    delivery = "/tmp/out.csv"
    spec = _spec(
        primary_locator=delivery,
        primary_schema=["queue_id", "sku_id", "part_name"],  # arbitrary
    )
    oc = _oc(
        delivery=delivery,
        required=[
            "queue_id", "sku_id", "part_name", "supplier_id", "supplier_name",
            "trigger_type", "risk_before", "recommended_qty", "order_qty_rounded",
            "estimated_cost", "approval_mode", "expected_arrival_date",
        ],  # 12 columns
    )
    chosen, reason = derive_max_iterations(
        spec=spec, semantic_role="decide", output_closure=oc
    )
    assert chosen == 5
    assert "terminal_node_with_required_columns" in reason


# ---------------------------------------------------------------------------
# I8: apply_iteration_budget_policy never lowers an LLM-authored value
# ---------------------------------------------------------------------------


def test_i8_policy_does_not_lower_existing_higher_value():
    """If the LLM specced max_iter=4, act-policy says 2, but
    we don't downgrade — preserve LLM authority on the upper end."""
    spec = _spec(node_id="A", max_iter=4)
    planner = WorkflowPlannerOutput(
        node_drafts=[
            WorkflowPlannerNodeDraft(
                draft_node_id="A",
                step_name="A",
                node_name="A",
                node_semantic_type="act",  # planner enum: act = write
                semantic_goal="g",
                preferred_capability_ids=["coding_agent"],
                candidate_capability_ids=["coding_agent"],
                upstream_dependencies=[],
                planner_metadata={},
            )
        ],
        edge_drafts=[],
        entry_node_ids=["A"],
        exit_node_ids=["A"],
    )
    audit = apply_iteration_budget_policy(
        specs=[spec], planner_output=planner, output_closure=_oc()
    )
    assert audit == []  # no change recorded
    assert spec.execution_constraints.max_iterations == 4


def test_i9_policy_raises_low_value_to_role_floor():
    spec = _spec(node_id="A", max_iter=1)  # too tight
    planner = WorkflowPlannerOutput(
        node_drafts=[
            WorkflowPlannerNodeDraft(
                draft_node_id="A",
                step_name="A",
                node_name="A",
                node_semantic_type="transform",
                semantic_goal="g",
                preferred_capability_ids=["coding_agent"],
                candidate_capability_ids=["coding_agent"],
                upstream_dependencies=[],
                planner_metadata={},
            )
        ],
        edge_drafts=[],
        entry_node_ids=["A"],
        exit_node_ids=["A"],
    )
    audit = apply_iteration_budget_policy(
        specs=[spec], planner_output=planner, output_closure=_oc()
    )
    assert len(audit) == 1
    decision = audit[0]
    assert decision.previous_iterations == 1
    assert decision.chosen_iterations == 3
    assert decision.semantic_role == "transform"
    assert spec.execution_constraints.max_iterations == 3
