from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_build_planning_v43 import (
    _create_blueprint,
    _run_build,
    build_runtime_constraints_with_compile_context,
    install_fake_build_planner_provider,
)
from tests.test_phase3_core import create_project, execute_job, wait_for_run_completion


def _build_runnable_package(client: TestClient, monkeypatch) -> tuple[dict, dict, dict, dict]:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 88,
              "proposed_bindings": [
                {
                  "step_name": "parse_input",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "parse_json",
                  "config_hints": {"source_field": "raw_json"}
                },
                {
                  "step_name": "rank_items",
                  "logical_step_kind": "rank",
                  "selected_capability_id": "rank_candidates",
                  "config_hints": {
                    "source_step": "parse_input",
                    "source_field": "parsed_payload",
                    "score_field": "score"
                  }
                },
                {
                  "step_name": "persist_results",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "write_artifact",
                  "config_hints": {
                    "source_step": "rank_items",
                    "source_field": "ranked_candidates",
                    "artifact_filename": "ranked_candidates.json"
                  }
                }
              ]
            }
            """,
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {"step_name": "parse_input", "node_name": "parser", "logical_step_kind": "transform"},
            {"step_name": "rank_items", "node_name": "ranker", "logical_step_kind": "rank"},
            {"step_name": "persist_results", "node_name": "writer", "logical_step_kind": "persist"},
        ],
    )
    build, executable = _run_build(client, project["id"], blueprint["id"])
    return project, blueprint, build, executable


def test_build_materializes_execution_package_contract_and_files(client: TestClient, monkeypatch) -> None:
    _, _, build, executable = _build_runnable_package(client, monkeypatch)

    manifest = build["manifest_snapshot_json"]
    assert manifest["package"]["bindings"] == "bindings.json"
    assert manifest["package"]["runtime_entry"] == "runtime_entry.json"
    assert manifest["package"]["package_metadata"] == "package_metadata.json"
    assert manifest["package"]["asset_index"] == "assets/asset_index.json"
    assert manifest["metadata"]["execution_package"]["runnable_step_count"] == 3
    assert manifest["metadata"]["execution_package"]["blocked_step_count"] == 0
    assert manifest["metadata"]["execution_package_compatibility_view"]["semantic_role"] == "legacy_execution_compatibility_view"
    assert "workflow_native_execution_policy" in manifest["metadata"]["execution_package_compatibility_view"]["not_authoritative_for"]
    assert manifest["metadata"]["runtime_compatibility_sources"]["steps"]["path"] == "steps.json"
    assert manifest["metadata"]["runtime_compatibility_sources"]["steps"]["semantic_role"] == "legacy_sequential_compatibility_source"
    assert manifest["metadata"]["runtime_compatibility_sources"]["steps"]["deprecated_by"] == "workflow_native/workflow_graph.json"
    assert manifest["metadata"]["workflow_native_sidecar"]["projection_applied"] is True
    assert manifest["metadata"]["workflow_native_sidecar"]["planner_output"] == "workflow_native/planner_output.json"
    assert manifest["metadata"]["workflow_native_sidecar"]["planner_draft_entry"] == "workflow_native/planner_output.json"
    assert manifest["metadata"]["workflow_native_sidecar"]["workflow_graph"] == "workflow_native/workflow_graph.json"
    assert manifest["metadata"]["workflow_native_sidecar"]["verification_plan_draft"] == "workflow_native/verification_plan.draft.json"
    assert manifest["metadata"]["workflow_native_sidecar"]["verification_plan_bound"] == "workflow_native/verification_plan.bound.json"
    assert manifest["metadata"]["workflow_native_sidecar"]["execution_contract"] == "workflow_native/execution_contract.json"
    assert manifest["metadata"]["workflow_native_sidecar"]["build_materialization_record"] == "workflow_native/build_materialization_record.json"
    assert manifest["metadata"]["workflow_native_sidecar"]["workflow_artifact"] == "workflow_native/workflow_artifact.json"
    assert manifest["metadata"]["workflow_native_sidecar"]["preferred_explainability_entry"] == "workflow_native/workflow_artifact.json"
    assert manifest["metadata"]["workflow_native_sidecar"]["verification_binding_status"] == "bound"

    file_paths = {item["path"] for item in manifest["files"]}
    assert "bindings.json" in file_paths
    assert "runtime_entry.json" in file_paths
    assert "package_metadata.json" in file_paths
    assert "assets/asset_index.json" in file_paths
    assert "workflow_native/node_specs.json" in file_paths
    assert "workflow_native/node_instances.json" in file_paths
    assert "workflow_native/planner_output.json" in file_paths
    assert "workflow_native/workflow_graph.json" in file_paths
    assert "workflow_native/verification_plan.draft.json" in file_paths
    assert "workflow_native/verification_plan.bound.json" in file_paths
    assert "workflow_native/execution_contract.json" in file_paths
    assert "workflow_native/build_materialization_record.json" in file_paths
    assert "workflow_native/workflow_artifact.json" in file_paths

    bindings_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "bindings.json"},
    )
    assert bindings_response.status_code == 200
    bindings_content = bindings_response.json()["content"]
    assert '"selected_capability_id": "parse_json"' in bindings_content
    assert '"selected_capability_id": "rank_candidates"' in bindings_content
    assert '"selected_capability_id": "write_artifact"' in bindings_content
    assert '"binding_status": "runnable"' in bindings_content

    runtime_entry_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "runtime_entry.json"},
    )
    assert runtime_entry_response.status_code == 200
    runtime_entry_content = runtime_entry_response.json()["content"]
    assert '"execution_mode": "execution-package"' in runtime_entry_content
    assert '"is_runnable": true' in runtime_entry_content

    graph_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "workflow_native/workflow_graph.json"},
    )
    assert graph_response.status_code == 200
    graph_content = graph_response.json()["content"]
    assert '"graph_id": "workflow_native_projection_v1"' in graph_content
    assert '"entry_nodes": [' in graph_content

    node_specs_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "workflow_native/node_specs.json"},
    )
    assert node_specs_response.status_code == 200
    node_specs_content = node_specs_response.json()["content"]
    assert '"spec_id": "parse_input_spec"' in node_specs_content
    assert '"spec_id": "persist_results_spec"' in node_specs_content

    planner_output_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "workflow_native/planner_output.json"},
    )
    assert planner_output_response.status_code == 200
    planner_output_content = planner_output_response.json()["content"]
    assert '"node_drafts"' in planner_output_content
    assert '"edge_drafts"' in planner_output_content
    assert '"planner_version"' in planner_output_content

    verification_draft_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "workflow_native/verification_plan.draft.json"},
    )
    assert verification_draft_response.status_code == 200
    verification_draft_content = verification_draft_response.json()["content"]
    assert '"verification_binding_status": "draft"' in verification_draft_content
    assert '"method_id": "parse_input_contract_check"' in verification_draft_content

    verification_bound_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "workflow_native/verification_plan.bound.json"},
    )
    assert verification_bound_response.status_code == 200
    verification_bound_content = verification_bound_response.json()["content"]
    assert '"verification_binding_status": "bound"' in verification_bound_content
    assert '"method_id": "persist_results_contract_check"' in verification_bound_content

    execution_contract_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "workflow_native/execution_contract.json"},
    )
    assert execution_contract_response.status_code == 200
    execution_contract_content = execution_contract_response.json()["content"]
    assert '"global_retry_policy"' in execution_contract_content
    assert '"runtime_entry_ready": true' in execution_contract_content

    materialization_record_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "workflow_native/build_materialization_record.json"},
    )
    assert materialization_record_response.status_code == 200
    materialization_record_content = materialization_record_response.json()["content"]
    assert '"node_binding_decisions"' in materialization_record_content
    assert '"node_id": "node_1"' in materialization_record_content

    workflow_artifact_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "workflow_native/workflow_artifact.json"},
    )
    assert workflow_artifact_response.status_code == 200
    workflow_artifact_content = workflow_artifact_response.json()["content"]
    assert '"artifact_id": "workflow-artifact-' in workflow_artifact_content
    assert '"workflow_graph"' in workflow_artifact_content
    assert '"execution_contract"' in workflow_artifact_content
    assert '"verification_plan"' in workflow_artifact_content


def test_execution_package_metadata_includes_admissibility_and_planner_projections(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 88,
              "proposed_bindings": [
                {
                  "step_name": "persist_results",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "write_artifact",
                  "config_hints": {
                    "artifact_filename": "result.md"
                  }
                }
              ]
            }
            """,
        ],
    )
    project = create_project(client)
    requirement = client.post(
        "/api/v1/requirements",
        json={"project_id": project["id"], "title": "Workflow Admissibility Requirement"},
    ).json()
    blueprint_response = client.post(
        "/api/v1/blueprints",
        json={
            "project_id": project["id"],
            "requirement_id": requirement["id"],
            "blueprint_type": "workflow",
            "name": "Admissibility Workflow Blueprint",
            "execution_steps_json": {
                "steps": [
                    {"step_name": "persist_results", "node_name": "writer", "logical_step_kind": "persist"}
                ]
            },
            "runtime_constraints_json": build_runtime_constraints_with_compile_context(
                {
                    "admissibility_status": "review_needed",
                    "admissibility_scope": "current_system_level",
                    "build_admission_decision": "admit_with_review",
                    "primary_execution_regime": "workflow_build",
                    "max_automation_level": "human_gated",
                    "expected_deliverable_class": "report",
                    "confidence": 82,
                    "requires_human_review": True,
                    "reason_codes": ["human_judgment_required"],
                    "summary": "Workflow is admissible with review.",
                    "artifactability": {
                        "level": "partial",
                        "expected_types": ["workflow_spec", "report"],
                        "constraints": ["requires_human_review_before_delivery"]
                    },
                    "workflowability": {
                        "level": "partial_workflow",
                        "regime_candidates": ["workflow_build", "human_gated"],
                        "primary_regime": "workflow_build",
                        "blockers": [],
                        "required_checkpoints": ["Operator review"]
                    },
                    "required_human_checkpoints": [
                        {
                            "checkpoint_id": "review_1",
                            "reason": "Operator review",
                            "related_axes": []
                        }
                    ],
                    "risk_class": "medium",
                    "capability_gaps": [],
                    "environment_requirements": ["workspace_access"],
                    "blocking_assumptions": [],
                    "recommended_next_stage": "build_with_review"
                }
            ),
        },
    )
    assert blueprint_response.status_code == 201
    blueprint = blueprint_response.json()

    build, _ = _run_build(client, project["id"], blueprint["id"])

    package_metadata = build["manifest_snapshot_json"]["metadata"]["execution_package"]
    assert package_metadata["execution_admissibility"]["primary_execution_regime"] == "workflow_build"
    assert package_metadata["execution_admissibility"]["admissibility_status"] == "review_needed"
    assert package_metadata["selected_planner_capability_projections"][0]["capability_id"] == "write_artifact"


