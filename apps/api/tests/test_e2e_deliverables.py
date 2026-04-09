"""End-to-end deliverable tests: verify workflow templates produce artifacts.

Mock mode — validates structure, not LLM output quality.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.workflow_templates import (
    dataset_assessment_blueprint,
    repo_analysis_blueprint,
    research_briefing_blueprint,
)
from tests.test_phase3_core import (
    create_project,
    execute_job,
    wait_for_v2_build_completion,
)


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _full_pipeline(client: TestClient, bp_payload: dict) -> tuple[dict, dict, dict]:
    """Run full pipeline: blueprint → build → run. Return (blueprint, executable, run)."""
    bp = client.post("/api/v1/blueprints", json=bp_payload).json()
    assert "id" in bp, f"Blueprint creation failed: {bp}"

    build = client.post("/api/v1/builds", json={
        "project_id": bp_payload["project_id"],
        "blueprint_id": bp["id"],
        "build_options_json": {"target": "test"},
    }).json()
    started = client.post(f"/api/v1/builds/{build['id']}/start").json()
    execute_job(client, started["active_job_id"])
    completed = wait_for_v2_build_completion(client, build["id"])
    exe = client.get(f"/api/v1/executables/{completed['executable_id']}").json()

    rp = client.post("/api/v1/runtime-profiles", json={
        "project_id": bp_payload["project_id"],
        "blueprint_id": bp["id"],
        "name": "E2E Test",
        "is_default": True,
    }).json()
    run = client.post("/api/v1/runs", json={
        "project_id": bp_payload["project_id"],
        "blueprint_id": bp["id"],
        "executable_id": exe["id"],
        "runtime_profile_id": rp["id"],
        "run_input": {"topic": "AI safety", "query": "test"},
    }).json()
    started_run = client.post(f"/api/v1/runs/{run['id']}/start").json()
    job_id = started_run.get("active_job_id")
    if job_id:
        execute_job(client, job_id)
    final = client.get(f"/api/v1/runs/{run['id']}").json()
    return bp, exe, final


class TestResearchBriefingDeliverable:

    def test_research_briefing_produces_artifact(self, client: TestClient):
        """Research briefing template should produce a run with markdown artifact step."""
        project = create_project(client)
        req = client.post("/api/v1/requirements", json={
            "project_id": project["id"], "title": "Research E2E",
        }).json()
        payload = research_briefing_blueprint(
            project_id=project["id"],
            requirement_id=req["id"],
            topic="AI safety developments",
        )
        bp, exe, final = _full_pipeline(client, payload)

        assert final["status"] in ("completed", "failed")
        steps = final.get("run_steps", [])
        assert len(steps) == 3
        step_names = {s["step_name"] for s in steps}
        assert step_names == {"gather_sources", "analyze_sources", "produce_briefing"}

        # The artifact writer step should have completed
        writer_step = next(s for s in steps if s["step_name"] == "produce_briefing")
        assert writer_step["status"] == "completed"

        # Check artifacts were generated
        artifacts = client.get(f"/api/v1/runs/{final['id']}/artifacts").json()
        artifact_types = [a["artifact_type"] for a in artifacts]
        # Should have at least the writer artifacts
        assert any("report" in t or "artifact" in t for t in artifact_types), (
            f"Expected report artifact, got: {artifact_types}"
        )


class TestRepoAnalysisDeliverable:

    def test_repo_analysis_produces_artifact(self, client: TestClient):
        """Repo analysis template should produce a run with architecture report artifact."""
        project = create_project(client)
        req = client.post("/api/v1/requirements", json={
            "project_id": project["id"], "title": "Repo E2E",
        }).json()
        payload = repo_analysis_blueprint(
            project_id=project["id"],
            requirement_id=req["id"],
            repo_path="/Users/llk/agentsFactory",
            modules=["api", "worker"],
        )
        bp, exe, final = _full_pipeline(client, payload)

        assert final["status"] in ("completed", "failed")
        steps = final.get("run_steps", [])
        # scan + 2 analyze + merge = 4 steps
        assert len(steps) == 4
        step_names = {s["step_name"] for s in steps}
        assert "scan_repo" in step_names
        assert "analyze_api" in step_names
        assert "analyze_worker" in step_names
        assert "merge_report" in step_names


class TestDatasetAssessmentDeliverable:

    def test_dataset_assessment_produces_artifact(self, client: TestClient):
        """Dataset assessment template should produce a quality report artifact."""
        project = create_project(client)
        req = client.post("/api/v1/requirements", json={
            "project_id": project["id"], "title": "Dataset E2E",
        }).json()
        payload = dataset_assessment_blueprint(
            project_id=project["id"],
            requirement_id=req["id"],
            file_path="test_data.csv",
        )
        bp, exe, final = _full_pipeline(client, payload)

        assert final["status"] in ("completed", "failed")
        steps = final.get("run_steps", [])
        # ingest + profile + detect + report = 4 steps
        assert len(steps) == 4
        step_names = {s["step_name"] for s in steps}
        assert step_names == {"ingest_data", "profile_schema", "detect_quality", "produce_report"}
