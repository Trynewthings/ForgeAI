from __future__ import annotations

from app.schemas.workflow_native import (
    BuildMaterializationRecord,
    CapabilityRequirementRef,
    EvidenceRetentionPolicy,
    ExecutionStateModel,
    HumanOverlayRecord,
    InterventionPolicy,
    NodeCapabilityBinding,
    NodeDraft,
    NodeInstance,
    NodePortSpec,
    NodeSpec,
    ObservabilityContract,
    RetryPolicy,
    RerunPolicy,
    TestabilityContract as WorkflowTestabilityContract,
    VerificationContract,
    VerificationMethod,
    VerificationPlan,
    WorkflowArtifact,
    WorkflowEdge,
    WorkflowExecutionContract,
    WorkflowGraphDraft,
    WorkflowGraph,
    WorkflowPlanningDraft,
)


def test_workflow_native_node_spec_and_instance_models_round_trip() -> None:
    spec = NodeSpec(
        spec_id="fetch_and_parse_spec",
        node_semantic_type="acquire",
        input_port_specs=[
            NodePortSpec(port_name="source", schema_summary="URL or path", semantic_role="source_locator"),
        ],
        output_port_specs=[
            NodePortSpec(port_name="raw_content", schema_summary="Fetched content", semantic_role="evidence"),
        ],
        capability_requirements=[
            CapabilityRequirementRef(
                semantic_goal="acquire bounded source content",
                allowed_capability_ids=["fetch_url", "filesystem_read"],
                preferred_capability_ids=["fetch_url"],
            )
        ],
        intervention_policy=InterventionPolicy(allowed_modes=["inspect"]),
        rerun_policy=RerunPolicy(rerun_allowed=True, rerun_invalidates_downstream=True),
        observability_contract=ObservabilityContract(hooks=["execution_trace", "output_snapshot"]),
        testability_contract=WorkflowTestabilityContract(dry_run_supported=True, expected_output_schema="raw_content:string"),
        verification_contract=VerificationContract(postconditions=["raw_content is non-empty"]),
        admissible_execution_regimes=["evidence_seeking", "workflow_build"],
    )

    instance = NodeInstance(
        node_id="node_fetch_source",
        spec_ref=spec.spec_id,
        bound_capability=NodeCapabilityBinding(
            capability_id="fetch_url",
            produced_outputs=["raw_content"],
        ),
        input_wiring=[],
        materialization_status="resolved",
    )

    payload = instance.model_dump(mode="python")
    assert payload["spec_ref"] == "fetch_and_parse_spec"
    assert payload["bound_capability"]["capability_id"] == "fetch_url"


def test_workflow_native_graph_contract_and_verification_models_compose() -> None:
    graph = WorkflowGraph(
        graph_id="workflow_graph_v1",
        node_instances=[
            NodeInstance(node_id="n1", spec_ref="fetch_spec", materialization_status="resolved"),
            NodeInstance(node_id="n2", spec_ref="summarize_spec", materialization_status="resolved"),
        ],
        edge_set=[
            WorkflowEdge(
                edge_id="e1",
                edge_type="data_flow",
                from_node_id="n1",
                to_node_id="n2",
                payload_mapping={"raw_content": "source_text"},
            )
        ],
        entry_nodes=["n1"],
        exit_nodes=["n2"],
    )
    contract = WorkflowExecutionContract(
        global_retry_policy=RetryPolicy(max_attempts=2, backoff_strategy="simple_backoff"),
        rerun_legality_policy=RerunPolicy(rerun_allowed=True),
        intervention_legality=InterventionPolicy(allowed_modes=["inspect", "approve"]),
        evidence_retention_policy=EvidenceRetentionPolicy(retain_inputs=True, retain_outputs=True),
        verification_rules=["final artifact must be emitted"],
    )
    verification = VerificationPlan(
        verification_methods=[
            VerificationMethod(method_id="verify_artifact", method_kind="artifact_presence", binding_status="bound")
        ],
        required_evidence=["artifact.saved"],
        postconditions=["artifact.saved exists"],
        verification_binding_status="bound",
    )

    artifact = WorkflowArtifact(
        artifact_id="artifact_v1",
        workflow_graph=graph,
        execution_contract=contract,
        verification_plan=verification,
        build_materialization_record=BuildMaterializationRecord(build_warnings=["none"]),
        package_metadata={"package_kind": "workflow_artifact"},
    )

    dumped = artifact.model_dump(mode="python")
    assert dumped["workflow_graph"]["entry_nodes"] == ["n1"]
    assert dumped["execution_contract"]["global_retry_policy"]["max_attempts"] == 2
    assert dumped["verification_plan"]["verification_binding_status"] == "bound"


def test_workflow_native_execution_state_model_preserves_human_overlay_layer() -> None:
    state = ExecutionStateModel(
        human_overlay_state={
            "overlays": [
                HumanOverlayRecord(
                    overlay_id="overlay_1",
                    overlay_actor="operator",
                    overlay_scope="node:n2",
                    overlay_reason="Approve edited payload",
                    overlay_timestamp="2026-03-24T00:00:00Z",
                    affected_nodes=["n2"],
                    operation="approve",
                    reconciliation_status="applied",
                )
            ],
            "operator_notes": ["approved after inspection"],
        }
    )

    dumped = state.model_dump(mode="python")
    assert dumped["human_overlay_state"]["overlays"][0]["overlay_actor"] == "operator"
    assert dumped["human_overlay_state"]["overlays"][0]["reconciliation_status"] == "applied"


def test_workflow_native_planning_draft_models_compose() -> None:
    graph_draft = WorkflowGraphDraft(
        draft_id="draft_v1",
        node_drafts=[
            NodeDraft(
                draft_node_id="draft_node_1",
                step_name="fetch_source",
                node_semantic_type="acquire",
                semantic_goal="Acquire source material",
                candidate_capability_ids=["fetch_url", "filesystem_read"],
            ),
            NodeDraft(
                draft_node_id="draft_node_2",
                step_name="summarize_source",
                node_semantic_type="transform",
                semantic_goal="Summarize acquired source",
                upstream_dependencies=["draft_node_1"],
                preferred_capability_ids=["summarize_text_llm"],
            ),
        ],
        edge_set=[
            WorkflowEdge(
                edge_id="draft_edge_1_2",
                edge_type="data_flow",
                from_node_id="draft_node_1",
                to_node_id="draft_node_2",
            )
        ],
        entry_nodes=["draft_node_1"],
        exit_nodes=["draft_node_2"],
        draft_status="partial",
    )
    planning = WorkflowPlanningDraft(
        draft_id="planning_v1",
        workflow_graph_draft=graph_draft,
        verification_plan_draft=VerificationPlan(verification_binding_status="draft"),
        capability_subset_summary={"fetch_source": ["fetch_url", "filesystem_read"]},
        admissibility_summary={"primary_execution_regime": "workflow_build"},
    )

    dumped = planning.model_dump(mode="python")
    assert dumped["workflow_graph_draft"]["entry_nodes"] == ["draft_node_1"]
    assert dumped["workflow_graph_draft"]["node_drafts"][1]["preferred_capability_ids"] == ["summarize_text_llm"]
    assert dumped["verification_plan_draft"]["verification_binding_status"] == "draft"
