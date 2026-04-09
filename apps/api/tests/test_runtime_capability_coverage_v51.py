from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import ROOT_DIR
from app.services.run import case_workloads
from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_phase3_core import create_project, execute_job, wait_for_run_completion


def _start_and_complete_run(client: TestClient, *, project_id: str, blueprint_id: str, executable_id: str, run_input: dict) -> tuple[dict, list[dict], list[dict]]:
    run_response = client.post(
        "/api/v1/runs",
        json={
            "project_id": project_id,
            "blueprint_id": blueprint_id,
            "executable_id": executable_id,
            "triggered_by": "v51-test",
            "status": "pending",
            "input_snapshot_json": run_input,
        },
    )
    assert run_response.status_code == 201
    run = run_response.json()
    start_response = client.post(f"/api/v1/runs/{run['id']}/start")
    assert start_response.status_code == 200
    active_job_id = start_response.json()["active_job_id"]
    while True:
        executed_job = execute_job(client, active_job_id)
        assert executed_job["status"] in {"completed", "failed"}
        current_run = client.get(f"/api/v1/runs/{run['id']}").json()
        runtime_operation = current_run.get("runtime_operation", {})
        if runtime_operation.get("operation_state") == "waiting_for_confirmation":
            confirmation_request = runtime_operation.get("confirmation_request") or {}
            confirmation = client.post(
                f"/api/v1/runs/{run['id']}/confirm",
                json={
                    "decision": "approve",
                    "step_name": confirmation_request.get("step_name"),
                    "auto_resume": True,
                },
            )
            assert confirmation.status_code == 200
            active_job_id = confirmation.json()["active_job_id"]
            continue
        completed_run = wait_for_run_completion(client, run["id"])
        break
    steps = client.get(f"/api/v1/runs/{run['id']}/steps").json()
    artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()
    return completed_run, steps, artifacts

