"""Tests for WorkflowPlannerService.

Covers:
- Planner produces valid output from a blueprint
- Planner respects depends_on for DAG structure
- Planner identifies capability gaps as unresolved items
- Planner output is stored in build artifacts
- Planner output is consumable by build pipeline
- Linear vs DAG draft distinction
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.database import get_db
from app.db.models import Blueprint
from app.main import app as _app
from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    PlanningIntentContract,
    ProblemFormulationContract,
)
from app.schemas.requirement_compiler import (
    CompileContextCapabilityEnvelopeResponse,
    CompileContextConstraintsResponse,
    CompileContextResponse,
    CompileContextWorkUnitResponse,
)
from app.schemas.solvability import CapabilitySignature, GraphFeasibility, SolvabilityContract
from app.schemas.workflow_native import ExecutionAdmissibilityContract
from app.schemas.workflow_planner import WorkflowPlannerOutput
from app.services.capabilities.registry import CapabilityRegistryService
from app.services.workflow_planner_service import WorkflowPlannerService
import app.services.job.job_execution_service as job_execution_service
from tests.test_phase3_core import (
    create_project,
    execute_job,
    wait_for_v2_build_completion,
)


def _get_test_db():
    """Get a DB session using conftest's override (SQLite) if available, else real get_db."""
    override = _app.dependency_overrides.get(get_db)
    return next(override()) if override else next(get_db())

def _compile_context_for_steps(steps: list[dict], *, target_object: str = "generic_request", human_review_required: bool = False) -> dict[str, Any]:
    registry = CapabilityRegistryService()
    all_caps = registry.list_capabilities(context="build_planning", include_disabled=False)
    step_kinds = {
        str(step.get("logical_step_kind") or "").strip()
        for step in steps
        if isinstance(step, dict) and str(step.get("logical_step_kind") or "").strip()
    }
    seen_ids: set[str] = set()
    signatures = []
    for kind in sorted(step_kinds):
        matching = [cap for cap in all_caps if kind in (cap.supported_step_kinds or [])]
        for cap in matching:
            if cap.capability_id in seen_ids:
                continue
            seen_ids.add(cap.capability_id)
            projection = registry.build_planner_capability_projection(cap.capability_id)
            signatures.append(
                CapabilitySignature(
                    capability_id=cap.capability_id,
                    logical_step_kinds=list(cap.supported_step_kinds or []),
                    accepted_input_kinds=[port.semantic_role for port in projection.input_ports],
                    produced_output_kinds=[port.semantic_role for port in projection.output_ports],
                    side_effect_class=projection.side_effect_class or "none",
                    access_boundary=(
                        "remote"
                        if "network_access" in projection.runtime_constraints
                        else "workspace"
                        if "workspace_access" in projection.runtime_constraints
                        else "unknown"
                    ),
                    human_gate_requirement=(
                        "required" if (projection.requires_review or projection.requires_confirmation) else "none"
                    ),
                    graph_support=["linear", "dag"],
                    runtime_adapter_kind=cap.runtime_adapter or "unknown",
                )
            )

    execution_admissibility = ExecutionAdmissibilityContract(
        admissibility_status="review_needed" if human_review_required else "supported",
        build_admission_decision="admit_with_review" if human_review_required else "admit",
        primary_execution_regime="workflow_build",
        max_automation_level="full_workflow",
        expected_deliverable_class="report",
        requires_human_review=human_review_required,
    )
    compile_context = CompileContextResponse(
        selected_blueprint_type="workflow",
        graph_closure_status="graph_proposable",
        selected_template_id="workflow.test_template",
        problem_formulation=ProblemFormulationContract(
            interpreted_problem_kind="test_problem",
            target_object=target_object,
            problem_status="solvable",
            current_system_fit="in_scope",
            planning_intent=PlanningIntentContract(
                intent_id="test_intent",
                label="Test intent",
                target_object=target_object,
                deliverable_contract="test_contract",
                objective_family="analysis",
                preferred_graph_shape="dag" if any(step.get("depends_on") for step in steps if isinstance(step, dict)) else "linear",
                parallelism_hint="available" if any(step.get("depends_on") for step in steps if isinstance(step, dict)) else "none",
                acquisition_strategy="direct_read",
                evidence_strategy="generic",
                side_effect_budget="read_mostly",
            ),
        ),
        input_closure=InputClosureContract(
            target_object=target_object,
            planning_intent_id="test_intent",
            closure_status="input_closed",
        ),
        output_closure=OutputClosureContract(
            deliverable_form="report",
            closure_status="output_closed",
        ),
        solvability=SolvabilityContract(
            problem_kind="test_problem",
            planning_intent_id="test_intent",
            solvability_status="solvable_with_review" if human_review_required else "solvable",
            graph_feasibility=GraphFeasibility(
                candidate_shape="dag" if any(step.get("depends_on") for step in steps if isinstance(step, dict)) else "linear",
                representable=True,
                runtime_supported=True,
                policy_supported=True,
            ),
        ),
        semantic_core={"target_object": target_object, "deliverable_form": "report"},
        execution_admissibility=execution_admissibility,
        capability_envelope=CompileContextCapabilityEnvelopeResponse(
            capability_signatures=signatures,
        ),
        compile_constraints=CompileContextConstraintsResponse(
            allowed_node_semantic_types=["acquire", "transform", "act", "verify", "human_gate"],
            require_human_gate_before=["act"] if human_review_required else [],
        ),
        candidate_work_units=[
            CompileContextWorkUnitResponse(
                work_unit_id=f"work_unit_{index}",
                step_name=str(step.get("step_name") or ""),
                label=step.get("node_name"),
                objective=step.get("purpose") or step.get("output_intent"),
                logical_step_kind=step.get("logical_step_kind"),
                required_capability_classes=[
                    str(step.get("logical_step_kind"))
                ] if str(step.get("logical_step_kind") or "").strip() else [],
                required_inputs=[
                    str(step.get("input_intent"))
                ] if str(step.get("input_intent") or "").strip() else [],
                expected_outputs=[
                    str(step.get("output_intent"))
                ] if str(step.get("output_intent") or "").strip() else [],
                dependency_hints=[
                    f"work_unit_{steps.index(next_step) + 1}"
                    for next_step in steps
                    if isinstance(next_step, dict)
                    and str(next_step.get("step_name") or "") in {str(item) for item in (step.get("depends_on") or []) if str(item)}
                ],
                parallelism_hint="available" if step.get("depends_on") else "none",
            )
            for index, step in enumerate(steps, start=1)
            if isinstance(step, dict)
        ],
    )
    return compile_context.model_dump(mode="python")


