from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import ROOT_DIR
import app.services.run.run_execution_service as run_execution_service
from app.services.provider_runtime import ProviderResponse, ResolvedModelConfig
from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_runtime_capability_coverage_v51 import _start_and_complete_run
from tests.test_runtime_operations_v56 import _create_run
from tests.test_phase3_core import create_project, execute_job, wait_for_run_completion


def test_browser_click_then_save_text_to_workspace(client: TestClient, monkeypatch) -> None:
    root = ROOT_DIR / ".generated/runtime_v58/browser_save"
    root.mkdir(parents=True, exist_ok=True)
    (root / "landing.html").write_text(
        "<html><head><title>Landing</title></head><body><a href=\"article.html\" id=\"article-link\">Open article</a></body></html>",
        encoding="utf-8",
    )
    (root / "article.html").write_text(
        "<html><head><title>Article</title></head><body><p>Broader workflow browser text capture.</p></body></html>",
        encoding="utf-8",
    )
    output_relative = ".generated/runtime_v58/browser_save/article.txt"
    output_absolute = ROOT_DIR / output_relative
    if output_absolute.exists():
        output_absolute.unlink()

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            f"""
            {{
              "status": "planned",
              "confidence": 90,
              "proposed_bindings": [
                {{
                  "step_name": "open_article",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "browser_interaction",
                  "config_hints": {{
                    "action": "click",
                    "path": ".generated/runtime_v58/browser_save/landing.html",
                    "target_id": "article-link"
                  }}
                }},
                {{
                  "step_name": "save_article",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "browser_interaction",
                  "config_hints": {{
                    "action": "save_text",
                    "source_step": "open_article",
                    "output_path": "{output_relative}",
                    "allow_write": true
                  }}
                }}
              ]
            }}
            """,
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {"step_name": "open_article", "node_name": "browser", "logical_step_kind": "transform"},
            {"step_name": "save_article", "node_name": "browser", "logical_step_kind": "persist"},
        ],
    )
    build, executable = _run_build(client, project["id"], blueprint["id"])
    assert build["manifest_snapshot_json"]["metadata"]["execution_package"]["runnable_step_count"] == 2

    completed_run, steps, _ = _start_and_complete_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={},
    )
    assert completed_run["status"] == "completed"
    assert completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["active_scheduler"] == "workflow_graph_scheduler_v1"
    assert completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["active_scheduler_authority_source"] == "workflow_artifact.workflow_graph.linear_order"
    assert completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["supported"] is True
    assert completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["execution_trial_eligible"] is True
    assert (
        completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["read_model"]["ordered_step_names"]
        == ["open_article", "save_article"]
    )
    assert (
        completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["read_model"]["next_ready_step_name"]
        == "open_article"
    )
    assert (
        completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["read_model"]["graph_checkpoint_metadata"]["checkpoint_node_ids"]
        == []
    )
    assert (
        completed_run["resolved_config_snapshot_json"]["runtime_scheduler"]["workflow_graph_candidate"]["read_model"]["graph_checkpoint_metadata"]["ordered_checkpoint_candidates"][0]["step_name"]
        == "open_article"
    )
    assert completed_run["runtime_operation"]["scheduler_kind"] == "workflow_graph_scheduler_v1"
    assert completed_run["runtime_operation"]["pending_step_authority_source"] == "workflow_artifact.workflow_graph.linear_order"
    assert steps[0]["output_payload_json"]["structured_output"]["title"] == "Article"
    assert steps[1]["output_payload_json"]["structured_output"]["saved_path"] == output_relative
    assert "Broader workflow browser text capture." in output_absolute.read_text(encoding="utf-8")


def test_browser_save_then_terminal_postprocess(client: TestClient, monkeypatch) -> None:
    root = ROOT_DIR / ".generated/runtime_v58/browser_terminal"
    root.mkdir(parents=True, exist_ok=True)
    (root / "page.html").write_text(
        "<html><head><title>Terminal Source</title></head><body><p>alpha beta gamma delta</p></body></html>",
        encoding="utf-8",
    )
    saved_relative = ".generated/runtime_v58/browser_terminal/page.txt"
    saved_absolute = ROOT_DIR / saved_relative
    if saved_absolute.exists():
        saved_absolute.unlink()

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            f"""
            {{
              "status": "planned",
              "confidence": 88,
              "proposed_bindings": [
                {{
                  "step_name": "save_page",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "browser_interaction",
                  "config_hints": {{
                    "action": "save_text",
                    "path": ".generated/runtime_v58/browser_terminal/page.html",
                    "output_path": "{saved_relative}",
                    "allow_write": true
                  }}
                }},
                {{
                  "step_name": "count_words",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "terminal_command",
                  "config_hints": {{
                    "command_argv": ["wc", "-w", "{saved_relative}"],
                    "working_dir": ".",
                    "timeout_seconds": 3
                  }}
                }}
              ]
            }}
            """,
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[
            {"step_name": "save_page", "node_name": "browser", "logical_step_kind": "persist"},
            {"step_name": "count_words", "node_name": "terminal", "logical_step_kind": "transform"},
        ],
    )
    _, executable = _run_build(client, project["id"], blueprint["id"])
    completed_run, steps, _ = _start_and_complete_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={},
    )
    assert completed_run["status"] == "completed"
    assert steps[0]["output_payload_json"]["structured_output"]["saved_path"] == saved_relative
    assert steps[1]["output_payload_json"]["step_type"] == "terminal_command"
    assert steps[1]["output_payload_json"]["structured_output"]["success"] is True
    assert saved_relative in steps[1]["output_payload_json"]["structured_output"]["stdout"]