def test_execution_package_resolve_run_plan_uses_bound_runtime_entry(client: TestClient, monkeypatch) -> None:
    project, _, _, executable = _build_runnable_package(client, monkeypatch)

    resolve_response = client.post(
        f"/api/v1/executables/{executable['id']}/resolve-run-plan",
        json={
            "run_input": {
                "raw_json": [
                    {"name": "alpha", "score": 3},
                    {"name": "beta", "score": 9},
                    {"name": "gamma", "score": 5}
                ]
            }
        },
    )
    assert resolve_response.status_code == 200
    plan = resolve_response.json()
    assert plan["execution_mode"] == "execution-package"
    assert plan["runtime_entry"]["is_runnable"] is True
    assert plan["package_metadata"]["runtime_entry_ready"] is True
    assert plan["workflow_native_explainability"]["preferred_entry"] == "workflow_native/workflow_artifact.json"
    assert plan["workflow_native_explainability"]["assembled_truth_candidate"] is True
    assert plan["workflow_native_explainability"]["workflow_native_execution_policy_primary_source"] == "workflow_native/workflow_artifact.json"
    assert plan["workflow_native_explainability"]["execution_package_compatibility_view"]["semantic_role"] == "legacy_execution_compatibility_view"
    assert plan["workflow_native_explainability"]["runtime_compatibility_sources"]["steps"]["semantic_role"] == "legacy_sequential_compatibility_source"
    assert plan["workflow_native_explainability"]["compatibility_execution_graph_projection"]["semantic_role"] == "runtime_shadow_compatibility_projection"
    assert plan["workflow_native_explainability"]["workflow_graph_shadow_comparison"]["comparison_mode"] == "workflow_graph_vs_runtime_compatibility_projection"
    assert plan["workflow_native_explainability"]["workflow_graph_shadow_comparison"]["compatibility_projection_present"] is True
    assert plan["workflow_native_explainability"]["workflow_native_shadow_authority"]["shadow_mode"] == "advisory_only"
    assert plan["workflow_native_explainability"]["workflow_native_shadow_authority"]["primary_candidate_source"] == "workflow_native/workflow_artifact.json"
    assert plan["workflow_native_explainability"]["workflow_native_shadow_authority"]["intervention_legality"]["candidate"]["resume_policy"] == "resume_from_current"
    assert plan["workflow_native_explainability"]["workflow_native_shadow_authority"]["rerun_legality"]["compatibility_projection"]["rerun_invalidates_downstream"] is True
    assert plan["workflow_native_explainability"]["workflow_native_shadow_authority"]["human_gate_policy"]["candidate"]["approval_timeout_seconds"] is None
    assert plan["workflow_native_explainability"]["workflow_native_shadow_authority"]["failure_escalation_hints"]["compatibility_projection"]["compatibility_runtime_outcome_on_failure"] in {"waiting_for_intervention", "failed_terminal"}
    assert plan["workflow_native_explainability"]["runtime_authority_map"]["phase"] == "stage5_partial_authority_migration"
    assert plan["workflow_native_explainability"]["runtime_authority_map"]["runtime_policy_authority"]["human_gate_policy"]["status"] == "partially_migrated"
    assert plan["workflow_native_explainability"]["runtime_authority_map"]["runtime_policy_authority"]["rerun_legality_policy"]["consumed_by"] == [
        "run_execution.failure_recovery_options",
        "run_recovery.retry_step_admission",
    ]
    assert plan["workflow_native_explainability"]["runtime_authority_map"]["runtime_policy_authority"]["intervention_legality"]["status"] == "partially_migrated"
    assert plan["workflow_artifact"]["artifact_id"].startswith("workflow-artifact-")
    assert plan["resolved_steps_compatibility_view"]["semantic_role"] == "legacy_execution_compatibility_view"
    assert "steps.json" in plan["resolved_steps_compatibility_view"]["backed_by_sources"]
    assert plan["compatibility_execution_graph_projection"]["graph_id"] == "compatibility_execution_graph_projection_v1"
    assert plan["compatibility_execution_graph_projection"]["projection_source"] == "resolved_steps_and_bindings"
    assert len(plan["resolved_bindings"]) == 3
    assert plan["resolved_steps"][0]["execution_kind"] == "python_function"
    assert plan["resolved_steps"][2]["execution_kind"] == "artifact_writer"
    assert plan["preflight"]["valid"] is True
    assert project["id"]


