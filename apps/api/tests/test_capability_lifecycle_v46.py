from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import AppException
from app.db.base import Base
from app.schemas.capability_registry import CapabilityLifecycleUpdateRequest
from app.services.capabilities import CapabilityLifecycleService, CapabilityRegistryService, CapabilityRequestService
from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_phase3_core import create_project


def test_generated_request_can_convert_to_lifecycle_entry_without_becoming_active(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "no_changes",
              "confidence": 50
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

    request = client.get(f"/api/v1/projects/{project['id']}/capability-requests").json()[0]
    approved = client.patch(
        f"/api/v1/capability-requests/{request['request_id']}/review",
        json={"review_status": "approved", "review_notes": ["Approved for internal helper design."]},
    ).json()
    assert approved["review_state"]["review_status"] == "approved"

    conversion = client.post(f"/api/v1/capability-requests/{request['request_id']}/convert")
    assert conversion.status_code == 200
    payload = conversion.json()
    assert payload["request"]["review_state"]["review_status"] == "converted"
    assert payload["request"]["linked_capability_entry_id"] == payload["lifecycle_entry"]["capability_entry_id"]
    assert payload["lifecycle_entry"]["lifecycle_status"] == "proposed"
    assert payload["lifecycle_entry"]["is_usable"] is False
    assert payload["lifecycle_entry"]["request_kind_origin"] == "generated_internal"


def test_external_request_conversion_creates_disabled_lifecycle_entry_not_active(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "no_changes",
              "confidence": 50
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
    client.patch(
        f"/api/v1/capability-requests/{request['request_id']}/review",
        json={"review_status": "approved", "review_notes": ["Approved for connector onboarding review."]},
    )
    payload = client.post(f"/api/v1/capability-requests/{request['request_id']}/convert").json()
    assert payload["lifecycle_entry"]["lifecycle_status"] == "disabled"
    assert payload["lifecycle_entry"]["is_usable"] is False
    assert "credentials_setup" in payload["lifecycle_entry"]["activation_requirements"]
    assert payload["lifecycle_entry"]["request_kind_origin"] == "external_integration"


def test_template_backed_request_conversion_links_static_template_capability(client: TestClient, monkeypatch) -> None:
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
    client.patch(
        f"/api/v1/capability-requests/{request['request_id']}/review",
        json={"review_status": "approved", "review_notes": ["Approved for template variant review."]},
    )
    payload = client.post(f"/api/v1/capability-requests/{request['request_id']}/convert").json()
    assert payload["lifecycle_entry"]["lifecycle_status"] == "disabled"
    assert payload["lifecycle_entry"]["linked_static_registry_capability_id"] == "run_python_template"
    assert payload["lifecycle_entry"]["request_kind_origin"] == "template_backed_internal"


def test_manual_review_only_request_cannot_convert() -> None:
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        request_service = CapabilityRequestService(db)
        registry = CapabilityRegistryService()
        gap = registry.build_gap_response(
            gap_kind="review_needed",
            stage="build_planning",
            logical_step_kind="validate",
            requested_capability_id_or_name="manual_triage_gate",
            reason="This capability choice needs manual triage before lifecycle conversion is meaningful.",
            review_required=True,
            suggested_request=registry.build_request_suggestion(
                stage="build_planning",
                logical_step_kind="validate",
                requested_capability_id_or_name="manual_triage_gate",
                reason="Manual triage is required.",
                review_required=True,
                request_kind="manual_review",
            ),
            alternative_capabilities=[],
        )
        request = request_service.ensure_requests_for_gaps(project_id="project-1", gaps=[gap])[0]
        request_service.update_review_state(request.request_id, review_status="approved")
        lifecycle_service = CapabilityLifecycleService(db)
        try:
            lifecycle_service.convert_request(request.request_id)
            raise AssertionError("Expected conversion to be blocked for manual_review_only request")
        except AppException as exc:
            assert "manual_review_only" in str(exc)
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_active_lifecycle_entries_are_the_only_dynamic_entries_exposed_in_registry_snapshot() -> None:
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
        request_service.update_review_state(request.request_id, review_status="approved")
        lifecycle_service = CapabilityLifecycleService(db)
        conversion = lifecycle_service.convert_request(request.request_id)
        entry_id = conversion.lifecycle_entry.capability_entry_id

        inactive_snapshot_ids = {
            item.capability_id
            for item in CapabilityRegistryService(db, project_id="project-1").list_capabilities(context="build_planning", include_disabled=True)
        }
        assert conversion.lifecycle_entry.capability_id not in inactive_snapshot_ids

        lifecycle_service.update_entry(
            entry_id,
            CapabilityLifecycleUpdateRequest(
                lifecycle_status="active",
                enabled_contexts=["build_planning", "runtime"],
                review_notes=["Implementation completed and activated."],
            ),
        )
        active_registry = CapabilityRegistryService(db, project_id="project-1")
        active_snapshot_ids = {
            item.capability_id
            for item in active_registry.list_capabilities(context="build_planning", include_disabled=True)
        }
        assert conversion.lifecycle_entry.capability_id in active_snapshot_ids
        validation = active_registry.validate_capability_usage(
            capability_id=conversion.lifecycle_entry.capability_id,
            context="build_planning",
            step_kind="rank",
        )
        assert validation.valid is True
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
