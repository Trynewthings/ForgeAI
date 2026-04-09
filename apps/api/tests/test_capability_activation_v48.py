from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.schemas.capability_registry import CapabilityProposalActivationRequest, CapabilityProposalReviewUpdateRequest
from app.services.capabilities import CapabilityLifecycleService, CapabilityProposalService, CapabilityRegistryService, CapabilityRequestService
from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_phase3_core import create_project


def test_approved_generated_internal_proposal_requires_explicit_activation_and_then_becomes_usable(
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
    assert client.patch(
        f"/api/v1/capability-requests/{request['request_id']}/review",
        json={"review_status": "approved", "review_notes": ["Approved for generated helper proposal."]},
    ).status_code == 200
    lifecycle_conversion = client.post(f"/api/v1/capability-requests/{request['request_id']}/convert").json()
    lifecycle_entry = lifecycle_conversion["lifecycle_entry"]
    assert lifecycle_entry["lifecycle_status"] == "proposed"

    proposal = client.post(f"/api/v1/capability-requests/{request['request_id']}/proposal").json()
    approved_proposal = client.patch(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/review",
        json={"proposal_status": "approved", "review_notes": ["Proposal reviewed and approved for activation."]},
    ).json()
    assert approved_proposal["proposal_status"] == "approved"

    before_activation = {
        item.capability_id
        for item in CapabilityRegistryService().list_capabilities(context="build_planning", include_disabled=True)
    }
    assert proposal["capability_id_candidate"] not in before_activation

    eligibility = client.get(f"/api/v1/capability-proposals/{proposal['proposal_id']}/activation-eligibility").json()
    assert eligibility["eligible"] is True
    assert eligibility["activation_status"] == "eligible"
    assert eligibility["enabled_contexts_candidate"] == ["blueprint_planning", "build_planning", "runtime"]

    activation = client.post(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/activate",
        json={"activation_notes": ["Activation approved by platform owner."]},
    )
    assert activation.status_code == 200
    payload = activation.json()
    assert payload["activated"] is True
    assert payload["proposal"]["proposal_status"] == "converted"
    assert payload["lifecycle_entry"]["lifecycle_status"] == "active"
    assert payload["lifecycle_entry"]["is_usable"] is True
    assert payload["activated_at"] is not None
    assert "Activation approved by platform owner." in payload["lifecycle_entry"]["activation_notes"]

    active_ids = {
        item["capability_id"]
        for item in client.get(f"/api/v1/projects/{project['id']}/capability-lifecycle").json()
        if item["lifecycle_status"] == "active"
    }
    assert payload["resulting_capability_id"] in active_ids


def test_approved_template_backed_proposal_can_activate_only_with_valid_bounded_template_kind(
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
        json={"review_status": "approved", "review_notes": ["Approved for bounded template proposal."]},
    ).status_code == 200
    client.post(f"/api/v1/capability-requests/{request['request_id']}/convert")
    proposal = client.post(f"/api/v1/capability-requests/{request['request_id']}/proposal").json()
    assert client.patch(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/review",
        json={"proposal_status": "approved", "review_notes": ["Bounded template proposal approved."]},
    ).status_code == 200

    eligibility = client.get(f"/api/v1/capability-proposals/{proposal['proposal_id']}/activation-eligibility").json()
    assert eligibility["eligible"] is True
    assert eligibility["activation_status"] == "eligible"

    activation = client.post(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/activate",
        json={"activation_notes": ["Template activation approved."]},
    ).json()
    assert activation["activated"] is True
    assert activation["proposal"]["proposal_status"] == "converted"
    assert activation["lifecycle_entry"]["lifecycle_status"] == "active"
    assert activation["lifecycle_entry"]["linked_static_registry_capability_id"] == "run_python_template"


def test_proposal_approval_alone_does_not_activate_registry_usability() -> None:
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
        proposal_service.update_review_state(
            proposal.proposal_id,
            CapabilityProposalReviewUpdateRequest(
                proposal_status="approved",
                review_notes=["Proposal approved but not activated."],
            ),
        )

        lifecycle_entry = lifecycle_service.get_entry(conversion.lifecycle_entry.capability_entry_id)
        assert lifecycle_entry.lifecycle_status == "proposed"
        snapshot_ids = {
            item.capability_id
            for item in CapabilityRegistryService(db, project_id="project-1").list_capabilities(
                context="build_planning",
                include_disabled=True,
            )
        }
        assert proposal.capability_id_candidate not in snapshot_ids
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_successful_activation_makes_lifecycle_entry_visible_in_project_registry_snapshot() -> None:
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
        request_service.update_review_state(request.request_id, review_status="approved", review_notes=["Approved for activation path test."])
        lifecycle_service = CapabilityLifecycleService(db)
        lifecycle_service.convert_request(request.request_id)
        proposal_service = CapabilityProposalService(db)
        proposal = proposal_service.generate_for_request(request.request_id)
        proposal_service.update_review_state(
            proposal.proposal_id,
            CapabilityProposalReviewUpdateRequest(
                proposal_status="approved",
                review_notes=["Approved proposal for activation path test."],
            ),
        )
        activation = proposal_service.activate_proposal(
            proposal.proposal_id,
            CapabilityProposalActivationRequest(activation_notes=["Activate now."]),
        )
        assert activation.activated is True

        snapshot_ids = {
            item.capability_id
            for item in CapabilityRegistryService(db, project_id="project-1").list_capabilities(
                context="build_planning",
                include_disabled=True,
            )
        }
        assert proposal.capability_id_candidate in snapshot_ids
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_failed_activation_returns_structured_missing_requirements() -> None:
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        registry = CapabilityRegistryService()
        request_service = CapabilityRequestService(db)
        gap = registry.build_gap_response(
            gap_kind="template_capability_unrealized",
            stage="build_planning",
            logical_step_kind="transform",
            requested_capability_id_or_name="run_python_template",
            normalized_requested_capability_id="run_python_template",
            reason="Template-backed capability remains review-only.",
            review_required=True,
        )
        request = request_service.ensure_requests_for_gaps(project_id="project-1", gaps=[gap])[0]
        request_service.update_review_state(request.request_id, review_status="approved", review_notes=["Approved for template proposal generation."])
        lifecycle_service = CapabilityLifecycleService(db)
        lifecycle_service.convert_request(request.request_id)
        proposal_service = CapabilityProposalService(db)
        proposal = proposal_service.generate_for_request(request.request_id)
        proposal_service.update_review_state(
            proposal.proposal_id,
            CapabilityProposalReviewUpdateRequest(
                proposal_status="approved",
                review_notes=["Proposal approved but template kind needs validation."],
            ),
        )

        stored = proposal_service.repository.get_by_id(proposal.proposal_id)
        assert stored is not None
        stored.bounded_execution_kind = "unsafe_custom_template"
        db.commit()
        db.refresh(stored)

        eligibility = proposal_service.get_activation_eligibility(proposal.proposal_id)
        assert eligibility.eligible is False
        assert "Template-backed proposal requires a valid bounded execution kind." in eligibility.missing_requirements

        activation = proposal_service.activate_proposal(
            proposal.proposal_id,
            CapabilityProposalActivationRequest(activation_notes=["Attempted activation should fail."]),
        )
        assert activation.activated is False
        assert activation.eligibility.eligible is False
        assert activation.lifecycle_entry.lifecycle_status == "disabled"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
