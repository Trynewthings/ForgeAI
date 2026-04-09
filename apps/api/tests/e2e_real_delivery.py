"""Real LLM delivery evaluation — produces actual artifacts.

Usage:
    cd apps/api && source .venv/bin/activate
    python tests/e2e_real_delivery.py

Requires: LLM_MODE=real, DeepSeek API configured.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["LLM_MODE"] = "real"

from app.services.workflow_templates import research_briefing_blueprint


def main():
    from fastapi.testclient import TestClient
    from app.main import app
    from app.core.config import settings

    print(f"\nLLM_MODE={settings.llm_mode}, Model={settings.default_llm_model_name}\n")

    with TestClient(app) as client:
        # Create project
        project = client.post("/api/v1/projects", json={"name": "Real Delivery Test", "description": ""}).json()

        # Research Briefing
        print("=" * 60)
        print("  SCENARIO: Research Briefing")
        print("=" * 60)

        req = client.post("/api/v1/requirements", json={
            "project_id": project["id"], "title": "AI Safety Briefing",
        }).json()
        payload = research_briefing_blueprint(
            project_id=project["id"],
            requirement_id=req["id"],
            topic="AI safety developments in 2025",
        )
        bp = client.post("/api/v1/blueprints", json=payload).json()
        build = client.post("/api/v1/builds", json={
            "project_id": project["id"],
            "blueprint_id": bp["id"],
            "build_options_json": {"target": "test"},
        }).json()
        started = client.post(f"/api/v1/builds/{build['id']}/start").json()

        from app.services.job.job_execution_service import JobExecutionService
        try:
            JobExecutionService().execute_job(started["active_job_id"])
        except Exception:
            pass

        completed = None
        for _ in range(20):
            b = client.get(f"/api/v1/builds/{build['id']}").json()
            if b["status"] in ("completed", "failed"):
                completed = b
                break
            time.sleep(0.1)

        if not completed or completed["status"] != "completed":
            print("  BUILD FAILED")
            return

        exe = client.get(f"/api/v1/executables/{completed['executable_id']}").json()

        rp = client.post("/api/v1/runtime-profiles", json={
            "project_id": project["id"], "blueprint_id": bp["id"],
            "name": "Real", "is_default": True,
        }).json()
        run = client.post("/api/v1/runs", json={
            "project_id": project["id"], "blueprint_id": bp["id"],
            "executable_id": exe["id"], "runtime_profile_id": rp["id"],
            "run_input": {"topic": "AI safety developments in 2025"},
        }).json()
        started_run = client.post(f"/api/v1/runs/{run['id']}/start").json()
        job_id = started_run.get("active_job_id")

        t0 = time.time()
        if job_id:
            try:
                JobExecutionService().execute_job(job_id)
            except Exception:
                pass

        final = client.get(f"/api/v1/runs/{run['id']}").json()
        elapsed = time.time() - t0

        print(f"\n  Status: {final['status']}")
        print(f"  Elapsed: {elapsed:.1f}s")
        print(f"  Steps: {len(final.get('run_steps', []))}")

        for step in final.get("run_steps", []):
            out = step.get("output_payload_json", {})
            summary = out.get("summary", "")[:100]
            content_len = len(out.get("content", ""))
            print(f"    {step['step_name']}: {step['status']} — {summary}... ({content_len} chars)")

        # Check artifacts
        artifacts = client.get(f"/api/v1/runs/{run['id']}/artifacts").json()
        print(f"\n  Artifacts: {len(artifacts)}")
        for a in artifacts:
            content = a.get("path_or_content", "")
            print(f"    {a['artifact_type']}: {len(content)} chars")
            if "markdown" in a.get("artifact_type", "") or "report" in a.get("artifact_type", ""):
                print(f"\n--- ARTIFACT PREVIEW ---")
                print(content[:1500])
                print(f"--- END PREVIEW ({len(content)} total chars) ---\n")


if __name__ == "__main__":
    main()
