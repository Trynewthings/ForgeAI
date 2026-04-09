from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.schemas.capability_registry import CapabilityProposalReviewUpdateRequest
from app.services.capabilities import (
    CapabilityLifecycleService,
    CapabilityProposalService,
    CapabilityRegistryService,
    CapabilityRequestService,
    CapabilityResolutionService,
)
from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_phase3_core import create_project


def test_resolution_service_auto_generates_internal_proposal_and_folds_role_views() -> None:
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        registry = CapabilityRegistryService()
        request_service = CapabilityRequestService(db)
        lifecycle_service = CapabilityLifecycleService(db)
        proposal_service = CapabilityProposalService(db)
        resolution_service = CapabilityResolutionService(db)

        gap = registry.build_gap_response(
            gap_kind="missing_binding",
            stage="build_planning",
            logical_step_kind="rank",
            requested_capability_id_or_name="internal_ranker_helper",
            reason="No internal helper is registered for this ranking path.",
        )
        request = request_service.ensure_requests_for_gaps(project_id="project-1", gaps=[gap])[0]
        request_service.update_review_state(
            request.request_id,
            review_status="approved",
            review_notes=["Approved for internal helper lifecycle conversion."],
        )
        lifecycle_service.convert_request(request.request_id)

        resolution = resolution_service.build_role_views(project_id="project-1")

        assert proposal_service.list_project_proposals("project-1")
        assert resolution.end_user.resolution_state == "proposal_ready_for_review"
        assert resolution.end_user.detailed_governance_refs == []
        assert resolution.end_user.hidden_detail_count >= 3
        assert resolution.operator.resolution_state == "proposal_ready_for_review"
        assert resolution.operator.requires_operator_action is False
        assert resolution.operator.requires_capability_owner_action is True
        assert resolution.operator.auto_progress_notes
        assert all(ref.ref_kind == "request" for ref in resolution.operator.detailed_governance_refs)
        owner_ref_kinds = {ref.ref_kind for ref in resolution.capability_owner.detailed_governance_refs}
        assert {"request", "proposal", "lifecycle_entry"} <= owner_ref_kinds
        refreshed_request = request_service.get_request(request.request_id)
        assert refreshed_request.linked_proposal_id is not None
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_resolution_service_reports_ready_pending_activation_after_proposal_approval() -> None:
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        registry = CapabilityRegistryService()
        request_service = CapabilityRequestService(db)
        lifecycle_service = CapabilityLifecycleService(db)
        proposal_service = CapabilityProposalService(db)
        resolution_service = CapabilityResolutionService(db)

        gap = registry.build_gap_response(
            gap_kind="missing_binding",
            stage="build_planning",
            logical_step_kind="rank",
            requested_capability_id_or_name="internal_ranker_helper",
            reason="No internal helper is registered for this ranking path.",
        )
        request = request_service.ensure_requests_for_gaps(project_id="project-1", gaps=[gap])[0]
        request_service.update_review_state(
            request.request_id,
            review_status="approved",
            review_notes=["Approved for proposal path."],
        )
        lifecycle_service.convert_request(request.request_id)
        proposal = proposal_service.generate_for_request(request.request_id)
        proposal_service.update_review_state(
            proposal.proposal_id,
            CapabilityProposalReviewUpdateRequest(
                proposal_status="approved",
                review_notes=["Approved and waiting for explicit activation."],
            ),
        )

        resolution = resolution_service.build_role_views(project_id="project-1")

        assert resolution.end_user.resolution_state == "ready_pending_activation"
        assert resolution.end_user.platform_action_taken is True
        assert resolution.operator.resolution_state == "ready_pending_activation"
        assert resolution.operator.requires_capability_owner_action is True
        assert any(action.action_key == "activate_capability" for action in resolution.capability_owner.next_actions)
        activation_refs = [
            ref for ref in resolution.capability_owner.detailed_governance_refs if ref.ref_kind == "activation"
        ]
        assert activation_refs
        assert activation_refs[0].status == "eligible"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_project_summary_and_build_metadata_expose_external_resolution_concisely(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "no_changes",
              "confidence": 49
            }
            """,
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {
                "step_name": "ingest_incidents",
                "node_name": "collector",
                "logical_step_kind": "ingest",
                "required_capabilities": ["slack_connector"],
            },
        ],
    )

    build, _ = _run_build(client, project["id"], blueprint["id"])

    build_resolution = build["manifest_snapshot_json"]["metadata"]["build_planning"]["capability_resolution"]
    assert build_resolution["end_user"]["resolution_state"] == "blocked_requires_external_setup"
    assert build_resolution["end_user"]["detailed_governance_refs"] == []
    assert build_resolution["operator"]["requires_operator_action"] is True

    summary_response = client.get(f"/api/v1/projects/{project['id']}/capability-requests/summary")
    assert summary_response.status_code == 200
    payload = summary_response.json()
    resolution = payload["resolution"]
    assert resolution["end_user"]["resolution_state"] == "blocked_requires_external_setup"
    assert resolution["end_user"]["headline"] == "Capability support depends on external setup"
    assert resolution["end_user"]["requires_user_action"] is False
    assert resolution["operator"]["requires_operator_action"] is True
    assert resolution["operator"]["detailed_governance_refs"][0]["ref_kind"] == "request"
    assert resolution["capability_owner"]["detailed_governance_refs"]
    assert any(
        action["action_key"] == "complete_external_setup"
        for action in resolution["capability_owner"]["next_actions"]
    )
