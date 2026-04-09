from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.schemas.capability_registry import (
    CapabilityPlanningGraphEdgeResponse,
    CapabilityPlanningGraphNodeResponse,
)
from app.services.capabilities import CapabilityRegistryService
from tests.test_capability_materialization_v53 import _activate_and_materialize_internal_capability
from tests.test_requirement_compiler_v251 import create_project, create_requirement


def test_compile_proposes_research_briefing_planning_graph(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Research Briefing Workflow",
        goal="Create a workflow that collects bounded research inputs and produces an engineering research briefing.",
        input_description="Use bounded search results as the source material.",
        output_description="A markdown research briefing.",
        domain="research",
        acceptance_criteria_json={"items": ["Output is a concise research briefing artifact."]},
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["explainability"]["inferred_deliverable_intent"] == "research_briefing"
    steps = payload["blueprint_draft"]["execution_steps_json"]["steps"]
    assert [item["step_name"] for item in steps] == [
        "collect_research_sources",
        "synthesize_research_briefing",
        "render_research_briefing",
        "persist_research_briefing",
    ]
    assert steps[0]["semantic_role"] == "evidence_source"
    assert steps[1]["semantic_role"] == "synthesis"
    assert steps[2]["semantic_role"] == "render"
    assert steps[3]["semantic_role"] == "delivery"
    assert steps[3]["planning_dependencies"] == [
        {
            "from_step_id": "render_research_briefing",
            "to_step_id": "persist_research_briefing",
            "evidence_type": "research.brief.rendered",
        }
    ]

    step_planning = payload["explainability"]["step_planning"]
    assert step_planning["planning_graph_validation"]["decision"] == "accepted"
    assert step_planning["planning_graph_validation"]["cycle_detected"] is False
    assert step_planning["planning_graph_edges"] == [
        {
            "from_step_id": "collect_research_sources",
            "to_step_id": "synthesize_research_briefing",
            "evidence_type": "research.sources",
        },
        {
            "from_step_id": "synthesize_research_briefing",
            "to_step_id": "render_research_briefing",
            "evidence_type": "research.brief.draft",
        },
        {
            "from_step_id": "render_research_briefing",
            "to_step_id": "persist_research_briefing",
            "evidence_type": "research.brief.rendered",
        },
    ]


def test_compile_repository_analysis_exposes_explicit_dag_planning_graph(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Report",
        goal="Analyze this repository and produce a grounded architecture report.",
        input_description="A repository root inside the workspace: ./fixtures/sample_repo",
        output_description="A markdown architecture report and repository evidence bundle.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["Report includes evidence-backed conclusions."]},
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    step_planning = payload["explainability"]["step_planning"]
    assert step_planning["planning_graph_validation"]["decision"] == "accepted"
    assert step_planning["planning_graph_validation"]["cycle_detected"] is False
    assert any(item["semantic_role"] == "evidence_bundle" for item in step_planning["planning_graph_nodes"])
    assert any(item["semantic_role"] == "quality_gate" for item in step_planning["planning_graph_nodes"])

    steps = payload["blueprint_draft"]["execution_steps_json"]["steps"]
    validate_step = next(item for item in steps if item["step_name"] == "validate_architecture_report")
    assert validate_step["planning_dependencies"] == [
        {
            "from_step_id": "render_architecture_report",
            "to_step_id": "validate_architecture_report",
            "evidence_type": "report.architecture.rendered",
        },
        {
            "from_step_id": "draft_architecture_report_brief",
            "to_step_id": "validate_architecture_report",
            "evidence_type": "report.architecture.brief",
        },
    ]
    delivery_step = next(item for item in steps if item["step_name"] == "write_architecture_report_artifact")
    assert delivery_step["planning_dependencies"] == [
        {
            "from_step_id": "validate_architecture_report",
            "to_step_id": "write_architecture_report_artifact",
            "evidence_type": "report.architecture.validated",
        }
    ]


def test_compile_repository_analysis_marks_ambiguous_source_graph_review_needed(client) -> None:
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Repository Architecture Report",
        goal="Analyze this repository and generate an architecture report for engineering review.",
        input_description="This repository.",
        output_description="A markdown architecture report.",
        domain="developer tooling",
        acceptance_criteria_json={"items": ["Ambiguous repository sources should be surfaced honestly."]},
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["blueprint_draft"]["template_tags_json"]["repository_source_modality"] == "ambiguous_source"
    assert payload["explainability"]["step_planning"]["planning_graph_validation"]["decision"] == "review_needed"
    issues = payload["explainability"]["step_planning"]["planning_graph_validation"]["issues"]
    assert any(item["issue_kind"] == "source_review_needed" for item in issues)


def test_materialized_reusable_capability_can_be_planning_visible_in_graph_proposal() -> None:
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        _activate_and_materialize_internal_capability(
            db,
            project_id="project-1",
            gap_kind="missing_binding",
            logical_step_kind="persist",
            requested_capability="write_artifact",
            reason="Need a reviewed reusable project artifact writer for planning visibility.",
        )
        registry = CapabilityRegistryService(db, project_id="project-1")
        proposal = registry.build_typed_workflow_proposal(
            "research_briefing",
            context="blueprint_planning",
            normalized_requirement={
                "title": "Research Briefing",
                "goal": "Produce a research briefing.",
                "output_description": "A markdown research briefing.",
                "domain": "research",
            },
        )

        assert proposal is not None
        persist_step = next(step for step in proposal.steps if step.step_id == "persist_research_briefing")
        assert persist_step.selected_reusable_capability_id is not None
        assert persist_step.selected_reusable_capability_id in persist_step.recommended_reusable_capability_ids
        assert proposal.validation.decision == "accepted"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_cycle_detection_rejects_manual_planning_cycle() -> None:
    registry = CapabilityRegistryService()
    nodes = [
        CapabilityPlanningGraphNodeResponse(
            step_id="a",
            order=1,
            node_name="a",
            logical_step_kind="transform",
            capability_id="parse_json",
            semantic_role="evidence_transform",
        ),
        CapabilityPlanningGraphNodeResponse(
            step_id="b",
            order=2,
            node_name="b",
            logical_step_kind="transform",
            capability_id="parse_json",
            semantic_role="evidence_transform",
        ),
    ]
    edges = [
        CapabilityPlanningGraphEdgeResponse(from_step_id="a", to_step_id="b", evidence_type="x"),
        CapabilityPlanningGraphEdgeResponse(from_step_id="b", to_step_id="a", evidence_type="y"),
    ]

    assert registry._graph_has_cycle(nodes=nodes, edges=edges) is True  # noqa: SLF001
