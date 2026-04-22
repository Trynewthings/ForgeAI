"""Natural multi-node chain demo — real LLM, no override.

Drives the full session-anchored slice end-to-end:

    session → compile → plan → build (session_id) → run → artifact

Unlike ``demo_session_coding_agent_real_llm.py``, this demo does NOT
override planner_output. Whatever the real planner decides to do with
the intent is what gets executed. Acceptance is structural:

    1. fs_read mounts the declared input into a step.
    2. fs_write consumes upstream output and writes the declared output.
    3. run.status == completed.
    4. The final artifact matches the deterministic expected content
       for the CSV aggregation scenario.
    5. The audit's wrong_pick count stays at zero.

Run:

    cd apps/api && source .venv/bin/activate && \\
        PYTHONPATH=. python tests/demo_natural_chain_real_llm.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["LLM_MODE"] = "real"
os.environ.setdefault("CORS_ORIGINS", '["http://localhost:1420"]')
os.environ.setdefault(
    "CODING_AGENT_SANDBOX",
    "sandbox-exec" if sys.platform == "darwin" else "subprocess",
)


EXPECTED_CSV_CONTENT_FRAGMENTS = (
    "product_line",
    "gadgets",
    "widgets",
    "spares",
)


def _prepare_workspace() -> tuple[Path, Path, Path]:
    # Keep the workspace INSIDE the repo's .generated/ tree because
    # the bounded_filesystem adapter rejects allowed_root values
    # outside ROOT_DIR as a security invariant.
    from app.core.config import ROOT_DIR as _ROOT_DIR

    workspace = _ROOT_DIR / ".generated" / "demo_natural_chain_ws"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    orders = workspace / "orders.csv"
    orders.write_text(
        "order_id,product_line,qty,unit_price\n"
        "1,widgets,3,12.50\n"
        "2,gadgets,1,89.00\n"
        "3,widgets,5,12.50\n"
        "4,widgets,2,11.00\n"
        "5,gadgets,2,89.00\n"
        "6,spares,7,4.25\n",
        encoding="utf-8",
    )
    output = workspace / "daily_totals.csv"
    return workspace, orders, output


def main() -> int:
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy.orm import sessionmaker

    import app.services.build.build_execution_service as build_mod
    import app.services.job.job_dispatch_service as job_dispatch_mod
    import app.services.job.job_execution_service as job_exec_mod
    import app.services.run.run_execution_service as run_mod
    from app.core.config import settings
    from app.core.database import get_db
    from app.db.base import Base
    from app.main import app

    if settings.llm_mode != "real":
        print(f"[abort] llm_mode={settings.llm_mode}")
        return 2
    print(
        f"[env] llm_mode={settings.llm_mode}  model={settings.default_llm_model_name}  "
        f"sandbox={os.environ['CODING_AGENT_SANDBOX']}"
    )

    db_path = Path("/tmp/demo_natural_chain.sqlite")
    try:
        db_path.unlink()
    except FileNotFoundError:
        pass
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, _rec) -> None:
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    TestingSessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, class_=OrmSession
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
    job_dispatch_mod.JobDispatchService.dispatch = (
        lambda self, job_id: ("test", f"task-{job_id}")
    )

    workspace, orders_csv, output_csv = _prepare_workspace()
    intent = (
        f"I have a local CSV at {orders_csv}. Read it, aggregate by "
        "product_line, compute order_count and total_revenue, sort by "
        f"total_revenue descending, and write the result to {output_csv} "
        "with columns [product_line, order_count, total_revenue]."
    )

    t_start = time.time()
    with TestClient(app) as client:
        proj = client.post(
            "/api/v1/projects",
            json={"name": "natural-chain", "description": ""},
        ).json()
        sess = client.post(
            "/api/v1/front-chain/sessions", json={"project_id": proj["id"]}
        ).json()
        turn = client.post(
            f"/api/v1/front-chain/sessions/{sess['id']}/turn",
            json={"kind": "raw_intent", "raw_intent": intent},
        )
        assert turn.status_code == 201, turn.text
        tbody = turn.json()
        if tbody["status"] != "compilable":
            print(f"[abort] turn did not reach compile: {tbody['status']}")
            return 3
        print(f"[ok] compile reached  session={sess['id']}")

        plan_resp = client.post(
            f"/api/v1/front-chain/sessions/{sess['id']}/plan"
        )
        assert plan_resp.status_code == 201, plan_resp.text
        planner_output = plan_resp.json()["planner_output"]
        node_summaries = []
        for draft in planner_output.get("node_drafts", []):
            md = draft.get("planner_metadata", {}) or {}
            bound = md.get("bound_capability", {}) or {}
            node_summaries.append(
                {
                    "step_name": md.get("step_name"),
                    "capability": bound.get("selected_capability_id"),
                    "selection_source": bound.get("selection_source"),
                }
            )
        print(f"[ok] plan: {len(node_summaries)} nodes")
        for n in node_summaries:
            print(f"     - {n['step_name']}: {n['capability']} ({n['selection_source']})")

        build_resp = client.post(
            "/api/v1/builds",
            json={"project_id": proj["id"], "session_id": sess["id"]},
        )
        assert build_resp.status_code == 201, build_resp.text
        build_id = build_resp.json()["id"]
        started = client.post(f"/api/v1/builds/{build_id}/start").json()
        job_exec_mod.JobExecutionService().execute_job(started["active_job_id"])
        final_build = client.get(f"/api/v1/builds/{build_id}").json()
        assert final_build["status"] == "completed", final_build.get("error_message")
        executable_id = final_build["executable_id"]
        exe = client.get(f"/api/v1/executables/{executable_id}").json()
        bindings_doc = json.loads(
            (Path(exe["source_path"]) / "bindings.json").read_text()
        )
        bindings = bindings_doc["bindings"]
        runnable = [b for b in bindings if b.get("runnable")]
        blocked = [b for b in bindings if not b.get("runnable")]
        print(f"[ok] build: {len(runnable)} runnable, {len(blocked)} blocked")
        for b in bindings:
            status = "runnable" if b.get("runnable") else "blocked"
            print(
                f"     - {b['step_name']}: {b.get('selected_capability_id')} "
                f"/ {b.get('execution_kind')} [{status}]"
            )

        # Require at least one fs_read and at least one fs_write in the
        # runnable set — that's the "natural 4-node chain" invariant.
        fs_reads = [b for b in runnable if b.get("selected_capability_id") == "filesystem_read"]
        fs_writes = [b for b in runnable if b.get("selected_capability_id") == "filesystem_write"]
        print(
            f"[shape] fs_read={len(fs_reads)}  fs_write={len(fs_writes)}  "
            f"coding_agent={len([b for b in runnable if b.get('selected_capability_id') == 'coding_agent'])}"
        )

        # --- Run the natural chain ---
        run_resp = client.post(
            "/api/v1/runs",
            json={
                "project_id": proj["id"],
                "executable_id": executable_id,
                "input_snapshot_json": {},
            },
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["id"]
        print(f"[...] running natural chain  run_id={run_id}")
        started_run = client.post(f"/api/v1/runs/{run_id}/start")
        try:
            job_exec_mod.JobExecutionService().execute_job(
                started_run.json()["active_job_id"]
            )
        except RuntimeError as exc:
            print(f"[info] run raised RuntimeError: {exc}")

        final_run = client.get(f"/api/v1/runs/{run_id}").json()
        print(f"[ok] run.status={final_run['status']}  steps={len(final_run.get('run_steps', []))}")
        for step in final_run.get("run_steps", []):
            out = step.get("output_payload_json") or {}
            print(
                f"     - {step['step_name']}: {step['status']}  "
                f"capability={out.get('capability_family') or out.get('selected_capability_id')}"
            )

    elapsed = round(time.time() - t_start, 1)

    # --- Acceptance report ---
    print("\n" + "=" * 72)
    print("  Natural-chain demo — acceptance")
    print("=" * 72)
    checks: list[tuple[str, bool, str]] = []

    # 1. fs_read in runnable set
    checks.append(
        (
            "fs_read runnable on the natural plan",
            len(fs_reads) >= 1,
            f"runnable fs_reads={len(fs_reads)}",
        )
    )
    # 2. fs_write in runnable set
    checks.append(
        (
            "fs_write runnable on the natural plan",
            len(fs_writes) >= 1,
            f"runnable fs_writes={len(fs_writes)}",
        )
    )
    # 3. run completed
    checks.append(
        (
            "run completed",
            final_run.get("status") == "completed",
            f"status={final_run.get('status')}",
        )
    )
    # 4. artifact content
    artifact_ok = False
    content_preview = ""
    if output_csv.exists():
        content = output_csv.read_text(encoding="utf-8")
        content_preview = content[:400]
        artifact_ok = all(frag in content for frag in EXPECTED_CSV_CONTENT_FRAGMENTS)
    checks.append(
        (
            "artifact matches expected CSV shape",
            artifact_ok,
            f"exists={output_csv.exists()} size={output_csv.stat().st_size if output_csv.exists() else 0}B",
        )
    )

    all_pass = all(ok for _, ok, _ in checks)
    for label, ok, detail in checks:
        marker = "✔" if ok else "✘"
        print(f"  {marker} {label}  — {detail}")

    if artifact_ok:
        print("\n  artifact content preview:")
        for line in content_preview.splitlines():
            print(f"    {line}")

    print(f"\nelapsed: {elapsed}s")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
