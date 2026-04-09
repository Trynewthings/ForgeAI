from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.schemas.capability_registry import CapabilityProposalReviewUpdateRequest
from app.services.capabilities import (
    CapabilityLifecycleService,
    CapabilityMaterializationService,
    CapabilityProposalService,
    CapabilityRegistryService,
    CapabilityRequestService,
)
from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_phase3_core import create_project, execute_job, wait_for_run_completion


def _activate_and_materialize_internal_capability(
    db: Session,
    *,
    project_id: str,
    gap_kind: str,
    logical_step_kind: str,
    requested_capability: str,
    reason: str,
    requested_name: str | None = None,
) -> tuple[dict, dict, dict]:
    registry = CapabilityRegistryService()
    request_service = CapabilityRequestService(db)
    gap = registry.build_gap_response(
        gap_kind=gap_kind,
        stage="build_planning",
        logical_step_kind=logical_step_kind,
        requested_capability_id_or_name=requested_capability,
        normalized_requested_capability_id=requested_capability,
        reason=reason,
        review_required=gap_kind == "template_capability_unrealized",
    )
    request = request_service.ensure_requests_for_gaps(project_id=project_id, gaps=[gap])[0]
    request_service.update_review_state(request.request_id, review_status="approved", review_notes=["Approved for runtime materialization test."])

    lifecycle_service = CapabilityLifecycleService(db)
    lifecycle = lifecycle_service.convert_request(request.request_id).lifecycle_entry

    proposal_service = CapabilityProposalService(db)
    proposal = proposal_service.generate_for_request(request.request_id)
    proposal_service.update_review_state(
        proposal.proposal_id,
        CapabilityProposalReviewUpdateRequest(
            proposal_status="approved",
            review_notes=["Proposal approved for runtime materialization test."],
        ),
    )
    proposal_service.activate_proposal(proposal.proposal_id)

    materialization_service = CapabilityMaterializationService(db)
    materialization = materialization_service.materialize_proposal(proposal.proposal_id)
    return request.model_dump(mode="python"), lifecycle.model_dump(mode="python"), materialization.model_dump(mode="python")


def test_generated_internal_materialization_creates_bounded_runtime_realization() -> None:
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        request, lifecycle, result = _activate_and_materialize_internal_capability(
            db,
            project_id="project-1",
            gap_kind="missing_binding",
            logical_step_kind="rank",
            requested_capability="internal_ranker_helper",
            reason="No internal helper is registered for this ranking path.",
        )
        materialization = result["materialization"]
        assert result["materialized"] is True
        assert materialization["materialization_kind"] == "generated_internal_runtime"
        assert materialization["materialization_status"] == "materialized"
        assert materialization["resulting_execution_kind"] == "python_function"
        assert materialization["resulting_runtime_adapter"] == "materialized_internal_python"
        assert materialization["resulting_handler_ref"] == "rank_candidates"
        assert materialization["materialized_asset_refs"][0]["kind"] == "generated_runtime_stub"

        linked_request = CapabilityRequestService(db).get_request(request["request_id"])
        linked_lifecycle = CapabilityLifecycleService(db).get_entry(lifecycle["capability_entry_id"])
        linked_proposal = CapabilityProposalService(db).get_proposal(result["proposal"]["proposal_id"])
        assert linked_request.linked_materialization_status == "materialized"
        assert linked_lifecycle.linked_materialization_status == "materialized"
        assert linked_proposal.linked_materialization_status == "materialized"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_template_backed_materialization_creates_template_runtime_realization() -> None:
    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        _, _, result = _activate_and_materialize_internal_capability(
            db,
            project_id="project-1",
            gap_kind="template_capability_unrealized",
            logical_step_kind="transform",
            requested_capability="run_python_template",
            reason="Template-backed capability remains review-only until a bounded runtime is materialized.",
        )
        materialization = result["materialization"]
        assert result["materialized"] is True
        assert materialization["materialization_kind"] == "template_backed_runtime"
        assert materialization["materialization_status"] == "materialized"
        assert materialization["resulting_execution_kind"] == "template_runtime"
        assert materialization["resulting_runtime_adapter"] == "materialized_template_runtime"
        assert materialization["resulting_handler_ref"] == "record_normalizer"
        assert materialization["materialized_asset_refs"][0]["kind"] == "template_runtime_stub"
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


