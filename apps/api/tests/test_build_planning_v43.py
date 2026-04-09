from __future__ import annotations

from fastapi.testclient import TestClient

import app.services.build.build_planning_service as build_planning_service
import app.services.requirement.compiler.llm_assisted as compiler_llm_assisted
from app.schemas.problem_formulation import InputClosureContract, OutputClosureContract, ProblemFormulationContract
from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.solvability import SolvabilityContract
from app.services.provider_runtime import ProviderResponse, ResolvedModelConfig
from app.schemas.workflow_native import ExecutionAdmissibilityContract
from tests.test_phase3_core import create_project, execute_job, wait_for_v2_build_completion


class _SequentialFakeAdapter:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses

    def chat(self, messages):  # noqa: ANN001
        if not self.responses:
            raise AssertionError(f"Unexpected provider call: {messages}")
        content = self.responses.pop(0)
        return ProviderResponse(
            content=content,
            prompt_tokens=24,
            completion_tokens=48,
            total_tokens=72,
            raw_metadata={"mode": "real", "source": "build-planning-test"},
        )


def install_fake_build_planner_provider(monkeypatch, responses: list[str]) -> None:
    queued_responses = list(responses)

    def fake_get_provider_adapter(model_config=None, overrides=None):  # noqa: ANN001, ARG001
        return _SequentialFakeAdapter(queued_responses), ResolvedModelConfig(
            provider="openai-compatible",
            model_name="test-build-model",
            endpoint="https://example.test/v1",
            api_key="test-key",
            temperature=0.2,
            max_tokens=512,
            source="test",
            llm_mode="real",
        )

    monkeypatch.setattr(build_planning_service.settings, "llm_mode", "real")
    monkeypatch.setattr(build_planning_service, "get_provider_adapter", fake_get_provider_adapter)
    monkeypatch.setattr(compiler_llm_assisted, "get_provider_adapter", fake_get_provider_adapter)


def _create_blueprint(client: TestClient, project_id: str, *, steps: list[dict], runtime_constraints_json: dict | None = None) -> dict:
    requirement = client.post(
        "/api/v1/requirements",
        json={"project_id": project_id, "title": "Build Planning Requirement"},
    ).json()
    response = client.post(
        "/api/v1/blueprints",
        json={
            "project_id": project_id,
            "requirement_id": requirement["id"],
            "blueprint_type": "workflow",
            "name": "Build Planning Blueprint",
            "execution_steps_json": {"steps": steps},
            "runtime_constraints_json": runtime_constraints_json or {},
        },
    )
    assert response.status_code == 201
    return response.json()


def build_runtime_constraints_with_compile_context(execution_admissibility: dict) -> dict:
    compile_context = CompileContextResponse(
        selected_blueprint_type="workflow",
        problem_formulation=ProblemFormulationContract(
            interpreted_problem_kind="test_problem",
            target_object="generic_request",
            problem_status="solvable",
            current_system_fit="in_scope",
            planning_intent={
                "intent_id": "generic_problem_envelope",
                "label": "Generic problem",
                "objective_family": "analysis",
            },
        ),
        input_closure=InputClosureContract(target_object="generic_request", closure_status="input_closed"),
        output_closure=OutputClosureContract(deliverable_form="report", closure_status="output_closed"),
        solvability=SolvabilityContract(
            problem_kind="test_problem",
            solvability_status="solvable",
            graph_feasibility={
                "candidate_shape": "linear",
                "representable": True,
                "runtime_supported": True,
                "policy_supported": True,
            },
        ),
        execution_admissibility=ExecutionAdmissibilityContract.model_validate(execution_admissibility),
    )
    return {"compile_context": compile_context.model_dump(mode="python")}


def _run_build(client: TestClient, project_id: str, blueprint_id: str) -> tuple[dict, dict]:
    build_response = client.post(
        "/api/v1/builds",
        json={"project_id": project_id, "blueprint_id": blueprint_id, "build_options_json": {"target": "demo"}},
    )
    assert build_response.status_code == 201
    build = build_response.json()
    start_response = client.post(f"/api/v1/builds/{build['id']}/start")
    assert start_response.status_code == 200
    executed_job = execute_job(client, start_response.json()["active_job_id"])
    assert executed_job["status"] == "completed"
    completed_build = wait_for_v2_build_completion(client, build["id"])
    assert completed_build["status"] == "completed"
    executable = client.get(f"/api/v1/executables/{completed_build['executable_id']}").json()
    return completed_build, executable


def test_build_planning_adds_registry_bound_bindings_to_manifest_and_steps(client: TestClient, monkeypatch):
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 84,
              "proposed_bindings": [
                {
                  "step_name": "collect_inputs",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "fetch_url",
                  "fallback_capability_ids": [],
                  "config_hints": {"source_mode": "url_list"}
                },
                {
                  "step_name": "filter_and_process",
                  "logical_step_kind": "summarize",
                  "selected_capability_id": "summarize_text_llm"
                },
                {
                  "step_name": "publish_outputs",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "write_artifact"
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
            {"step_name": "collect_inputs", "node_name": "collector", "logical_step_kind": "ingest"},
            {"step_name": "filter_and_process", "node_name": "processor", "logical_step_kind": "summarize"},
            {"step_name": "publish_outputs", "node_name": "publisher", "logical_step_kind": "persist"},
        ],
    )

    build, executable = _run_build(client, project["id"], blueprint["id"])

    planning = build["manifest_snapshot_json"]["metadata"]["build_planning"]
    assert planning["used"] is True
    assert planning["referenced_capabilities"] == ["fetch_url", "summarize_text_llm", "write_artifact"]
    assert planning["missing_bindings"] == []
    assert planning["capability_gap_summary"]["total_count"] >= 1
    assert any(item["gap_kind"] == "review_needed" for item in planning["capability_gaps"])
    analyze_step = next(item for item in build["build_steps"] if item["step_name"] == "analyze_blueprint")
    assert analyze_step["output_payload_json"]["build_planning"]["used"] is True
    steps_content_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "steps.json"},
    )
    assert steps_content_response.status_code == 200
    content = steps_content_response.json()["content"]
    assert '"selected_capability_id": "fetch_url"' in content
    assert '"selected_capability_id": "summarize_text_llm"' in content
    assert '"selected_capability_id": "write_artifact"' in content


