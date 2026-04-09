from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.services.requirement.compiler.llm_assisted as compiler_llm_assisted
import app.services.requirement_session_service as requirement_session_service
from app.db.base import Base
from app.schemas.capability_registry import CapabilityGapResponse
from app.services.capabilities import CapabilityRegistryService, CapabilityRequestService
from app.services.provider_runtime import ProviderResponse, ResolvedModelConfig
from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_phase3_core import create_project


def test_capability_request_classification_distinguishes_external_template_and_generated() -> None:
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        service = CapabilityRequestService(db)
        registry = CapabilityRegistryService()

        external_gap = registry.build_gap_response(
            gap_kind="unsupported_mention",
            stage="authoring",
            requested_capability_id_or_name="slack_connector",
            reason="Capability mention is not present in the registered authoring/runtime vocabulary.",
            review_required=True,
        )
        template_gap = registry.build_gap_response(
            gap_kind="template_capability_unrealized",
            stage="build_planning",
            logical_step_kind="transform",
            requested_capability_id_or_name="run_python_template",
            normalized_requested_capability_id="run_python_template",
            reason="Template-backed capability remains review-only.",
            review_required=True,
        )
        generated_gap = registry.build_gap_response(
            gap_kind="missing_capability",
            stage="blueprint_planning",
            logical_step_kind="rank",
            requested_capability_id_or_name="internal_ranker_helper",
            reason="No internal deterministic helper is currently registered for this ranking variant.",
        )

        assert service.classify_gap(external_gap).request_kind == "external_integration"
        assert service.classify_gap(template_gap).request_kind == "template_backed_internal"
        assert service.classify_gap(generated_gap).request_kind == "generated_internal"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_session_gap_creates_formal_capability_request_and_review_status_can_update(
    client: TestClient,
    monkeypatch,
) -> None:
    project = create_project(client)
    response = client.post("/api/v1/requirements/sessions", json={"project_id": project["id"]})
    assert response.status_code == 201
    session_id = response.json()["id"]

    patch_payload = {
        "field_patches": {
            "goal": "Prepare a weekly engineering research digest.",
            "input_description": "source links",
            "output_description": "markdown digest",
            "acceptance_criteria_json": {"items": [{"id": "criterion_1", "type": "functional", "level": "hard", "description": "Produces a digest."}]},
        },
        "confidence": 0.9,
        "unsupported_capability_mentions": ["slack_connector"],
    }

    class StubAdapter:
        def chat(self, messages):
            return ProviderResponse(
                content=json.dumps(patch_payload),
                prompt_tokens=20,
                completion_tokens=20,
                total_tokens=40,
                raw_metadata={},
            )

    runtime_config = ResolvedModelConfig(
        provider="openai_compatible",
        model_name="session-test",
        endpoint="http://test.invalid/v1",
        api_key="test-key",
        temperature=0.2,
        max_tokens=512,
        source="test",
        llm_mode="real",
    )

    monkeypatch.setattr(requirement_session_service.settings, "llm_mode", "real")
    monkeypatch.setattr(requirement_session_service, "get_provider_adapter", lambda model_config=None, overrides=None: (StubAdapter(), runtime_config))
    monkeypatch.setattr(compiler_llm_assisted, "get_provider_adapter", lambda model_config=None, overrides=None: (StubAdapter(), runtime_config))
    monkeypatch.setattr(requirement_session_service.RequirementSessionService, "_build_runtime_assistant_reply", lambda self, **kwargs: "patched")

    response = client.post(
        f"/api/v1/requirements/sessions/{session_id}/message",
        json={"content": "We should support Slack incident intake."},
    )
    assert response.status_code == 200
    payload = response.json()
    request_id = payload["authoring_feedback"]["explanation"]["capability_requests"][0]["request_id"]
    assert payload["authoring_feedback"]["explanation"]["capability_requests"][0]["request_kind"] == "external_integration"
    assert payload["authoring_feedback"]["explanation"]["capability_requests"][0]["classification"]["request_kind"] == "external_integration"
    assert payload["authoring_feedback"]["explanation"]["capability_resolution"]["end_user"]["resolution_state"] == "blocked_requires_external_setup"
    assert payload["authoring_feedback"]["explanation"]["capability_resolution"]["end_user"]["detailed_governance_refs"] == []
    assert any(
        section["section_key"] == "capability_requests"
        for section in payload["authoring_feedback"]["explanation"]["presentation"]["sections"]
    )
    assert any(
        section["section_key"] == "capability_resolution"
        for section in payload["authoring_feedback"]["explanation"]["presentation"]["sections"]
    )

    requests_response = client.get(f"/api/v1/projects/{project['id']}/capability-requests")
    assert requests_response.status_code == 200
    requests_payload = requests_response.json()
    assert requests_payload[0]["request_id"] == request_id
    assert requests_payload[0]["request_kind"] == "external_integration"
    assert requests_payload[0]["review_state"]["review_status"] == "proposed"

    review_response = client.patch(
        f"/api/v1/capability-requests/{request_id}/review",
        json={"review_status": "under_review", "review_notes": ["Assigned for connector review."]},
    )
    assert review_response.status_code == 200
    assert review_response.json()["review_state"]["review_status"] == "under_review"
    assert review_response.json()["review_state"]["review_notes"] == ["Assigned for connector review."]


