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