def _create_blueprint_in_db(client: TestClient, project_id: str, steps: list[dict]) -> Blueprint:
    """Create a blueprint via API and load it from DB."""
    requirement = client.post(
        "/api/v1/requirements",
        json={"project_id": project_id, "title": "Planner Test Req"},
    ).json()
    bp_resp = client.post(
        "/api/v1/blueprints",
        json={
            "project_id": project_id,
            "requirement_id": requirement["id"],
            "blueprint_type": "workflow",
            "name": "Planner Test Blueprint",
            "execution_steps_json": {"steps": steps},
            "runtime_constraints_json": {
                "compile_context": _compile_context_for_steps(steps),
                "semantic_interpretation": {"semantic_core": {"target_object": "generic_request"}},
            },
        },
    ).json()
    db = _get_test_db()
    try:
        bp = db.query(Blueprint).filter(Blueprint.id == bp_resp["id"]).first()
        assert bp is not None
        return bp
    finally:
        db.close()


def _read_executable_file(client: TestClient, executable_id: str, path: str) -> dict[str, Any]:
    """Read a file from the executable bundle via the API."""
    resp = client.get(f"/api/v1/executables/{executable_id}/files/content", params={"path": path})
    if resp.status_code != 200:
        return {}
    data = resp.json()
    content = data.get("content", "")
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Unit tests for WorkflowPlannerService
# ---------------------------------------------------------------------------


