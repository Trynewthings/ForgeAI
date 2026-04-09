from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.core.config import ROOT_DIR
from tests.test_build_planning_v43 import _create_blueprint, _run_build, install_fake_build_planner_provider
from tests.test_runtime_capability_coverage_v51 import _start_and_complete_run
from tests.test_phase3_core import create_project


def _create_repo_fixture(root_relative: str) -> str:
    root = ROOT_DIR / root_relative
    (root / "app" / "services").mkdir(parents=True, exist_ok=True)
    (root / "app" / "models").mkdir(parents=True, exist_ok=True)
    (root / "jobs").mkdir(parents=True, exist_ok=True)
    (root / "README.md").write_text("# Demo Repo\n\nRepository analysis fixture.", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        """
[project]
name = "demo-repo"
version = "0.1.0"

[project.scripts]
demo-repo = "app.main:app"
        """.strip(),
        encoding="utf-8",
    )
    (root / ".env.example").write_text("DATABASE_URL=sqlite:///demo.db\n", encoding="utf-8")
    (root / "app" / "main.py").write_text(
        """
from fastapi import FastAPI
from app.services.user_service import get_user

app = FastAPI()

@app.get("/users/{user_id}")
def read_user(user_id: str):
    return get_user(user_id)
        """.strip(),
        encoding="utf-8",
    )
    (root / "app" / "services" / "user_service.py").write_text(
        """
from app.models.user import User

def get_user(user_id: str) -> dict:
    return User(user_id=user_id).model_dump()
        """.strip(),
        encoding="utf-8",
    )
    (root / "app" / "models" / "user.py").write_text(
        """
from pydantic import BaseModel

class User(BaseModel):
    user_id: str
        """.strip(),
        encoding="utf-8",
    )
    (root / "jobs" / "sync.py").write_text(
        """
def sync_users() -> None:
    print("sync")
        """.strip(),
        encoding="utf-8",
    )
    return root_relative


