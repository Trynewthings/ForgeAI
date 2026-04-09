from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import ROOT_DIR
from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_phase3_core import create_project
from tests.test_repository_analysis_v59 import _create_repo_fixture
from tests.test_runtime_capability_coverage_v51 import _start_and_complete_run


def _create_repo_archive_fixture(root_relative: str, archive_relative: str) -> tuple[str, str]:
    repo_root = _create_repo_fixture(root_relative)
    archive_base = ROOT_DIR / archive_relative
    archive_base.parent.mkdir(parents=True, exist_ok=True)
    archive_path = archive_base.with_suffix(".zip")
    archive_path.unlink(missing_ok=True)
    shutil.make_archive(str(archive_base), "zip", root_dir=ROOT_DIR / repo_root)
    return repo_root, str(archive_path.relative_to(ROOT_DIR)).replace("\\", "/")


def test_repository_checkout_from_archive_source_is_package_bound_and_confirmation_aware(
    client: TestClient,
    monkeypatch,
) -> None:
    _, archive_relative = _create_repo_archive_fixture(
        ".generated/runtime_v592/source_repo",
        ".generated/runtime_v592/repository_archive",
    )
    archive_uri = (ROOT_DIR / archive_relative).resolve().as_uri()
    checkout_relative = ".generated/runtime_v592/checked_out_repo"
    checkout_root = ROOT_DIR / checkout_relative
    if checkout_root.exists():
        shutil.rmtree(checkout_root)

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "planned",
              "confidence": 93,
              "proposed_bindings": [
                {
                  "step_name": "resolve_repository_source",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "repository_source_resolve"
                },
                {
                  "step_name": "checkout_repository_source",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "repository_checkout",
                  "config_hints": {
                    "source_step": "resolve_repository_source"
                  }
                },
                {
                  "step_name": "inventory_repository",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "repository_inventory",
                  "config_hints": {
                    "source_step": "checkout_repository_source",
                    "max_files": 20
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
            {"step_name": "resolve_repository_source", "node_name": "repo_source", "logical_step_kind": "ingest"},
            {"step_name": "checkout_repository_source", "node_name": "repo_checkout", "logical_step_kind": "ingest"},
            {"step_name": "inventory_repository", "node_name": "repo_inventory", "logical_step_kind": "ingest"},
        ],
    )
    _, executable = _run_build(client, project["id"], blueprint["id"])

    bindings_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "bindings.json"},
    )
    assert bindings_response.status_code == 200
    bindings_document = json.loads(bindings_response.json()["content"])
    checkout_binding = next(item for item in bindings_document["bindings"] if item["step_name"] == "checkout_repository_source")
    assert checkout_binding["selected_capability_id"] == "repository_checkout"
    assert checkout_binding["requires_confirmation"] is True

    completed_run, steps, _ = _start_and_complete_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={"repo_locator": archive_uri, "checkout_destination": checkout_relative},
    )
    assert completed_run["status"] == "completed"
    resolve_output = steps[0]["output_payload_json"]["structured_output"]
    checkout_output = steps[1]["output_payload_json"]["structured_output"]
    inventory_output = steps[2]["output_payload_json"]["structured_output"]

    assert resolve_output["source_kind"] == "archive_url"
    assert checkout_output["checkout_performed"] is True
    assert checkout_output["workspace_root"] == inventory_output["repo_root"]
    assert "pyproject.toml" in inventory_output["key_config_files"]
    assert inventory_output["coverage"]["included_file_count"] >= 4