class TestPlannerServiceUnit:

    def test_linear_plan_produces_valid_output(self, client: TestClient):
        """Planner produces a valid WorkflowPlannerOutput for linear steps."""
        project = create_project(client)
        bp = _create_blueprint_in_db(client, project["id"], [
            {"step_name": "collect", "node_name": "collector", "logical_step_kind": "acquire"},
            {"step_name": "analyze", "node_name": "analyzer", "logical_step_kind": "transform"},
            {"step_name": "report", "node_name": "reporter", "logical_step_kind": "act"},
        ])
        planner = WorkflowPlannerService()
        output = planner.plan(bp)

        assert isinstance(output, WorkflowPlannerOutput)
        assert output.planner_version == "workflow_planner_v1"
        assert len(output.node_drafts) == 3
        assert len(output.edge_drafts) == 2  # linear: collect->analyze, analyze->report
        assert len(output.entry_node_ids) == 1
        assert len(output.exit_node_ids) == 1
        assert output.draft_status in ("complete", "review_required", "partial")

    def test_dag_plan_produces_correct_edges(self, client: TestClient):
        """Planner produces DAG edges from depends_on."""
        project = create_project(client)
        bp = _create_blueprint_in_db(client, project["id"], [
            {"step_name": "root", "node_name": "root", "logical_step_kind": "acquire"},
            {"step_name": "left", "node_name": "left", "logical_step_kind": "transform", "depends_on": ["root"]},
            {"step_name": "right", "node_name": "right", "logical_step_kind": "transform", "depends_on": ["root"]},
            {"step_name": "join", "node_name": "join", "logical_step_kind": "transform", "depends_on": ["left", "right"]},
        ])
        planner = WorkflowPlannerService()
        output = planner.plan(bp)

        assert len(output.node_drafts) == 4
        assert len(output.edge_drafts) == 4  # root->left, root->right, left->join, right->join
        assert len(output.entry_node_ids) == 1  # root
        assert len(output.exit_node_ids) == 1  # join

    def test_planner_identifies_capability_gaps(self, client: TestClient):
        """Planner flags unresolved items for unknown step kinds."""
        project = create_project(client)
        bp = _create_blueprint_in_db(client, project["id"], [
            {"step_name": "magic", "node_name": "wizard", "logical_step_kind": "quantum_teleport"},
        ])
        planner = WorkflowPlannerService()
        output = planner.plan(bp)

        assert len(output.node_drafts) == 1
        assert len(output.unresolved_items) >= 1
        gap_item = output.unresolved_items[0]
        assert gap_item.item_kind == "capability_gap"
        assert "quantum_teleport" in gap_item.description

    def test_planner_extracts_compile_context(self, client: TestClient):
        """Planner extracts compile context from blueprint runtime_constraints."""
        project = create_project(client)
        bp = _create_blueprint_in_db(client, project["id"], [
            {"step_name": "s1", "node_name": "n1", "logical_step_kind": "acquire"},
        ])
        db = _get_test_db()
        try:
            bp_db = db.query(Blueprint).filter(Blueprint.id == bp.id).first()
            rtc = dict(bp_db.runtime_constraints_json or {})
            compile_context = _compile_context_for_steps(
                [{"step_name": "s1", "node_name": "n1", "logical_step_kind": "acquire"}],
                target_object="generic_request",
            )
            compile_context["semantic_core"].update(
                {
                    "analysis_frame": "comparative",
                    "workflow_shape": "linear_pipeline",
                }
            )
            rtc["compile_context"] = compile_context
            bp_db.runtime_constraints_json = rtc
            db.commit()
            db.refresh(bp_db)
            planner = WorkflowPlannerService()
            compile_context = planner._extract_compile_context(bp_db)
            assert compile_context is not None
            assert compile_context.output_closure.deliverable_form == "report"
            assert compile_context.semantic_core["analysis_frame"] == "comparative"
            assert compile_context.semantic_core["workflow_shape"] == "linear_pipeline"
            assert len(compile_context.candidate_work_units) == 1
        finally:
            db.close()

    def test_planner_respects_human_review_constraint(self, client: TestClient):
        """Planner marks human gate when human_review_required is set."""
        project = create_project(client)
        bp = _create_blueprint_in_db(client, project["id"], [
            {"step_name": "do_thing", "node_name": "actor", "logical_step_kind": "act"},
        ])
        db = _get_test_db()
        try:
            bp_db = db.query(Blueprint).filter(Blueprint.id == bp.id).first()
            rtc = dict(bp_db.runtime_constraints_json or {})
            rtc["compile_context"] = _compile_context_for_steps(
                [{"step_name": "do_thing", "node_name": "actor", "logical_step_kind": "act"}],
                human_review_required=True,
            )
            bp_db.runtime_constraints_json = rtc
            db.commit()
            db.refresh(bp_db)
            planner = WorkflowPlannerService()
            output = planner.plan(bp_db)
            act_node = next(n for n in output.node_drafts if n.step_name == "do_thing")
            assert act_node.requires_human_gate is True
        finally:
            db.close()

    def test_planner_trace_contains_metadata(self, client: TestClient):
        """Planner output has populated planner_trace."""
        project = create_project(client)
        bp = _create_blueprint_in_db(client, project["id"], [
            {"step_name": "s1", "node_name": "n1", "logical_step_kind": "acquire"},
        ])
        output = WorkflowPlannerService().plan(bp)
        assert output.planner_trace.get("planner_version") == "workflow_planner_v1"
        assert "has_dependency_hints" in output.planner_trace

    def test_planner_uses_work_unit_id_as_identity(self, client: TestClient):
        """Planner should derive graph identity from work_unit_id, not step_name."""
        project = create_project(client)
        bp = _create_blueprint_in_db(client, project["id"], [
            {"step_name": "legacy_root", "node_name": "root", "logical_step_kind": "acquire"},
            {"step_name": "legacy_join", "node_name": "join", "logical_step_kind": "transform"},
        ])
        db = _get_test_db()
        try:
            bp_db = db.query(Blueprint).filter(Blueprint.id == bp.id).first()
            rtc = dict(bp_db.runtime_constraints_json or {})
            compile_context = _compile_context_for_steps([
                {"step_name": "legacy_root", "node_name": "root", "logical_step_kind": "acquire"},
                {"step_name": "legacy_join", "node_name": "join", "logical_step_kind": "transform"},
            ])
            compile_context["candidate_work_units"] = [
                {
                    "work_unit_id": "collect_sources",
                    "step_name": "legacy_root",
                    "label": "root",
                    "objective": "collect source material",
                    "logical_step_kind": "acquire",
                    "required_capability_classes": ["acquire"],
                    "required_inputs": [],
                    "expected_outputs": ["sources"],
                    "dependency_hints": [],
                    "parallelism_hint": "none",
                    "human_gate_hint": None,
                },
                {
                    "work_unit_id": "merge_findings",
                    "step_name": "legacy_join",
                    "label": "join",
                    "objective": "merge source findings",
                    "logical_step_kind": "transform",
                    "required_capability_classes": ["transform"],
                    "required_inputs": ["sources"],
                    "expected_outputs": ["report"],
                    "dependency_hints": ["collect_sources"],
                    "parallelism_hint": "none",
                    "human_gate_hint": None,
                },
            ]
            rtc["compile_context"] = compile_context
            bp_db.runtime_constraints_json = rtc
            db.commit()
            db.refresh(bp_db)

            output = WorkflowPlannerService().plan(bp_db)
            assert len(output.edge_drafts) == 1
            merge_node = next(n for n in output.node_drafts if n.step_name == "legacy_join")
            collect_node = next(n for n in output.node_drafts if n.step_name == "legacy_root")
            assert merge_node.upstream_dependencies == [collect_node.draft_node_id]
        finally:
            db.close()

    def test_empty_steps_produces_blocked_output(self, client: TestClient):
        """Planner produces blocked output for blueprint with no steps."""
        project = create_project(client)
        bp = _create_blueprint_in_db(client, project["id"], [])
        output = WorkflowPlannerService().plan(bp)
        assert output.draft_status == "blocked"
        assert len(output.unresolved_items) >= 1


