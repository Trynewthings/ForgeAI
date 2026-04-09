from __future__ import annotations

from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_phase3_core import create_project


def test_generated_internal_materialization_becomes_reusable_candidate_and_binds_naturally(client, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "no_changes",
              "confidence": 48
            }
            """,
            """
            {
              "status": "no_changes",
              "confidence": 52
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
        json={"review_status": "approved", "review_notes": ["Approve generated internal request."]},
    )
    client.post(f"/api/v1/capability-requests/{request['request_id']}/convert")
    proposal = client.post(f"/api/v1/capability-requests/{request['request_id']}/proposal").json()
    client.patch(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/review",
        json={"proposal_status": "approved", "review_notes": ["Approve proposal."]},
    )
    client.post(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/activate",
        json={"activation_notes": ["Activate lifecycle."]},
    )
    client.post(f"/api/v1/capability-proposals/{proposal['proposal_id']}/materialize")

    reusable_summary = client.get(f"/api/v1/projects/{project['id']}/reusable-capabilities/summary")
    assert reusable_summary.status_code == 200
    reusable_candidates = reusable_summary.json()["reusable_candidates"]
    generated_candidate = next(item for item in reusable_candidates if item["origin_request_kind"] == "generated_internal")
    assert generated_candidate["reusable"] is True
    assert generated_candidate["runtime_ready"] is True
    assert "internal_ranker_helper" in generated_candidate["alias_ids"]

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
    planning = build["manifest_snapshot_json"]["metadata"]["build_planning"]
    binding = planning["proposed_bindings"][0]
    assert binding["selected_source_kind"] == "materialization"
    assert binding["selected_capability_id"] == "gen_internal_ranker_helper"
    assert binding["selected_materialization_id"]
    assert "exactly matches the requested project-scoped capability hint" in binding["selection_reason"]
    assert "rank_candidates" in binding["rejected_candidate_ids"]

    bindings_doc = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "bindings.json"},
    ).json()["content"]
    assert '"selected_capability_id": "gen_internal_ranker_helper"' in bindings_doc
    assert '"source_kind": "materialization"' in bindings_doc


def test_static_exact_capability_id_keeps_priority_over_reusable_materialization(client, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "no_changes",
              "confidence": 48
            }
            """,
            """
            {
              "status": "no_changes",
              "confidence": 52
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
        json={"review_status": "approved", "review_notes": ["Approve generated internal request."]},
    )
    client.post(f"/api/v1/capability-requests/{request['request_id']}/convert")
    proposal = client.post(f"/api/v1/capability-requests/{request['request_id']}/proposal").json()
    client.patch(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/review",
        json={"proposal_status": "approved", "review_notes": ["Approve proposal."]},
    )
    client.post(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/activate",
        json={"activation_notes": ["Activate lifecycle."]},
    )
    client.post(f"/api/v1/capability-proposals/{proposal['proposal_id']}/materialize")

    canonical_blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {
                "step_name": "rank_candidates",
                "node_name": "ranker",
                "logical_step_kind": "rank",
                "required_capabilities": ["rank_candidates"],
            },
        ],
    )
    build, executable = _run_build(client, project["id"], canonical_blueprint["id"])
    planning = build["manifest_snapshot_json"]["metadata"]["build_planning"]
    binding = planning["proposed_bindings"][0]
    assert binding["selected_source_kind"] == "static_registry"
    assert binding["selected_capability_id"] == "rank_candidates"
    assert "exact canonical static capability" in binding["selection_reason"]

    bindings_doc = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "bindings.json"},
    ).json()["content"]
    assert '"selected_capability_id": "rank_candidates"' in bindings_doc
    assert '"source_kind": "static_registry"' in bindings_doc


def test_active_but_not_materialized_internal_capability_is_not_reusable(client, monkeypatch) -> None:
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
        json={"review_status": "approved", "review_notes": ["Approve generated internal request."]},
    )
    client.post(f"/api/v1/capability-requests/{request['request_id']}/convert")

    reusable_summary = client.get(f"/api/v1/projects/{project['id']}/reusable-capabilities/summary")
    assert reusable_summary.status_code == 200
    payload = reusable_summary.json()
    assert payload["summary"]["reusable_count"] == 0
    assert payload["reusable_candidates"] == []


def test_template_backed_materialization_becomes_project_reusable_candidate(client, monkeypatch) -> None:
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
              "status": "no_changes",
              "confidence": 52
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
        json={"review_status": "approved", "review_notes": ["Approve template request."]},
    )
    client.post(f"/api/v1/capability-requests/{request['request_id']}/convert")
    proposal = client.post(f"/api/v1/capability-requests/{request['request_id']}/proposal").json()
    client.patch(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/review",
        json={"proposal_status": "approved", "review_notes": ["Approve template proposal."]},
    )
    client.post(
        f"/api/v1/capability-proposals/{proposal['proposal_id']}/activate",
        json={"activation_notes": ["Activate template lifecycle."]},
    )
    client.post(f"/api/v1/capability-proposals/{proposal['proposal_id']}/materialize")

    reusable_candidates = client.get(f"/api/v1/projects/{project['id']}/reusable-capabilities").json()
    template_candidate = next(item for item in reusable_candidates if item["origin_request_kind"] == "template_backed_internal")
    assert template_candidate["reusable"] is True
    assert template_candidate["runtime_shape"]["execution_kind"] == "template_runtime"

    runnable_blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {
                "step_name": "prepare_records",
                "node_name": "processor",
                "logical_step_kind": "transform",
                "required_capabilities": ["tmpl_run_python_template"],
            },
        ],
    )
    build, executable = _run_build(client, project["id"], runnable_blueprint["id"])
    planning = build["manifest_snapshot_json"]["metadata"]["build_planning"]
    binding = planning["proposed_bindings"][0]
    assert binding["selected_source_kind"] == "materialization"
    assert binding["selected_capability_id"] == "tmpl_run_python_template"
    assert "project-scoped capability hint" in binding["selection_reason"]

    bindings_doc = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "bindings.json"},
    ).json()["content"]
    assert '"execution_kind": "template_runtime"' in bindings_doc
    assert '"source_kind": "materialization"' in bindings_doc
