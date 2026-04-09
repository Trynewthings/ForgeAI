from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.schemas.capability_registry import CapabilityProposalReviewUpdateRequest
from app.services.capabilities import CapabilityLifecycleService, CapabilityProposalService, CapabilityRegistryService, CapabilityRequestService
from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_phase3_core import create_project


def test_generated_internal_request_can_generate_durable_proposal_with_request_and_lifecycle_links(
    client: TestClient,
    monkeypatch,
) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "no_changes",
              "confidence": 52
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
                "step_name": "rank_candidates",
                "node_name": "ranker",
                "logical_step_kind": "rank",
                "required_capabilities": ["internal_ranker_helper"],
            },
        ],
    )
    _run_build(client, project["id"], blueprint["id"])

    request = next(
        item for item in client.get(f"/api/v1/projects/{project['id']}/capability-requests").json()
        if item["request_kind"] == "generated_internal"
    )
    approved = client.patch(
        f"/api/v1/capability-requests/{request['request_id']}/review",
        json={"review_status": "approved", "review_notes": ["Approved for reviewed helper proposal."]},
    )
    assert approved.status_code == 200
    conversion = client.post(f"/api/v1/capability-requests/{request['request_id']}/convert")
    assert conversion.status_code == 200
    lifecycle_entry = conversion.json()["lifecycle_entry"]
    assert lifecycle_entry["lifecycle_status"] == "proposed"

    proposal_response = client.post(f"/api/v1/capability-requests/{request['request_id']}/proposal")
    assert proposal_response.status_code == 200
    proposal = proposal_response.json()
    assert proposal["proposal_kind"] == "generated_internal_proposal"
    assert proposal["proposal_status"] == "proposed"
    assert proposal["source_request_id"] == request["request_id"]
    assert proposal["source_lifecycle_entry_id"] == lifecycle_entry["capability_entry_id"]
    assert proposal["implementation"]["code_stub"] is not None
    assert "Proposal stub only" in proposal["implementation"]["code_stub"]
    assert proposal["implementation"]["capability_metadata_draft"]["enabled"] is False
    assert proposal["implementation"]["capability_metadata_draft"]["capability_type"] == "deterministic"
    assert proposal["implementation"]["test_suggestions"]

    request_after = client.get(f"/api/v1/capability-requests/{request['request_id']}").json()
    assert request_after["linked_proposal_id"] == proposal["proposal_id"]
    assert request_after["linked_proposal_status"] == "proposed"
    lifecycle_after = client.get(f"/api/v1/capability-lifecycle/{lifecycle_entry['capability_entry_id']}").json()
    assert lifecycle_after["linked_proposal_id"] == proposal["proposal_id"]
    assert lifecycle_after["linked_proposal_status"] == "proposed"

    summary = client.get(f"/api/v1/projects/{project['id']}/capability-proposals/summary").json()
    assert summary["summary"]["by_kind_counts"]["generated_internal_proposal"] == 1
    assert summary["summary"]["by_status_counts"]["proposed"] == 1
    assert summary["latest_proposals"][0]["proposal_id"] == proposal["proposal_id"]


def test_template_backed_request_can_generate_bounded_template_proposal(
    client: TestClient,
    monkeypatch,
) -> None:
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
    _run_build(client, project["id"], blueprint["id"])

    request = next(
        item for item in client.get(f"/api/v1/projects/{project['id']}/capability-requests").json()
        if item["request_kind"] == "template_backed_internal"
    )
    assert client.patch(
        f"/api/v1/capability-requests/{request['request_id']}/review",
        json={"review_status": "approved", "review_notes": ["Approved for template proposal review."]},
    ).status_code == 200
    conversion = client.post(f"/api/v1/capability-requests/{request['request_id']}/convert")
    assert conversion.status_code == 200

    proposal = client.post(f"/api/v1/capability-requests/{request['request_id']}/proposal").json()
    assert proposal["proposal_kind"] == "template_backed_proposal"
    assert proposal["request_kind_origin"] == "template_backed_internal"
    assert proposal["implementation"]["bounded_execution_kind"] == "record_normalizer"
    assert proposal["implementation"]["template_stub"] is not None
    assert proposal["implementation"]["code_stub"] is None
    assert proposal["implementation"]["capability_metadata_draft"]["template_backed_only"] is True
    assert proposal["implementation"]["capability_metadata_draft"]["enabled"] is False


def test_external_integration_request_cannot_generate_internal_proposal(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "no_changes",
              "confidence": 48
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
    _run_build(client, project["id"], blueprint["id"])

    request = next(
        item for item in client.get(f"/api/v1/projects/{project['id']}/capability-requests").json()
        if item["request_kind"] == "external_integration"
    )
    assert client.patch(
        f"/api/v1/capability-requests/{request['request_id']}/review",
        json={"review_status": "approved", "review_notes": ["Approved for external integration review."]},
    ).status_code == 200
    assert client.post(f"/api/v1/capability-requests/{request['request_id']}/convert").status_code == 200

    proposal_response = client.post(f"/api/v1/capability-requests/{request['request_id']}/proposal")
    assert proposal_response.status_code == 409
    assert "only supported for generated_internal and template_backed_internal" in proposal_response.text


def test_proposal_review_status_does_not_activate_capability_or_change_lifecycle_state() -> None:
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        registry = CapabilityRegistryService()
        request_service = CapabilityRequestService(db)
        gap = registry.build_gap_response(
            gap_kind="missing_binding",
            stage="build_planning",
            logical_step_kind="rank",
            requested_capability_id_or_name="internal_ranker_helper",
            reason="No internal helper is registered for this ranking path.",
        )
        request = request_service.ensure_requests_for_gaps(project_id="project-1", gaps=[gap])[0]
        request_service.update_review_state(request.request_id, review_status="approved", review_notes=["Approved for proposal generation."])
        lifecycle_service = CapabilityLifecycleService(db)
        conversion = lifecycle_service.convert_request(request.request_id)
        proposal_service = CapabilityProposalService(db)
        proposal = proposal_service.generate_for_request(request.request_id)

        updated = proposal_service.update_review_state(
            proposal.proposal_id,
            payload=CapabilityProposalReviewUpdateRequest(
                proposal_status="approved",
                review_notes=["Proposal approved for implementation planning."],
            ),
        )
        assert updated.proposal_status == "approved"
        assert updated.review_state.proposal_status == "approved"

        lifecycle_entry = lifecycle_service.get_entry(conversion.lifecycle_entry.capability_entry_id)
        assert lifecycle_entry.lifecycle_status == "proposed"
        active_ids = {
            item.capability_id
            for item in CapabilityRegistryService(db, project_id="project-1").list_capabilities(
                context="build_planning",
                include_disabled=True,
            )
        }
        assert proposal.capability_id_candidate not in active_ids
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