# ---------------------------------------------------------------------------
# Integration: planner output stored in build artifacts
# ---------------------------------------------------------------------------


class TestPlannerBuildIntegration:

    def test_build_stores_planner_output_file(self, client: TestClient):
        """Build pipeline stores planner_output.json in the executable bundle."""
        project = create_project(client)
        requirement = client.post(
            "/api/v1/requirements",
            json={"project_id": project["id"], "title": "Planner Build Req"},
        ).json()
        blueprint = client.post(
            "/api/v1/blueprints",
            json={
                "project_id": project["id"],
                "requirement_id": requirement["id"],
                "blueprint_type": "workflow",
                "name": "Planner Build Blueprint",
                "execution_steps_json": {
                    "steps": [
                        {"step_name": "gather", "node_name": "g", "logical_step_kind": "acquire"},
                        {"step_name": "process", "node_name": "p", "logical_step_kind": "transform"},
                    ]
                },
                "runtime_constraints_json": {
                    "compile_context": _compile_context_for_steps([
                        {"step_name": "gather", "node_name": "g", "logical_step_kind": "acquire"},
                        {"step_name": "process", "node_name": "p", "logical_step_kind": "transform"},
                    ]),
                    "semantic_interpretation": {"semantic_core": {"target_object": "generic_request"}},
                },
            },
        ).json()
        build = client.post(
            "/api/v1/builds",
            json={"project_id": project["id"], "blueprint_id": blueprint["id"], "build_options_json": {}},
        ).json()
        started = client.post(f"/api/v1/builds/{build['id']}/start").json()
        execute_job(client, started["active_job_id"])
        completed = wait_for_v2_build_completion(client, build["id"])
        executable = client.get(f"/api/v1/executables/{completed['executable_id']}").json()

        planner_data = _read_executable_file(client, executable["id"], "workflow_native/planner_output.json")
        assert planner_data, "planner_output.json should exist in executable bundle"
        # planner_version may be deterministic or LLM mock (if linear upgrade applied)
        assert "workflow_planner" in planner_data.get("planner_version", "")
        assert len(planner_data.get("node_drafts", [])) == 2
        assert len(planner_data.get("edge_drafts", [])) == 1  # linear

    def test_build_planner_output_matches_dag_graph(self, client: TestClient):
        """Planner output DAG structure matches the built workflow_graph."""
        project = create_project(client)
        requirement = client.post(
            "/api/v1/requirements",
            json={"project_id": project["id"], "title": "Planner DAG Match Req"},
        ).json()
        blueprint = client.post(
            "/api/v1/blueprints",
            json={
                "project_id": project["id"],
                "requirement_id": requirement["id"],
                "blueprint_type": "workflow",
                "name": "Planner DAG Match Blueprint",
                "execution_steps_json": {
                    "steps": [
                        {"step_name": "root", "node_name": "root", "logical_step_kind": "acquire"},
                        {"step_name": "left", "node_name": "left", "logical_step_kind": "transform", "depends_on": ["root"]},
                        {"step_name": "right", "node_name": "right", "logical_step_kind": "transform", "depends_on": ["root"]},
                        {"step_name": "merge", "node_name": "merge", "logical_step_kind": "transform", "depends_on": ["left", "right"]},
                    ]
                },
                "runtime_constraints_json": {
                    "compile_context": _compile_context_for_steps([
                        {"step_name": "root", "node_name": "root", "logical_step_kind": "acquire"},
                        {"step_name": "left", "node_name": "left", "logical_step_kind": "transform", "depends_on": ["root"]},
                        {"step_name": "right", "node_name": "right", "logical_step_kind": "transform", "depends_on": ["root"]},
                        {"step_name": "merge", "node_name": "merge", "logical_step_kind": "transform", "depends_on": ["left", "right"]},
                    ]),
                    "semantic_interpretation": {"semantic_core": {"target_object": "generic_request"}},
                },
            },
        ).json()
        build = client.post(
            "/api/v1/builds",
            json={"project_id": project["id"], "blueprint_id": blueprint["id"], "build_options_json": {}},
        ).json()
        started = client.post(f"/api/v1/builds/{build['id']}/start").json()
        execute_job(client, started["active_job_id"])
        completed = wait_for_v2_build_completion(client, build["id"])
        executable = client.get(f"/api/v1/executables/{completed['executable_id']}").json()

        planner_data = _read_executable_file(client, executable["id"], "workflow_native/planner_output.json")
        wg_data = _read_executable_file(client, executable["id"], "workflow_native/workflow_graph.json")

        # Both should have 4 nodes and 4 edges
        assert len(planner_data.get("node_drafts", [])) == 4
        assert len(planner_data.get("edge_drafts", [])) == 4
        assert len(wg_data.get("node_instances", [])) == 4
        assert len(wg_data.get("edge_set", [])) == 4

        # Both should have same entry/exit structure
        assert len(planner_data.get("entry_node_ids", [])) == 1
        assert len(planner_data.get("exit_node_ids", [])) == 1
        assert len(wg_data.get("entry_nodes", [])) == 1
        assert len(wg_data.get("exit_nodes", [])) == 1