def test_active_internal_capability_without_materialization_remains_blocked_in_execution_package(client, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "no_changes",
              "confidence": 51
            }
            """,
            """
            {
              "status": "planned",
              "confidence": 86,
              "proposed_bindings": [
                {
                  "step_name": "rank_candidates",
                  "logical_step_kind": "rank",
                  "selected_capability_id": "gen_internal_ranker_helper"
                }
              ]
            }
            """,
        ],
    )
    project = create_project(client)
    bootstrap_blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {
                "step_name": "bootstrap_ranker",
                "node_name": "ranker",
                "logical_step_kind": "rank",
                "required_capabilities": ["internal_ranker_helper"],
            },
        ],
    )
    _run_build(client, project["id"], bootstrap_blueprint["id"])

    request = next(
        item for item in client.get(f"/api/v1/projects/{project['id']}/capability-requests").json()
        if item["request_kind"] == "generated_internal"
    )
    client.patch(
        f"/api/v1/capability-requests/{request['request_id']}/review",
        json={"review_status": "approved", "review_notes": ["Approved for activation."]},
    )
    client.post(f"/api/v1/capability-requests/{request['request_id']}/convert")
    proposal = client.post(f"/api/v1/capability-requests/{request['request_id']}/proposal").json()
    client.patch(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/review",
        json={"proposal_status": "approved", "review_notes": ["Approved proposal."]},
    )
    client.post(f"/api/v1/capability-proposals/{proposal['proposal_id']}/activate", json={"activation_notes": ["Activate lifecycle only."]})

    runnable_blueprint = _create_blueprint(
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
    build, executable = _run_build(client, project["id"], runnable_blueprint["id"])
    bindings_doc = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "bindings.json"},
    ).json()["content"]
    assert '"selected_capability_id": "gen_internal_ranker_helper"' in bindings_doc
    assert '"binding_status": "blocked"' in bindings_doc
    assert '"blocked_reason": "active_internal_capability_not_materialized"' in bindings_doc


def test_materialized_generated_internal_capability_becomes_runnable_and_executes(client, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "no_changes",
              "confidence": 51
            }
            """,
            """
            {
              "status": "planned",
              "confidence": 88,
              "proposed_bindings": [
                {
                  "step_name": "rank_candidates",
                  "logical_step_kind": "rank",
                  "selected_capability_id": "gen_internal_ranker_helper"
                }
              ]
            }
            """,
        ],
    )
    project = create_project(client)
    bootstrap_blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {
                "step_name": "bootstrap_ranker",
                "node_name": "ranker",
                "logical_step_kind": "rank",
                "required_capabilities": ["internal_ranker_helper"],
            },
        ],
    )
    _run_build(client, project["id"], bootstrap_blueprint["id"])

    request = next(
        item for item in client.get(f"/api/v1/projects/{project['id']}/capability-requests").json()
        if item["request_kind"] == "generated_internal"
    )
    client.patch(
        f"/api/v1/capability-requests/{request['request_id']}/review",
        json={"review_status": "approved", "review_notes": ["Approved for materialization."]},
    )
    client.post(f"/api/v1/capability-requests/{request['request_id']}/convert")
    proposal = client.post(f"/api/v1/capability-requests/{request['request_id']}/proposal").json()
    client.patch(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/review",
        json={"proposal_status": "approved", "review_notes": ["Approved proposal."]},
    )
    client.post(f"/api/v1/capability-proposals/{proposal['proposal_id']}/activate", json={"activation_notes": ["Activate lifecycle."]})
    materialization = client.post(f"/api/v1/capability-proposals/{proposal['proposal_id']}/materialize")
    assert materialization.status_code == 200
    assert materialization.json()["materialized"] is True

    runnable_blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {"step_name": "rank_candidates", "node_name": "ranker", "logical_step_kind": "rank"},
        ],
    )
    build, executable = _run_build(client, project["id"], runnable_blueprint["id"])
    bindings_doc = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "bindings.json"},
    ).json()["content"]
    assert '"selected_capability_id": "gen_internal_ranker_helper"' in bindings_doc
    assert '"source_kind": "materialization"' in bindings_doc
    assert '"binding_status": "runnable"' in bindings_doc
    assert '"handler_ref": "rank_candidates"' in bindings_doc

    run = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": runnable_blueprint["id"],
            "executable_id": executable["id"],
            "input_snapshot_json": {
                "candidates": [
                    {"name": "alpha", "score": 3},
                    {"name": "beta", "score": 9},
                    {"name": "gamma", "score": 5},
                ]
            },
        },
    ).json()
    start_response = client.post(f"/api/v1/runs/{run['id']}/start")
    assert start_response.status_code == 200
    run_payload = start_response.json()
    assert run_payload["active_job_id"]
    assert execute_job(client, run_payload["active_job_id"])["status"] == "completed"
    completed_run = wait_for_run_completion(client, run["id"])
    assert completed_run["status"] == "completed"
    steps = client.get(f"/api/v1/runs/{run['id']}/steps").json()
    assert steps[0]["status"] == "completed"
    assert steps[0]["output_payload_json"]["handler_ref"] == "rank_candidates"
    assert steps[0]["output_payload_json"]["structured_output"]["ranked_candidates"][0]["name"] == "beta"


