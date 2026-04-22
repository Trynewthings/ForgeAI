"""Smoke: walk the main chain step-by-step and record where it breaks.

Two chains coexist in the codebase right now:

    Chain A (Blueprint-anchored, canonical today):
        project -> blueprint -> build -> executable -> runtime_profile -> run

    Chain B (session-anchored, partial post front-chain v2):
        project -> session -> turn(raw_intent) -> compile -> plan (planner_output)

Both are expected in the smoke. We exercise each leg with mock LLM and
record a factual pass / break table so the next decision (compose a
bridge vs. dive into coding_agent demo) is grounded in evidence, not
assumptions.

Run:

    cd apps/api && source .venv/bin/activate && \\
        python tests/smoke_main_chain.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("BUILD_EXECUTION_MODE", "thread")
os.environ.setdefault("CORS_ORIGINS", '["http://localhost:1420","http://127.0.0.1:1420"]')


RESULTS: list[dict[str, Any]] = []


def record(stage: str, status: str, detail: str, extra: dict | None = None) -> None:
    row = {"stage": stage, "status": status, "detail": detail}
    if extra:
        row["extra"] = extra
    RESULTS.append(row)
    marker = {"pass": "[PASS]", "break": "[BREAK]", "skip": "[SKIP]", "info": "[INFO]"}.get(status, f"[{status}]")
    print(f"  {marker} {stage}: {detail}")


def chain_a_blueprint_to_run(client) -> None:
    print("\n" + "=" * 64)
    print("  Chain A — Blueprint → Build → Executable → Run (mock LLM)")
    print("=" * 64)

    from app.services.workflow_templates import research_briefing_blueprint
    from app.services.job.job_execution_service import JobExecutionService

    # Project
    try:
        proj = client.post(
            "/api/v1/projects",
            json={"name": "Smoke ChainA", "description": ""},
        )
        assert proj.status_code == 201, proj.text
        project_id = proj.json()["id"]
        record("A.project", "pass", f"project_id={project_id}")
    except Exception as e:
        record("A.project", "break", str(e))
        return

    # Requirement (template helper needs one)
    try:
        req = client.post(
            "/api/v1/requirements",
            json={"project_id": project_id, "title": "Smoke topic"},
        )
        assert req.status_code == 201, req.text
        requirement_id = req.json()["id"]
        record("A.requirement", "pass", f"requirement_id={requirement_id}")
    except Exception as e:
        record("A.requirement", "break", str(e))
        return

    # Blueprint (template)
    try:
        payload = research_briefing_blueprint(
            project_id=project_id,
            requirement_id=requirement_id,
            topic="Smoke topic",
        )
        bp = client.post("/api/v1/blueprints", json=payload)
        assert bp.status_code == 201, bp.text
        blueprint_id = bp.json()["id"]
        record("A.blueprint", "pass", f"blueprint_id={blueprint_id} (research_briefing template)")
    except Exception as e:
        record("A.blueprint", "break", str(e))
        return

    # Build create
    try:
        build_resp = client.post(
            "/api/v1/builds",
            json={
                "project_id": project_id,
                "blueprint_id": blueprint_id,
                "build_options_json": {"target": "smoke"},
            },
        )
        assert build_resp.status_code == 201, build_resp.text
        build_id = build_resp.json()["id"]
        record("A.build.create", "pass", f"build_id={build_id} status=pending")
    except Exception as e:
        record("A.build.create", "break", str(e))
        return

    # Build start + synchronous execution
    try:
        started = client.post(f"/api/v1/builds/{build_id}/start")
        assert started.status_code == 200, started.text
        active_job_id = started.json().get("active_job_id")
        record("A.build.start", "pass", f"job_id={active_job_id}")
    except Exception as e:
        record("A.build.start", "break", str(e))
        return

    # Execute the build synchronously (bypass worker) so we can observe the signature point.
    try:
        JobExecutionService().execute_job(active_job_id)
        record("A.build.execute", "pass", "execute_job returned without exception")
    except Exception as e:
        record("A.build.execute", "break", f"{type(e).__name__}: {e}")
        # fall through — still inspect what landed on the build object

    # Build final state
    try:
        final_build = client.get(f"/api/v1/builds/{build_id}").json()
        record(
            "A.build.final",
            "pass" if final_build["status"] == "completed" else "break",
            f"status={final_build['status']} err={final_build.get('error_message')}",
        )
        executable_id = final_build.get("executable_id")
    except Exception as e:
        record("A.build.final", "break", str(e))
        return

    if not executable_id or final_build["status"] != "completed":
        record("A.executable", "skip", "build did not complete; skipping executable / run")
        return
    record("A.executable", "pass", f"executable_id={executable_id}")

    # Runtime profile
    try:
        rp = client.post(
            "/api/v1/runtime-profiles",
            json={
                "project_id": project_id,
                "blueprint_id": blueprint_id,
                "name": "Smoke",
                "is_default": True,
            },
        )
        assert rp.status_code == 201, rp.text
        runtime_profile_id = rp.json()["id"]
        record("A.runtime_profile", "pass", f"profile_id={runtime_profile_id}")
    except Exception as e:
        record("A.runtime_profile", "break", str(e))
        return

    # Run
    try:
        run_resp = client.post(
            "/api/v1/runs",
            json={
                "project_id": project_id,
                "blueprint_id": blueprint_id,
                "executable_id": executable_id,
                "runtime_profile_id": runtime_profile_id,
                "input_snapshot_json": {"topic": "Smoke topic"},
            },
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["id"]
        record("A.run.create", "pass", f"run_id={run_id}")
    except Exception as e:
        record("A.run.create", "break", str(e))
        return

    try:
        started_run = client.post(f"/api/v1/runs/{run_id}/start")
        assert started_run.status_code == 200, started_run.text
        run_job_id = started_run.json().get("active_job_id")
        JobExecutionService().execute_job(run_job_id)
        final = client.get(f"/api/v1/runs/{run_id}").json()
        record(
            "A.run.final",
            "pass" if final["status"] in {"completed", "waiting_for_intervention"} else "break",
            f"status={final['status']} steps={len(final.get('run_steps', []))}",
            extra={"run_status": final["status"]},
        )
    except Exception as e:
        record("A.run.final", "break", f"{type(e).__name__}: {e}")


def chain_b_session_to_plan(client) -> None:
    print("\n" + "=" * 64)
    print("  Chain B — Session → compile → plan (mock LLM)")
    print("=" * 64)

    try:
        proj = client.post(
            "/api/v1/projects",
            json={"name": "Smoke ChainB", "description": ""},
        )
        assert proj.status_code == 201, proj.text
        project_id = proj.json()["id"]
        record("B.project", "pass", f"project_id={project_id}")
    except Exception as e:
        record("B.project", "break", str(e))
        return

    try:
        s = client.post(
            "/api/v1/front-chain/sessions", json={"project_id": project_id}
        )
        assert s.status_code == 201, s.text
        session_id = s.json()["id"]
        record("B.session", "pass", f"session_id={session_id}")
    except Exception as e:
        record("B.session", "break", str(e))
        return

    # Turn — this runs interpret -> evaluate -> compile in one call
    try:
        turn = client.post(
            f"/api/v1/front-chain/sessions/{session_id}/turn",
            json={
                "kind": "raw_intent",
                "raw_intent": (
                    "Analyze the repository at https://github.com/example/foo "
                    "and produce a markdown architecture report."
                ),
            },
        )
        assert turn.status_code == 201, turn.text
        body = turn.json()
        status = body.get("status")
        stages = body.get("stages_run", [])
        record(
            f"B.turn.status={status}",
            "pass" if status == "compilable" else "info",
            f"stages_run={stages}",
            extra={"session_status": status},
        )
    except Exception as e:
        record("B.turn", "break", f"{type(e).__name__}: {e}")
        return

    if status != "compilable":
        record("B.plan", "skip", f"session not compilable yet (status={status}); /plan would 422")
        return

    # Plan
    try:
        plan = client.post(f"/api/v1/front-chain/sessions/{session_id}/plan")
        assert plan.status_code == 201, plan.text
        pbody = plan.json()
        node_count = len(pbody.get("planner_output", {}).get("node_drafts", []))
        record(
            "B.plan",
            "pass",
            f"planner_version={pbody.get('planner_version')} nodes={node_count}",
            extra={"planner_output": pbody.get("planner_output", {})},
        )
    except Exception as e:
        record("B.plan", "break", f"{type(e).__name__}: {e}")
        return

    # Session-anchored C-slice: POST /builds with session_id.
    try:
        from app.services.job.job_execution_service import JobExecutionService

        build_resp = client.post(
            "/api/v1/builds",
            json={
                "project_id": project_id,
                "session_id": session_id,
                "build_options_json": {"target": "c_slice_smoke"},
            },
        )
        if build_resp.status_code != 201:
            record("B.build_from_session", "break", build_resp.text[:200])
            return
        build_id = build_resp.json()["id"]
        record("B.build.create", "pass", f"build_id={build_id}")
        started = client.post(f"/api/v1/builds/{build_id}/start").json()
        JobExecutionService().execute_job(started["active_job_id"])
        final_build = client.get(f"/api/v1/builds/{build_id}").json()
        record(
            "B.build.final",
            "pass" if final_build["status"] == "completed" else "break",
            f"status={final_build['status']} err={final_build.get('error_message')}",
        )
        exec_id = final_build.get("executable_id")
        if exec_id:
            record("B.executable", "pass", f"executable_id={exec_id}")
    except Exception as e:
        record("B.build_from_session", "break", f"{type(e).__name__}: {e}")


def summarize() -> None:
    print("\n" + "=" * 64)
    print("  Fact table")
    print("=" * 64)
    for row in RESULTS:
        tag = {"pass": "✔", "break": "✘", "skip": "·", "info": "~"}.get(row["status"], "?")
        print(f"  {tag} {row['stage']:<40} {row['detail']}")
    breaks = [r for r in RESULTS if r["status"] == "break"]
    print("\n" + "=" * 64)
    print(f"  Summary: {len(breaks)} breakpoint(s)")
    if breaks:
        for r in breaks:
            print(f"    - {r['stage']}: {r['detail']}")
    print("=" * 64 + "\n")


def main() -> int:
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session, sessionmaker

    import app.services.build.build_execution_service as build_mod
    import app.services.job.job_dispatch_service as job_dispatch_mod
    import app.services.job.job_execution_service as job_exec_mod
    import app.services.run.run_execution_service as run_mod
    from app.core.database import get_db
    from app.db.base import Base
    from app.main import app

    # SQLite in-memory per process (same conftest pattern)
    tmp = Path(sys.modules["tempfile"].mkdtemp()) if False else Path("/tmp/smoke_chain_db.sqlite")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    engine = create_engine(
        f"sqlite:///{tmp}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, _rec) -> None:
        c = dbapi_connection.cursor()
        c.execute("PRAGMA foreign_keys=ON")
        c.close()

    TestingSessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, class_=Session
    )
    Base.metadata.create_all(bind=engine)

    def _override_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_db
    build_mod.SessionLocal = TestingSessionLocal
    run_mod.SessionLocal = TestingSessionLocal
    job_exec_mod.SessionLocal = TestingSessionLocal
    build_mod.settings.build_step_delay_seconds = 0.01
    run_mod.settings.build_step_delay_seconds = 0.01
    run_mod.settings.llm_mode = "mock"
    job_dispatch_mod.JobDispatchService.dispatch = lambda self, job_id: ("test", f"task-{job_id}")

    with TestClient(app) as client:
        chain_a_blueprint_to_run(client)
        chain_b_session_to_plan(client)
    summarize()
    return 0


if __name__ == "__main__":
    sys.exit(main())