def test_repository_analysis_workflow_generates_evidence_bundle_and_report(client: TestClient, monkeypatch) -> None:
    repo_root = _create_repo_fixture(".generated/runtime_v59/repo_fixture")
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            f"""
            {{
              "status": "planned",
              "confidence": 92,
              "proposed_bindings": [
                {{
                  "step_name": "inventory_repo",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "repository_inventory",
                  "config_hints": {{
                    "repo_root": "{repo_root}",
                    "max_files": 20
                  }}
                }},
                {{
                  "step_name": "extract_structure",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "source_structure_extract",
                  "config_hints": {{
                    "repo_root": "{repo_root}",
                    "source_step": "inventory_repo"
                  }}
                }},
                {{
                  "step_name": "map_relationships",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "repository_relationship_map",
                  "config_hints": {{
                    "source_step": "extract_structure"
                  }}
                }},
                {{
                  "step_name": "inspect_execution_surfaces",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "repository_execution_surface_inspect",
                  "config_hints": {{
                    "repo_root": "{repo_root}",
                    "inventory_step": "inventory_repo",
                    "structure_step": "extract_structure"
                  }}
                }},
                {{
                  "step_name": "build_evidence_bundle",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "repository_evidence_bundle",
                  "config_hints": {{
                    "inventory_step": "inventory_repo",
                    "structure_step": "extract_structure",
                    "relationship_step": "map_relationships",
                    "execution_surface_step": "inspect_execution_surfaces"
                  }}
                }},
                {{
                  "step_name": "synthesize_report",
                  "logical_step_kind": "summarize",
                  "selected_capability_id": "architecture_report_synthesis",
                  "config_hints": {{
                    "source_step": "build_evidence_bundle",
                    "source_field": "evidence_bundle",
                    "objective": "Summarize the repository architecture."
                  }}
                }},
                {{
                  "step_name": "write_evidence_artifact",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "write_artifact",
                  "config_hints": {{
                    "artifact_filename": "repository_evidence_bundle.json",
                    "artifact_format": "json",
                    "source_step": "build_evidence_bundle",
                    "source_field": "evidence_bundle"
                  }}
                }},
                {{
                  "step_name": "write_report_artifact",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "write_artifact",
                  "config_hints": {{
                    "artifact_filename": "architecture_report.md",
                    "artifact_format": "markdown",
                    "source_step": "synthesize_report",
                    "source_field": "report_markdown"
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
            {"step_name": "inventory_repo", "node_name": "repo", "logical_step_kind": "ingest"},
            {"step_name": "extract_structure", "node_name": "repo", "logical_step_kind": "transform"},
            {"step_name": "map_relationships", "node_name": "repo", "logical_step_kind": "transform"},
            {"step_name": "inspect_execution_surfaces", "node_name": "repo", "logical_step_kind": "transform"},
            {"step_name": "build_evidence_bundle", "node_name": "repo", "logical_step_kind": "persist"},
            {"step_name": "synthesize_report", "node_name": "report", "logical_step_kind": "summarize"},
            {"step_name": "write_evidence_artifact", "node_name": "writer", "logical_step_kind": "persist"},
            {"step_name": "write_report_artifact", "node_name": "writer", "logical_step_kind": "persist"},
        ],
    )
    build, executable = _run_build(client, project["id"], blueprint["id"])
    assert build["manifest_snapshot_json"]["metadata"]["execution_package"]["runnable_step_count"] == 8

    bindings_response = client.get(
        f"/api/v1/executables/{executable['id']}/files/content",
        params={"path": "bindings.json"},
    )
    assert bindings_response.status_code == 200
    bindings_document = json.loads(bindings_response.json()["content"])
    assert any(item["selected_capability_id"] == "repository_inventory" for item in bindings_document["bindings"])
    assert any(item["selected_capability_id"] == "architecture_report_synthesis" for item in bindings_document["bindings"])

    completed_run, steps, artifacts = _start_and_complete_run(
        client,
        project_id=project["id"],
        blueprint_id=blueprint["id"],
        executable_id=executable["id"],
        run_input={},
    )
    assert completed_run["status"] == "completed"
    inventory_output = steps[0]["output_payload_json"]["structured_output"]
    assert inventory_output["repo_root"] == repo_root
    assert "pyproject.toml" in inventory_output["key_config_files"]
    structure_output = steps[1]["output_payload_json"]["structured_output"]
    assert structure_output["module_count"] >= 3
    relationship_output = steps[2]["output_payload_json"]["structured_output"]
    assert len(relationship_output["dependency_edges"]) >= 1
    execution_surface_output = steps[3]["output_payload_json"]["structured_output"]
    assert len(execution_surface_output["runtime_entry_points"]) >= 1
    evidence_output = steps[4]["output_payload_json"]["structured_output"]["evidence_bundle"]
    assert evidence_output["inventory_summary"]["file_count"] >= 4
    report_output = steps[5]["output_payload_json"]["structured_output"]
    assert "# Architecture Report" in report_output["report_markdown"]
    evidence_artifact = next(item for item in artifacts if item["artifact_type"] == "written_artifact_json")
    report_artifact = next(item for item in artifacts if item["artifact_type"] == "written_artifact_markdown")
    assert "repository_evidence_bundle.json" in evidence_artifact["metadata_json"]["filename"]
    assert "architecture_report.md" in report_artifact["metadata_json"]["filename"]
    assert "Execution Surfaces" in report_artifact["path_or_content"]


def test_repository_inventory_partial_coverage_and_confirmed_report_save(client: TestClient, monkeypatch) -> None:
    repo_root = _create_repo_fixture(".generated/runtime_v59/repo_fixture_partial")
    report_copy_relative = ".generated/runtime_v59/repo_fixture_partial/report_copy.md"
    report_copy_absolute = ROOT_DIR / report_copy_relative
    if report_copy_absolute.exists():
        report_copy_absolute.unlink()

    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            f"""
            {{
              "status": "planned",
              "confidence": 88,
              "proposed_bindings": [
                {{
                  "step_name": "inventory_repo",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "repository_inventory",
                  "config_hints": {{
                    "repo_root": "{repo_root}",
                    "max_files": 2
                  }}
                }},
                {{
                  "step_name": "build_evidence_bundle",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "repository_evidence_bundle",
                  "config_hints": {{
                    "inventory_step": "inventory_repo"
                  }}
                }},
                {{
                  "step_name": "synthesize_report",
                  "logical_step_kind": "summarize",
                  "selected_capability_id": "architecture_report_synthesis",
                  "config_hints": {{
                    "source_step": "build_evidence_bundle",
                    "source_field": "evidence_bundle"
                  }}
                }},
                {{
                  "step_name": "save_report_copy",
                  "logical_step_kind": "persist",
                  "selected_capability_id": "filesystem_write",
                  "config_hints": {{
                    "action": "write",
                    "path": "{report_copy_relative}",
                    "allow_write": true,
                    "source_step": "synthesize_report",
                    "source_field": "report_markdown"
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
            {"step_name": "inventory_repo", "node_name": "repo", "logical_step_kind": "ingest"},
            {"step_name": "build_evidence_bundle", "node_name": "repo", "logical_step_kind": "persist"},
            {"step_name": "synthesize_report", "node_name": "report", "logical_step_kind": "summarize"},
            {"step_name": "save_report_copy", "node_name": "writer", "logical_step_kind": "persist"},
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
    inventory_output = steps[0]["output_payload_json"]["structured_output"]
    assert inventory_output["partial"] is True
    evidence_output = steps[1]["output_payload_json"]["structured_output"]
    assert evidence_output["partial"] is True
    assert any("Partial coverage affected" in item for item in evidence_output["unknowns"])
    assert report_copy_absolute.exists()
    assert "Coverage status: partial" in report_copy_absolute.read_text(encoding="utf-8")


def test_repository_execution_surface_inspection_uses_declared_dependency_repo_root(client: TestClient, monkeypatch) -> None:
    repo_root = _create_repo_fixture(".generated/runtime_v59/repo_fixture_declared_edges")
    install_fake_build_planner_provider(
        monkeypatch,
        responses=[
            f"""
            {{
              "status": "planned",
              "confidence": 90,
              "proposed_bindings": [
                {{
                  "step_name": "inventory_repo",
                  "logical_step_kind": "ingest",
                  "selected_capability_id": "repository_inventory",
                  "config_hints": {{
                    "repo_root": "{repo_root}",
                    "max_files": 20
                  }}
                }},
                {{
                  "step_name": "extract_structure",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "source_structure_extract",
                  "config_hints": {{
                    "source_step": "inventory_repo"
                  }}
                }},
                {{
                  "step_name": "inspect_execution_surfaces",
                  "logical_step_kind": "transform",
                  "selected_capability_id": "repository_execution_surface_inspect",
                  "config_hints": {{
                    "inventory_step": "inventory_repo",
                    "structure_step": "extract_structure"
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
            {"step_name": "inventory_repo", "node_name": "repo", "logical_step_kind": "ingest"},
            {"step_name": "extract_structure", "node_name": "repo", "logical_step_kind": "transform"},
            {"step_name": "inspect_execution_surfaces", "node_name": "repo", "logical_step_kind": "transform"},
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
    inventory_output = steps[0]["output_payload_json"]["structured_output"]
    execution_surface_output = steps[2]["output_payload_json"]["structured_output"]
    assert inventory_output["repo_root"] == repo_root
    assert len(execution_surface_output["config_surfaces"]) >= 1
    assert any(item["path"].endswith("app/main.py") for item in execution_surface_output["runtime_entry_points"])