def test_execution_package_runs_web_search_capability(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 91,
              "proposed_bindings": [
                {
                  "step_name": "search_web",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "web_search",
                  "config_hints": {
                    "query_field": "query",
                    "limit": 3
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
        steps=[{"step_name": "search_web", "node_name": "searcher", "logical_step_kind": "ingest"}],
    )
    build, executable = _run_build(client, project["id"], blueprint["id"])
    package_metadata = build["manifest_snapshot_json"]["metadata"]["execution_package"]
    assert package_metadata["runnable_step_count"] == 1

    completed_run, steps, artifacts = _start_and_complete_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={"query": "runtime execution package"},
    )
    assert completed_run["status"] == "completed"
    assert steps[0]["output_payload_json"]["step_type"] == "web_search"
    assert steps[0]["output_payload_json"]["runtime_adapter"] == "bounded_web_search"
    assert len(steps[0]["output_payload_json"]["structured_output"]["results"]) >= 1
    step_artifact = next(item for item in artifacts if item["artifact_type"] == "search_web_output")
    assert step_artifact["metadata_json"]["selected_capability_id"] == "web_search"


def test_execution_package_web_search_accepts_allowed_domains_as_urls(client: TestClient, monkeypatch) -> None:
    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self) -> bytes:
            return b"<html><head><title>Remote Article</title></head><body><p>AI article body</p></body></html>"

        def geturl(self) -> str:
            return "https://www.ruanyifeng.com/blog/2026/03/weekly-issue-389.html"

        @property
        def headers(self):
            class _Headers:
                @staticmethod
                def get_content_type() -> str:
                    return "text/html"

            return _Headers()

    monkeypatch.setattr(case_workloads, "urlopen", lambda request, timeout=5: _FakeResponse())

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 91,
              "proposed_bindings": [
                {
                  "step_name": "search_web",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "web_search",
                  "config_hints": {
                    "query_field": "query",
                    "source_urls": ["https://www.ruanyifeng.com/blog/2026/03/weekly-issue-389.html"],
                    "allow_network": true,
                    "allowed_domains": ["https://www.ruanyifeng.com"]
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
        steps=[{"step_name": "search_web", "node_name": "searcher", "logical_step_kind": "ingest"}],
    )
    _, executable = _run_build(client, project["id"], blueprint["id"])

    completed_run, steps, _ = _start_and_complete_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={"query": "AI"},
    )
    assert completed_run["status"] == "completed"
    output = steps[0]["output_payload_json"]
    assert output["structured_output"]["source_mode"] == "network_documents"
    assert output["structured_output"]["network_used"] is True


def test_execution_package_runs_filesystem_write_and_read(client: TestClient, monkeypatch) -> None:
    relative_path = ".generated/runtime_v51/filesystem_note.txt"
    absolute_path = ROOT_DIR / relative_path
    if absolute_path.exists():
        absolute_path.unlink()

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            f"""
            {{
              "status": "planned",
              "confidence": 89,
              "proposed_bindings": [
                {{
                  "step_name": "write_note",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "filesystem_write",
                  "config_hints": {{
                    "action": "write",
                    "path": "{relative_path}",
                    "allow_write": true,
                    "source_field": "content"
                  }}
                }},
                {{
                  "step_name": "read_note",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "filesystem_read",
                  "config_hints": {{
                    "action": "read",
                    "path": "{relative_path}"
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
            {"step_name": "write_note", "node_name": "writer", "logical_step_kind": "persist"},
            {"step_name": "read_note", "node_name": "reader", "logical_step_kind": "ingest"},
        ],
    )
    _, executable = _run_build(client, project["id"], blueprint["id"])
    completed_run, steps, _ = _start_and_complete_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={"content": "filesystem coverage test"},
    )
    assert completed_run["status"] == "completed"
    assert steps[0]["output_payload_json"]["step_type"] == "filesystem"
    assert steps[1]["output_payload_json"]["structured_output"]["content"] == "filesystem coverage test"
    assert absolute_path.read_text(encoding="utf-8") == "filesystem coverage test"


def test_execution_package_runs_bounded_terminal_command(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 87,
              "proposed_bindings": [
                {
                  "step_name": "inspect_workspace",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "terminal_command",
                  "config_hints": {
                    "command_argv": ["pwd"],
                    "working_dir": "apps/api",
                    "timeout_seconds": 3
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
        steps=[{"step_name": "inspect_workspace", "node_name": "terminal", "logical_step_kind": "transform"}],
    )
    _, executable = _run_build(client, project["id"], blueprint["id"])
    completed_run, steps, artifacts = _start_and_complete_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={},
    )
    assert completed_run["status"] == "completed"
    assert steps[0]["output_payload_json"]["step_type"] == "terminal_command"
    assert steps[0]["output_payload_json"]["structured_output"]["success"] is True
    assert "apps/api" in steps[0]["output_payload_json"]["content"]
    step_artifact = next(item for item in artifacts if item["artifact_type"] == "inspect_workspace_output")
    assert step_artifact["metadata_json"]["runtime_adapter"] == "bounded_terminal"


def test_execution_package_runs_bounded_browser_extract_text(client: TestClient, monkeypatch) -> None:
    relative_path = ".generated/runtime_v51/browser_fixture.html"
    absolute_path = ROOT_DIR / relative_path
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_text(
        "<html><head><title>Browser Fixture</title></head><body><h1>Execution Package Browser Test</h1><a href=\"https://example.com\">example</a></body></html>",
        encoding="utf-8",
    )

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            f"""
            {{
              "status": "planned",
              "confidence": 83,
              "proposed_bindings": [
                {{
                  "step_name": "inspect_page",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "browser_interaction",
                  "config_hints": {{
                    "action": "extract_text",
                    "path": "{relative_path}"
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
        steps=[{"step_name": "inspect_page", "node_name": "browser", "logical_step_kind": "ingest"}],
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
    output = steps[0]["output_payload_json"]
    assert output["step_type"] == "browser_interaction"
    assert output["structured_output"]["title"] == "Browser Fixture"
    assert "Execution Package Browser Test" in output["structured_output"]["visible_text"]


def test_execution_package_runs_bounded_browser_click_navigation(client: TestClient, monkeypatch) -> None:
    root = ROOT_DIR / ".generated/runtime_v52/browser_click"
    root.mkdir(parents=True, exist_ok=True)
    (root / "landing.html").write_text(
        "<html><head><title>Landing</title></head><body><a href=\"details.html\" id=\"details-link\">Open details</a></body></html>",
        encoding="utf-8",
    )
    (root / "details.html").write_text(
        "<html><head><title>Details</title></head><body><p>Bounded browser click reached the details page.</p></body></html>",
        encoding="utf-8",
    )

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            f"""
            {{
              "status": "planned",
              "confidence": 88,
              "proposed_bindings": [
                {{
                  "step_name": "click_page",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "browser_interaction",
                  "config_hints": {{
                    "action": "click",
                    "path": ".generated/runtime_v52/browser_click/landing.html",
                    "target_id": "details-link"
                  }}
                }}
              ]
            }}
            """,
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(client, project["id"], steps=[{"step_name": "click_page", "node_name": "browser", "logical_step_kind": "ingest"}])
    build, executable = _run_build(client, project["id"], blueprint["id"])

    bindings_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "bindings.json"},
    )
    assert bindings_response.status_code == 200
    assert '"action_kind": "click"' in bindings_response.json()["content"]

    completed_run, steps, _ = _start_and_complete_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={},
    )
    assert completed_run["status"] == "completed"
    assert build["manifest_snapshot_json"]["metadata"]["execution_package"]["runnable_step_count"] == 1
    output = steps[0]["output_payload_json"]
    assert output["step_type"] == "browser_interaction"
    assert output["structured_output"]["action"] == "click"
    assert output["structured_output"]["title"] == "Details"
    assert "details page" in output["structured_output"]["visible_text"]


def test_execution_package_runs_bounded_browser_fill_and_submit(client: TestClient, monkeypatch) -> None:
    root = ROOT_DIR / ".generated/runtime_v52/browser_forms"
    root.mkdir(parents=True, exist_ok=True)
    (root / "search_form.html").write_text(
        """
        <html><head><title>Search Form</title></head><body>
          <form id="search-form" action="search_result.html" method="get">
            <input type="text" id="query-field" name="q" value="">
            <button id="submit-button" type="submit">Search</button>
          </form>
        </body></html>
        """,
        encoding="utf-8",
    )
    (root / "search_result.html").write_text(
        "<html><head><title>Search Result</title></head><body><p>Bounded browser submit reached the result page.</p></body></html>",
        encoding="utf-8",
    )

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            f"""
            {{
              "status": "planned",
              "confidence": 85,
              "proposed_bindings": [
                {{
                  "step_name": "fill_form",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "browser_interaction",
                  "config_hints": {{
                    "action": "fill",
                    "path": ".generated/runtime_v52/browser_forms/search_form.html",
                    "form_id": "search-form",
                    "target_id": "query-field",
                    "value": "runtime safety"
                  }}
                }},
                {{
                  "step_name": "submit_form",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "browser_interaction",
                  "config_hints": {{
                    "action": "submit",
                    "path": ".generated/runtime_v52/browser_forms/search_form.html",
                    "form_id": "search-form",
                    "form_values": {{
                      "q": "runtime safety"
                    }}
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
            {"step_name": "fill_form", "node_name": "browser", "logical_step_kind": "transform"},
            {"step_name": "submit_form", "node_name": "browser", "logical_step_kind": "ingest"},
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
    assert steps[0]["output_payload_json"]["structured_output"]["form_state"]["q"] == "runtime safety"
    assert steps[1]["output_payload_json"]["structured_output"]["action"] == "submit"
    assert steps[1]["output_payload_json"]["structured_output"]["title"] == "Search Result"


def test_execution_package_runs_bounded_browser_screenshot(client: TestClient, monkeypatch) -> None:
    relative_path = ".generated/runtime_v52/browser_screenshot.html"
    absolute_path = ROOT_DIR / relative_path
    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    absolute_path.write_text(
        "<html><head><title>Screenshot Fixture</title></head><body><p>Screenshot me</p></body></html>",
        encoding="utf-8",
    )

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            f"""
            {{
              "status": "planned",
              "confidence": 82,
              "proposed_bindings": [
                {{
                  "step_name": "capture_page",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "browser_interaction",
                  "config_hints": {{
                    "action": "screenshot",
                    "path": "{relative_path}"
                  }}
                }}
              ]
            }}
            """,
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(client, project["id"], steps=[{"step_name": "capture_page", "node_name": "browser", "logical_step_kind": "ingest"}])
    _, executable = _run_build(client, project["id"], blueprint["id"])
    completed_run, steps, artifacts = _start_and_complete_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={},
    )
    assert completed_run["status"] == "completed"
    output = steps[0]["output_payload_json"]
    assert output["structured_output"]["screenshot_format"] == "svg_preview"
    assert output["content"].startswith("<svg")
    step_artifact = next(item for item in artifacts if item["artifact_type"] == "capture_page_output")
    assert "<svg" in step_artifact["path_or_content"]


def test_execution_package_runs_bounded_fetch_url_when_network_allowed(client: TestClient, monkeypatch) -> None:
    from app.services.run import case_workloads

    class _FakeHeaders:
        @staticmethod
        def get_content_type() -> str:
            return "text/html"

    class _FakeResponse:
        def __init__(self) -> None:
            self.headers = _FakeHeaders()
            self.status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return b"<html><head><title>Fetch Fixture</title></head><body><p>Remote fetch content</p></body></html>"

        def geturl(self) -> str:
            return "https://example.com/page.html"

    monkeypatch.setattr(case_workloads, "urlopen", lambda request, timeout=5: _FakeResponse())

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 90,
              "proposed_bindings": [
                {
                  "step_name": "fetch_page",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "fetch_url",
                  "config_hints": {
                    "action": "fetch_page",
                    "url": "https://example.com/page.html",
                    "allow_network": true,
                    "allowed_domains": ["example.com"]
                  }
                }
              ]
            }
            """,
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(client, project["id"], steps=[{"step_name": "fetch_page", "node_name": "fetcher", "logical_step_kind": "ingest"}])
    build, executable = _run_build(client, project["id"], blueprint["id"])
    assert build["manifest_snapshot_json"]["metadata"]["execution_package"]["runnable_step_count"] == 1
    completed_run, steps, _ = _start_and_complete_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={},
    )
    assert completed_run["status"] == "completed"
    output = steps[0]["output_payload_json"]
    assert output["step_type"] == "network_fetch"
    assert output["runtime_adapter"] == "bounded_network_fetch"
    assert output["structured_output"]["network_used"] is True
    assert output["structured_output"]["title"] == "Fetch Fixture"


def test_execution_package_blocks_fetch_url_when_network_not_allowed(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 84,
              "proposed_bindings": [
                {
                  "step_name": "fetch_page",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "fetch_url",
                  "config_hints": {
                    "action": "fetch_page",
                    "url": "https://example.com/page.html",
                    "allowed_domains": ["example.com"]
                  }
                }
              ]
            }
            """,
        ],
    )
    project = create_project(client)
    blueprint = _create_blueprint(client, project["id"], steps=[{"step_name": "fetch_page", "node_name": "fetcher", "logical_step_kind": "ingest"}])
    _, executable = _run_build(client, project["id"], blueprint["id"])
    run_response = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable["id"],
            "triggered_by": "v52-test",
            "status": "pending",
            "input_snapshot_json": {},
        },
    )
    assert run_response.status_code == 201
    run = run_response.json()
    start_response = client.post(f"/api/v1/runs/{run['id']}/start")
    assert start_response.status_code == 200
    executed_job = execute_job(client, start_response.json()["active_job_id"])
    assert executed_job["status"] == "failed"
    completed_run = wait_for_run_completion(client, run["id"])
    assert completed_run["status"] == "failed"
    assert "allow_network=true" in completed_run["error_message"]


def test_terminal_command_policy_blocks_non_allowlisted_command(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 81,
              "proposed_bindings": [
                {
                  "step_name": "dangerous_terminal",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "terminal_command",
                  "config_hints": {
                    "command_argv": ["python3", "-V"]
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
        steps=[{"step_name": "dangerous_terminal", "node_name": "terminal", "logical_step_kind": "transform"}],
    )
    _, executable = _run_build(client, project["id"], blueprint["id"])
    run_response = client.post(
        "/api/v1/runs",
        json={
            "project_id": project["id"],
            "blueprint_id": blueprint["id"],
            "executable_id": executable["id"],
            "triggered_by": "v51-test",
            "status": "pending",
            "input_snapshot_json": {},
        },
    )
    assert run_response.status_code == 201
    run = run_response.json()
    start_response = client.post(f"/api/v1/runs/{run['id']}/start")
    assert start_response.status_code == 200
    executed_job = execute_job(client, start_response.json()["active_job_id"])
    assert executed_job["status"] == "failed"
    completed_run = wait_for_run_completion(client, run["id"])
    steps = client.get(f"/api/v1/runs/{run['id']}/steps").json()
    artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()
    assert completed_run["status"] == "failed"
    assert "allowlist" in completed_run["error_message"]
    assert steps[0]["status"] == "failed"
    assert any(item["artifact_type"] == "runtime_failure_report" for item in artifacts)


def test_browser_interaction_unsupported_action_is_review_needed_and_non_runnable(client: TestClient, monkeypatch) -> None:
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 78,
              "proposed_bindings": [
                {
                    "step_name": "click_page",
                    "logical_step_kind": "ingest",
                    "selected_capability_id": "browser_interaction",
                    "config_hints": {
                    "action": "drag"
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
        steps=[{"step_name": "click_page", "node_name": "browser", "logical_step_kind": "ingest"}],
    )
    build, executable = _run_build(client, project["id"], blueprint["id"])
    package_metadata = build["manifest_snapshot_json"]["metadata"]["execution_package"]
    assert package_metadata["runnable_step_count"] == 0
    assert package_metadata["review_required_step_count"] == 1

    bindings_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "bindings.json"},
    )
    assert bindings_response.status_code == 200
    assert '"binding_status": "review_needed"' in bindings_response.json()["content"]

    resolve_response = client.post(
        f"/api/v1/executables/{executable['id']}/resolve-run-plan",
        json={"run_input": {}},
    )
    assert resolve_response.status_code == 400
    assert "non-runnable steps" in resolve_response.json()["detail"]