def test_materialized_template_backed_capability_becomes_runnable_and_executes(client, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 77,
              "proposed_bindings": [
                {
                  "step_name": "prepare_records",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "run_python_template",
                  "template_execution_kind": "record_normalizer"
                }
              ]
            }
            """,
            """
            {
              "status": "planned",
              "confidence": 81,
              "proposed_bindings": [
                {
                  "step_name": "prepare_records",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "tmpl_run_python_template"
                }
              ]
            }
            """,
        ],
    )
    project = create_project(client)
    bootstrap_blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {"step_name": "prepare_records", "node_name": "processor", "logical_step_kind": "transform"},
        ],
    )
    _run_build(client, project["id"], bootstrap_blueprint["id"])
    request = next(
        item for item in client.get(f"/api/v1/projects/{project['id']}/capability-requests").json()
        if item["request_kind"] == "template_backed_internal"
    )
    client.patch(
        f"/api/v1/capability-requests/{request['request_id']}/review",
        json={"review_status": "approved", "review_notes": ["Approved for template runtime materialization."]},
    )
    client.post(f"/api/v1/capability-requests/{request['request_id']}/convert")
    proposal = client.post(f"/api/v1/capability-requests/{request['request_id']}/proposal").json()
    client.patch(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/review",
        json={"proposal_status": "approved", "review_notes": ["Approved template proposal."]},
    )
    client.post(f"/api/v1/capability-proposals/{proposal['proposal_id']}/activate", json={"activation_notes": ["Activate template lifecycle."]})
    materialization = client.post(f"/api/v1/capability-proposals/{proposal['proposal_id']}/materialize")
    assert materialization.status_code == 200
    assert materialization.json()["materialization"]["resulting_execution_kind"] == "template_runtime"

    runnable_blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {"step_name": "prepare_records", "node_name": "processor", "logical_step_kind": "transform"},
        ],
    )
    build, executable = _run_build(client, project["id"], runnable_blueprint["id"])
    bindings_doc = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "bindings.json"},
    ).json()["content"]
    assert '"selected_capability_id": "tmpl_run_python_template"' in bindings_doc
    assert '"execution_kind": "template_runtime"' in bindings_doc
    assert '"binding_status": "runnable"' in bindings_doc

    run = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": runnable_blueprint["id"],
            "executable_id": executable["id"],
            "input_snapshot_json": {
                "records": [
                    {"name": "Ada", "full_name": "Ada Lovelace", "score": 9},
                    {"name": "Grace", "full_name": "Grace Hopper", "score": 8},
                ]
            },
        },
    ).json()
    started = client.post(f"/api/v1/runs/{run['id']}/start").json()
    assert started["active_job_id"]
    assert execute_job(client, started["active_job_id"])["status"] == "completed"
    completed_run = wait_for_run_completion(client, run["id"])
    assert completed_run["status"] == "completed"
    steps = client.get(f"/api/v1/runs/{run['id']}/steps").json()
    assert steps[0]["status"] == "completed"
    assert steps[0]["output_payload_json"]["step_type"] == "template_runtime"
    assert steps[0]["output_payload_json"]["structured_output"]["template_runtime_kind"] == "record_normalizer"