def test_build_planning_rejects_unsupported_bindings_without_corrupting_build(client: TestClient, monkeypatch):
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 71,
              "proposed_bindings": [
                {
                  "step_name": "filter_and_process",
                  "logical_step_kind": "summarize",
                  "selected_capability_id": "made_up_capability"
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
            {"step_name": "collect_inputs", "node_name": "collector", "logical_step_kind": "ingest"},
            {"step_name": "filter_and_process", "node_name": "processor", "logical_step_kind": "summarize"},
            {"step_name": "publish_outputs", "node_name": "publisher", "logical_step_kind": "persist"},
        ],
    )

    build, executable = _run_build(client, project["id"], blueprint["id"])

    planning = build["manifest_snapshot_json"]["metadata"]["build_planning"]
    assert planning["used"] is False
    assert planning["unsupported_bindings"] == ["filter_and_process:made_up_capability"]
    assert planning["missing_bindings"][0]["reason"] == "unsupported_selected_capability"
    assert planning["capability_gaps"][0]["gap_kind"] == "unsupported_binding"
    assert planning["capability_gaps"][0]["alternative_capabilities"][0]["capability_id"] == "summarize_text_llm"
    steps_content_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "steps.json"},
    )
    assert steps_content_response.status_code == 200
    assert '"selected_capability_id": "made_up_capability"' not in steps_content_response.json()["content"]


def test_build_planning_marks_template_backed_capability_for_manual_review(client: TestClient, monkeypatch):
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 77,
              "proposed_bindings": [
                {
                  "step_name": "filter_and_process",
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
        steps=[
            {"step_name": "collect_inputs", "node_name": "collector", "logical_step_kind": "ingest"},
            {"step_name": "filter_and_process", "node_name": "processor", "logical_step_kind": "transform"},
            {"step_name": "publish_outputs", "node_name": "publisher", "logical_step_kind": "persist"},
        ],
    )

    build, executable = _run_build(client, project["id"], blueprint["id"])

    planning = build["manifest_snapshot_json"]["metadata"]["build_planning"]
    assert planning["used"] is True
    assert planning["review_required_step_names"] == ["filter_and_process"]
    assert any("template-backed" in item for item in planning["warnings"])
    assert any(item["gap_kind"] == "template_capability_unrealized" for item in planning["capability_gaps"])
    steps_content_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "steps.json"},
    )
    assert steps_content_response.status_code == 200
    content = steps_content_response.json()["content"]
    assert '"selected_capability_id": "run_python_template"' in content
    assert '"manual_review_required": true' in content
    assert '"template_execution_kind": "record_normalizer"' in content


def test_build_planning_applies_admissibility_guidance_without_blocking_build(client: TestClient, monkeypatch):
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 80,
              "proposed_bindings": [
                {
                  "step_name": "publish_outputs",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "write_artifact"
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
            {"step_name": "publish_outputs", "node_name": "publisher", "logical_step_kind": "persist"},
        ],
        runtime_constraints_json=build_runtime_constraints_with_compile_context(
            {
                "admissibility_status": "review_needed",
                "admissibility_scope": "current_system_level",
                "build_admission_decision": "admit_with_review",
                "primary_execution_regime": "analysis_only",
                "max_automation_level": "plan_only",
                "expected_deliverable_class": "report",
                "confidence": 71,
                "requires_human_review": True,
                "reason_codes": ["human_judgment_required"],
                "summary": "Proceed in advisory mode.",
                "artifactability": {"level": "partial", "expected_types": ["report"], "constraints": []},
                "workflowability": {
                    "level": "plan_only",
                    "regime_candidates": ["analysis_only", "workflow_build"],
                    "primary_regime": "analysis_only",
                    "blockers": [],
                    "required_checkpoints": ["Operator review"]
                },
                "required_human_checkpoints": [
                    {"checkpoint_id": "review_1", "reason": "Operator review", "related_axes": []}
                ],
                "risk_class": "medium",
                "capability_gaps": [],
                "environment_requirements": [],
                "blocking_assumptions": [],
                "recommended_next_stage": "build_with_review"
            }
        ),
    )

    build, _ = _run_build(client, project["id"], blueprint["id"])

    planning = build["manifest_snapshot_json"]["metadata"]["build_planning"]
    assert planning["used"] is True
    assert planning["admissibility_guidance_applied"] is True
    assert planning["primary_execution_regime"] == "analysis_only"
    assert planning["max_automation_level"] == "plan_only"
    assert planning["planner_capability_subset_applied"] is True
    assert planning["planner_capability_subset"]["primary_execution_regime"] == "analysis_only"
    assert planning["planner_capability_subset"]["step_kind_preferences"]["persist"]["preferred_capability_ids"]
    assert any("advisory mode" in item for item in planning["admissibility_warnings"])
    assert any("does not include the current admissibility regime" in item for item in planning["admissibility_warnings"])