def test_build_planning_gap_creates_template_backed_capability_request(client: TestClient, monkeypatch) -> None:
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

    build, _ = _run_build(client, project["id"], blueprint["id"])

    requests_payload = client.get(f"/api/v1/projects/{project['id']}/capability-requests").json()
    assert any(item["classification"]["request_kind"] == "template_backed_internal" for item in requests_payload)
    build_requests = build["manifest_snapshot_json"]["metadata"]["build_planning"]["capability_requests"]
    assert any(item["classification"]["request_kind"] == "template_backed_internal" for item in build_requests)


def test_build_planning_declared_internal_helper_gap_creates_generated_internal_request(client: TestClient, monkeypatch) -> None:
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

    build, _ = _run_build(client, project["id"], blueprint["id"])

    requests_payload = client.get(f"/api/v1/projects/{project['id']}/capability-requests").json()
    assert any(item["request_kind"] == "generated_internal" for item in requests_payload)
    assert any(item["implementation_route_hint"] == "propose_generated_helper" for item in requests_payload)
    build_requests = build["manifest_snapshot_json"]["metadata"]["build_planning"]["capability_requests"]
    assert any(item["request_kind"] == "generated_internal" for item in build_requests)


def test_build_planning_declared_external_connector_gap_creates_external_integration_request(client: TestClient, monkeypatch) -> None:
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

    build, _ = _run_build(client, project["id"], blueprint["id"])

    requests_payload = client.get(f"/api/v1/projects/{project['id']}/capability-requests").json()
    assert any(item["request_kind"] == "external_integration" for item in requests_payload)
    assert any(item["source_stage"] == "build_planning" for item in requests_payload)
    build_requests = build["manifest_snapshot_json"]["metadata"]["build_planning"]["capability_requests"]
    assert any(item["request_kind"] == "external_integration" for item in build_requests)


def test_project_capability_request_summary_exposes_kind_and_status_counts(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "no_changes",
              "confidence": 51
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
            {
                "step_name": "rank_candidates",
                "node_name": "ranker",
                "logical_step_kind": "rank",
                "required_capabilities": ["internal_ranker_helper"],
            },
        ],
    )

    _run_build(client, project["id"], blueprint["id"])

    summary_response = client.get(f"/api/v1/projects/{project['id']}/capability-requests/summary")
    assert summary_response.status_code == 200
    payload = summary_response.json()
    assert payload["summary"]["total_count"] >= 2
    assert payload["summary"]["actionable_count"] >= 2
    assert payload["summary"]["by_kind_counts"]["external_integration"] >= 1
    assert payload["summary"]["by_kind_counts"]["generated_internal"] >= 1
    assert payload["summary"]["by_stage_counts"]["build_planning"] >= 2
    assert payload["summary"]["by_status_counts"]["proposed"] >= 2
    assert any("external_integration" in item for item in payload["summary"]["headlines"])
    assert payload["latest_actionable_requests"][0]["request_kind"] in {"external_integration", "generated_internal"}
    assert payload["resolution"]["operator"]["linked_request_count"] >= 2
    assert payload["resolution"]["capability_owner"]["detailed_governance_refs"]