def test_execution_package_runnable_subset_can_execute_run_end_to_end(client: TestClient, monkeypatch) -> None:
    project, blueprint, _, executable = _build_runnable_package(client, monkeypatch)
    run_response = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable["id"],
            "triggered_by": "v50-test",
            "status": "pending",
            "input_snapshot_json": {
                "raw_json": [
                    {"name": "alpha", "score": 3},
                    {"name": "beta", "score": 9},
                    {"name": "gamma", "score": 5}
                ]
            },
        },
    )
    assert run_response.status_code == 201
    run = run_response.json()

    start_response = client.post(f"/api/v1/runs/{run['id']}/start")
    assert start_response.status_code == 200
    executed_job = execute_job(client, start_response.json()["active_job_id"])
    assert executed_job["status"] == "completed"
    completed_run = wait_for_run_completion(client, run["id"])
    assert completed_run["status"] == "completed"

    steps = client.get(f"/api/v1/runs/{run['id']}/steps").json()
    assert steps[0]["output_payload_json"]["step_type"] == "python_function"
    assert steps[1]["output_payload_json"]["structured_output"]["ranked_candidates"][0]["name"] == "beta"
    assert steps[2]["output_payload_json"]["step_type"] == "artifact_writer"
    artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()
    assert any(item["artifact_type"] == "written_artifact_json" for item in artifacts)


