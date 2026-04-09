from __future__ import annotations

from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_phase3_core import create_project
from tests.test_reusable_capability_authoring_v55 import (
    _materialize_generated_candidate,
    _materialize_template_candidate,
)


def test_template_materialization_selection_records_bounded_realization_candidates(client, monkeypatch) -> None:
    project = create_project(client)
    template_candidate = _materialize_template_candidate(client, project["id"], monkeypatch)

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 79,
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
        ],
    )
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {"step_name": "prepare_records", "node_name": "processor", "logical_step_kind": "transform"},
        ],
    )
    build, executable = _run_build(client, project["id"], blueprint["id"])

    binding = build["manifest_snapshot_json"]["metadata"]["build_planning"]["proposed_bindings"][0]
    assert binding["selected_source_kind"] == "materialization"
    assert binding["selected_capability_id"] == template_candidate["capability_id"]
    assert binding["selected_candidate"]["candidate_id"] == template_candidate["capability_id"]
    assert len(binding["realization_candidates"]) >= 2
    assert any(item["candidate_id"] == "parse_json" for item in binding["realization_candidates"])
    assert any(item["candidate_id"] == "deduplicate_items" for item in binding["realization_candidates"])
    assert "parse_json" in binding["rejected_candidate_ids"]
    assert any("rejection_reason" in item and item["rejection_reason"] for item in binding["rejected_candidates"])
    assert "bounded realization candidate" in binding["flexibility_explanation"]["summary"]

    steps_content = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "steps.json"},
    ).json()["content"]
    assert '"build_realization"' in steps_content
    assert '"selected_source_kind": "materialization"' in steps_content


def test_realization_selection_remains_deterministic_for_repeated_builds(client, monkeypatch) -> None:
    project = create_project(client)
    _materialize_template_candidate(client, project["id"], monkeypatch)

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 79,
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
              "confidence": 79,
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
        ],
    )

    first_blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {"step_name": "prepare_records", "node_name": "processor", "logical_step_kind": "transform"},
        ],
    )
    second_blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {"step_name": "prepare_records", "node_name": "processor", "logical_step_kind": "transform"},
        ],
    )

    first_build, _ = _run_build(client, project["id"], first_blueprint["id"])
    second_build, _ = _run_build(client, project["id"], second_blueprint["id"])

    first_binding = first_build["manifest_snapshot_json"]["metadata"]["build_planning"]["proposed_bindings"][0]
    second_binding = second_build["manifest_snapshot_json"]["metadata"]["build_planning"]["proposed_bindings"][0]
    assert first_binding["selected_source_kind"] == second_binding["selected_source_kind"] == "materialization"
    assert first_binding["selected_capability_id"] == second_binding["selected_capability_id"]
    assert [item["candidate_id"] for item in first_binding["realization_candidates"]] == [
        item["candidate_id"] for item in second_binding["realization_candidates"]
    ]
    assert [item["candidate_id"] for item in first_binding["rejected_candidates"]] == [
        item["candidate_id"] for item in second_binding["rejected_candidates"]
    ]


def test_static_exact_hint_still_wins_with_rejected_candidate_details(client, monkeypatch) -> None:
    project = create_project(client)
    generated_candidate = _materialize_generated_candidate(client, project["id"], monkeypatch)

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
    blueprint = _create_blueprint(
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
    build, _ = _run_build(client, project["id"], blueprint["id"])

    binding = build["manifest_snapshot_json"]["metadata"]["build_planning"]["proposed_bindings"][0]
    assert binding["selected_source_kind"] == "static_registry"
    assert binding["selected_capability_id"] == "rank_candidates"
    assert binding["selected_candidate"]["candidate_id"] == "rank_candidates"
    assert any(item["candidate_id"] == generated_candidate["capability_id"] for item in binding["rejected_candidates"])
    assert any(
        "exact canonical static match" in item["rejection_reason"] or "preferred static capability" in item["rejection_reason"]
        for item in binding["rejected_candidates"]
    )


def test_repair_invalid_build_planning_fallback_still_runs_deterministic_reusable_selection(client, monkeypatch) -> None:
    project = create_project(client)
    generated_candidate = _materialize_generated_candidate(client, project["id"], monkeypatch)

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            "this is not valid json",
            "still not valid json",
        ],
    )
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {
                "step_name": "rank_candidates",
                "node_name": "ranker",
                "logical_step_kind": "rank",
                "required_capabilities": ["internal_ranker_helper"],
                "selected_reusable_capability_id": generated_candidate["capability_id"],
            },
        ],
    )
    build, executable = _run_build(client, project["id"], blueprint["id"])

    trace = build["manifest_snapshot_json"]["metadata"]["build_planning_trace"]
    assert trace["outcome"] == "fallback"
    assert trace["fallback_reason"] == "repair_invalid"

    planning = build["manifest_snapshot_json"]["metadata"]["build_planning"]
    assert planning["llm_status"] == "no_changes"
    binding = planning["proposed_bindings"][0]
    assert binding["selected_source_kind"] == "materialization"
    assert binding["selected_capability_id"] == generated_candidate["capability_id"]
    assert binding["selected_materialization_id"]
    assert "requested project-scoped capability hint" in binding["selection_reason"]

    bindings_doc = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "bindings.json"},
    ).json()["content"]
    assert '"selected_capability_id": "gen_internal_ranker_helper"' in bindings_doc
    assert '"source_kind": "materialization"' in bindings_doc