def test_write_artifact_supports_markdown_and_csv_outputs(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 86,
              "proposed_bindings": [
                {
                  "step_name": "rank_candidates",
                  "logical_step_kind": "rank",
                  "selected_capability_id": "rank_candidates",
                  "config_hints": {
                    "source_field": "candidates",
                    "score_field": "score"
                  }
                },
                {
                  "step_name": "write_markdown",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "write_artifact",
                  "config_hints": {
                    "artifact_filename": "summary.md",
                    "artifact_format": "markdown",
                    "source_field": "note"
                  }
                },
                {
                  "step_name": "write_csv",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "write_artifact",
                  "config_hints": {
                    "artifact_filename": "ranked.csv",
                    "artifact_format": "csv",
                    "source_step": "rank_candidates",
                    "source_field": "ranked_candidates",
                    "csv_fields": ["name", "score"]
                  }
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
            {"step_name": "rank_candidates", "node_name": "ranker", "logical_step_kind": "rank"},
            {"step_name": "write_markdown", "node_name": "writer", "logical_step_kind": "persist"},
            {"step_name": "write_csv", "node_name": "writer", "logical_step_kind": "persist"},
        ],
    )
    _, executable = _run_build(client, project["id"], blueprint["id"])
    completed_run, steps, artifacts = _start_and_complete_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={
            "note": "# Workflow Output\n\nGenerated in markdown.",
            "candidates": [
                {"name": "beta", "score": 9},
                {"name": "alpha", "score": 3},
            ],
        },
    )
    assert completed_run["status"] == "completed"
    markdown_output = steps[1]["output_payload_json"]["structured_output"]["artifacts"][0]
    csv_output = steps[2]["output_payload_json"]["structured_output"]["artifacts"][0]
    assert markdown_output["content_format"] == "markdown"
    assert csv_output["content_format"] == "text"
    markdown_artifact = next(item for item in artifacts if item["artifact_type"] == "written_artifact_markdown")
    csv_artifact = next(item for item in artifacts if item["artifact_type"] == "written_artifact_csv")
    assert "# Workflow Output" in markdown_artifact["path_or_content"]
    assert "name,score" in csv_artifact["path_or_content"]