def test_execution_package_marks_template_backed_step_as_non_runnable(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 79,
              "proposed_bindings": [
                {
                  "step_name": "normalize_records",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "run_python_template",
                  "template_execution_kind": "record_normalizer"
                }
              ]
            }
            """,
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[{"step_name": "normalize_records", "node_name": "normalizer", "logical_step_kind": "transform"}],
    )
    build, executable = _run_build(client, project["id"], blueprint["id"])

    package_metadata = build["manifest_snapshot_json"]["metadata"]["execution_package"]
    assert package_metadata["runnable_step_count"] == 0
    assert package_metadata["review_required_step_count"] == 1
    assert build["manifest_snapshot_json"]["metadata"]["workflow_native_sidecar"]["verification_binding_status"] == "partially_bound"
    assert build["manifest_snapshot_json"]["metadata"]["workflow_native_sidecar"]["execution_contract"] == "workflow_native/execution_contract.json"
    assert build["manifest_snapshot_json"]["metadata"]["workflow_native_sidecar"]["build_materialization_record"] == "workflow_native/build_materialization_record.json"
    assert build["manifest_snapshot_json"]["metadata"]["workflow_native_sidecar"]["workflow_artifact"] == "workflow_native/workflow_artifact.json"

    resolve_response = client.post(
        f"/api/v1/executables/{executable['id']}/resolve-run-plan",
        json={"run_input": {}},
    )
    assert resolve_response.status_code == 400
    assert "non-runnable steps" in resolve_response.json()["detail"]