def test_execution_package_metadata_exposes_runtime_input_hints(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 88,
              "proposed_bindings": [
                {
                  "step_name": "collect_research_sources",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "web_search",
                  "config_hints": {}
                },
                {
                  "step_name": "count_words",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "terminal_command",
                  "config_hints": {}
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
            {"step_name": "collect_research_sources", "node_name": "research", "logical_step_kind": "ingest"},
            {"step_name": "count_words", "node_name": "terminal", "logical_step_kind": "transform"},
        ],
    )
    build, _ = _run_build(client, project["id"], blueprint["id"])
    hints = build["manifest_snapshot_json"]["metadata"]["execution_package"]["runtime_input_hints"]
    field_names = {(item["step_name"], item["field_name"]) for item in hints}
    assert ("collect_research_sources", "query") in field_names
    assert ("collect_research_sources", "source_urls") in field_names
    assert ("collect_research_sources", "allowed_domains") in field_names
    assert ("count_words", "command_argv") in field_names


def test_research_briefing_workflow_renders_markdown_before_artifact_delivery(client: TestClient, monkeypatch) -> None:
    original_llm_mode = run_execution_service.settings.llm_mode
    original_get_provider_adapter = run_execution_service.get_provider_adapter
    try:
        run_execution_service.settings.llm_mode = "real"

        class FakeAdapter:
            def generate(self, prompt: str) -> ProviderResponse:
                return ProviderResponse(
                    content=(
                        '{"summary":"AI notes were collected from the supplied sources.",'
                        '"key_findings":["The article focuses on recent AI developments."],'
                        '"unknowns":["No additional corroborating source was supplied."]}'
                    ),
                    prompt_tokens=48,
                    completion_tokens=36,
                    total_tokens=84,
                    raw_metadata={"mode": "fake-real", "provider": "openai", "model": "gpt-4o-mini"},
                )

        def fake_get_provider_adapter(model_config=None, overrides=None):
            return FakeAdapter(), ResolvedModelConfig(
                provider="openai",
                model_name="gpt-4o-mini",
                endpoint="https://api.openai.com/v1",
                api_key="masked",
                temperature=0.2,
                max_tokens=1024,
                source="test-fake-provider",
                llm_mode="real",
            )

        monkeypatch.setattr(run_execution_service, "get_provider_adapter", fake_get_provider_adapter)

        install_fake_build_planner_provider(
            monkeypatch,
            responses=[
                """
                {
                  "status": "planned",
                  "confidence": 90,
                  "proposed_bindings": [
                    {
                      "step_name": "collect_research_sources",
                      "logical_step_kind": "ingest",
                      "selected_capability_id": "web_search",
                      "config_hints": {
                        "query_field": "query",
                        "limit": 2
                      }
                    },
                    {
                      "step_name": "synthesize_research_briefing",
                      "logical_step_kind": "summarize",
                      "selected_capability_id": "summarize_text_llm",
                      "config_hints": {
                        "task_prompt": "Summarize the supplied research sources into a concise engineering-oriented briefing. Use only the provided sources and state uncertainty when evidence is incomplete.",
                        "expected_output": {
                          "summary": "string",
                          "key_findings": ["string"],
                          "unknowns": ["string"]
                        }
                      }
                    },
                    {
                      "step_name": "render_research_briefing",
                      "logical_step_kind": "persist",
                      "selected_capability_id": "research_briefing_render",
                      "config_hints": {
                        "source_step": "synthesize_research_briefing",
                        "title": "Research Briefing"
                      }
                    },
                    {
                      "step_name": "persist_research_briefing",
                      "logical_step_kind": "persist",
                      "selected_capability_id": "write_artifact",
                      "config_hints": {
                        "source_step": "render_research_briefing",
                        "source_field": "report_markdown",
                        "artifact_filename": "research_briefing.md",
                        "artifact_format": "markdown"
                      }
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
                {"step_name": "collect_research_sources", "node_name": "research_sources", "logical_step_kind": "ingest"},
                {"step_name": "synthesize_research_briefing", "node_name": "research_synthesis", "logical_step_kind": "summarize"},
                {"step_name": "render_research_briefing", "node_name": "research_render", "logical_step_kind": "persist"},
                {"step_name": "persist_research_briefing", "node_name": "research_delivery", "logical_step_kind": "persist"},
            ],
        )
        _, executable = _run_build(client, project["id"], blueprint["id"])
        completed_run, steps, artifacts = _start_and_complete_run(
            client,
            project_id=project["id"],
            blueprint_id=blueprint["id"],
            executable_id=executable["id"],
            run_input={"query": "AI"},
        )
        assert completed_run["status"] == "completed"
        rendered = steps[2]["output_payload_json"]["structured_output"]["report_markdown"]
        assert "# Research Briefing" in rendered
        assert "## Summary" in rendered
        assert "AI notes were collected from the supplied sources." in rendered
        artifact = next(item for item in artifacts if item["artifact_type"] == "written_artifact_markdown")
        assert "# Research Briefing" in artifact["path_or_content"]
        assert "## Key Findings" in artifact["path_or_content"]
        assert "The article focuses on recent AI developments." in artifact["path_or_content"]
    finally:
        run_execution_service.settings.llm_mode = original_llm_mode
        run_execution_service.get_provider_adapter = original_get_provider_adapter


def test_browser_save_text_respects_confirmation_runtime_ops(client: TestClient, monkeypatch) -> None:
    root = ROOT_DIR / ".generated/runtime_v58/browser_confirm"
    root.mkdir(parents=True, exist_ok=True)
    (root / "page.html").write_text(
        "<html><head><title>Confirm Save</title></head><body><p>Confirmation gated browser save.</p></body></html>",
        encoding="utf-8",
    )
    output_relative = ".generated/runtime_v58/browser_confirm/page.txt"
    output_absolute = ROOT_DIR / output_relative
    if output_absolute.exists():
        output_absolute.unlink()

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            f"""
            {{
              "status": "planned",
              "confidence": 87,
              "proposed_bindings": [
                {{
                  "step_name": "save_page",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "browser_interaction",
                  "config_hints": {{
                    "action": "save_text",
                    "path": ".generated/runtime_v58/browser_confirm/page.html",
                    "output_path": "{output_relative}",
                    "allow_write": true
                  }}
                }}
              ]
            }}
            """,
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(
        client,
        project["id"],
        steps=[{"step_name": "save_page", "node_name": "browser", "logical_step_kind": "persist"}],
    )
    _, executable = _run_build(client, project["id"], blueprint["id"])
    run = _create_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={},
    )
    started = client.post(f"/api/v1/runs/{run['id']}/start").json()
    assert execute_job(client, started["active_job_id"])["status"] == "completed"
    waiting = client.get(f"/api/v1/runs/{run['id']}").json()
    assert waiting["runtime_operation"]["operation_state"] == "waiting_for_confirmation"
    assert waiting["runtime_operation"]["confirmation_request"]["step_name"] == "save_page"
    assert waiting["runtime_operation"]["confirmation_request"]["authority_source"] == "workflow_artifact.execution_contract.human_gate_policy"
    assert not output_absolute.exists()

    approved = client.post(
        f"/api/v1/runs/{run['id']}/confirm",
        json={"decision": "approve", "step_name": "save_page", "auto_resume": True},
    ).json()
    assert execute_job(client, approved["active_job_id"])["status"] == "completed"
    completed = wait_for_run_completion(client, run["id"])
    assert completed["status"] == "completed"
    assert "Confirmation gated browser save." in output_absolute.read_text(encoding="utf-8")
