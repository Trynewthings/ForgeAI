"""Real-LLM demo: session → planner_output(coding_agent) → build → run → artifact.

This script exercises the session-anchored C-slice chain end to end
against the real LLM provider (DeepSeek via the OpenAI-compatible
adapter). It is a hand-run demo, not a pytest test — pytest would
make the real-LLM budget opaque and the output table harder to read.

Run:

    cd apps/api && source .venv/bin/activate && \\
        PYTHONPATH=. python tests/demo_session_coding_agent_real_llm.py

What it does
------------
1. Seed a small ``orders.csv`` on disk.
2. Create project + front-chain session.
3. Submit a ``raw_intent`` via the real-LLM interpret→evaluate→compile
   pipeline and run ``/plan``.
4. Observe what the deterministic planner bound to the node. If
   ``coding_agent`` isn't selected naturally (typical — it's a
   fallback, not a preferred capability), overwrite the planner_output
   with a synthetic single-node ``coding_agent`` graph so we can
   still validate the downstream leg. This substitution is made
   explicit in the report so nothing is misread as "planner selected
   coding_agent naturally".
5. Create + execute a session-anchored build.
6. Create + execute a run. The coding_agent node authors a real
   python script with DeepSeek, runs it under the bounded sandbox,
   and writes the declared output.
7. Emit the six-category result table the user asked for.

Nothing in here touches the legacy blueprint build path.
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
os.environ.setdefault("CORS_ORIGINS", '["http://localhost:1420","http://127.0.0.1:1420"]')
# On macOS prefer sandbox-exec; on Linux CI use subprocess fallback.
os.environ.setdefault(
    "CODING_AGENT_SANDBOX",
    "sandbox-exec" if sys.platform == "darwin" else "subprocess",
)


def _mk_orders_csv(workspace: Path) -> Path:
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
    return orders


def _synthetic_coding_agent_planner_output(
    *,
    workspace: Path,
    orders_csv: Path,
    output_csv: Path,
) -> dict:
    from app.schemas.workflow_planner import (
        WorkflowPlannerNodeDraft,
        WorkflowPlannerOutput,
    )
    from app.services.workflow_planner.runtime_field_enricher import (
        META_ARGS,
        META_BOUND_CAPABILITY,
        META_STEP_NAME,
        META_SYSTEM_PROMPT,
        META_TASK_PROMPT,
    )

    task = (
        f"Read the CSV at {orders_csv} and aggregate by the "
        "product_line column. For each product_line output a row with "
        "columns [product_line, order_count, total_revenue] where "
        "total_revenue = sum(qty * unit_price). Sort rows by "
        "total_revenue descending. Write the result as a CSV with a "
        f"header row to {output_csv}. Use python 3 standard library only."
    )
    node = WorkflowPlannerNodeDraft(
        draft_node_id="node_1",
        step_name="aggregate_orders",
        node_name="aggregate_orders",
        node_semantic_type="transform",
        semantic_goal=task,
        preferred_capability_ids=["coding_agent"],
        candidate_capability_ids=["coding_agent"],
        planner_metadata={
            META_STEP_NAME: "aggregate_orders",
            META_BOUND_CAPABILITY: {
                "selected_capability_id": "coding_agent",
                "candidate_ids": ["coding_agent"],
                "coverage": "full",
                "side_effect_class": "evidence_transform",
                "selection_source": "demo_override",
                "uncovered_step_kinds": [],
            },
            META_ARGS: {
                "available_tools": [],
                "loop_config": {"max_iterations": 3},
                "agentic": False,
                # Pin the sandbox workspace to the demo workspace so
                # the sandboxed script can read the input CSV and write
                # the declared output without hitting an absolute-path
                # policy violation.
                "workspace_dir": str(workspace),
            },
            META_SYSTEM_PROMPT: (
                "You are a coding agent solving a bounded CSV aggregation. "
                "Write one python script that completes the task, then "
                "call the write_and_run tool exactly once. Do not use "
                "third-party libraries."
            ),
            META_TASK_PROMPT: task,
            "expected_outputs": [
                {"locator": str(output_csv), "kind": "data.table.derived"},
            ],
        },
    )
    return WorkflowPlannerOutput(
        node_drafts=[node],
        entry_node_ids=["node_1"],
        exit_node_ids=["node_1"],
    ).model_dump(mode="json")


def _seed_state_with_planner_output(
    client, *, project_id: str, session_id: str, planner_output: dict
) -> str:
    """Overwrite the latest state row's planner_output_json."""
    from app.core.database import get_db
    from app.db.models import FrontChainState
    from app.main import app

    db_gen = app.dependency_overrides[get_db]()
    db = next(db_gen)
    row = (
        db.query(FrontChainState)
        .filter(FrontChainState.session_id == session_id)
        .order_by(FrontChainState.created_at.desc())
        .first()
    )
    if row is None:
        # No state yet (compile failed) — create a seed row that meets
        # the NOT NULL + CHECK constraints.
        row = FrontChainState(
            project_id=project_id,
            session_id=session_id,
            raw_intent="demo seed",
            problem_formulation_json={"target_object": {"value": "demo"}},
            input_closure_json={"closure_status": "input_closed"},
            output_closure_json={"closure_status": "output_closed"},
            merged_gaps_json={},
            top_questions_json=[],
            llm_trace_json={"call_count": 0, "llm_mode": "seed"},
            interpreter_version="demo",
            validator_version="demo",
            compile_context_json={"compile_trace": {"authority_source": "demo_seed"}},
            compile_version="demo",
        )
        db.add(row)
    row.planner_output_json = planner_output
    row.planner_version = "workflow_planner_v1"
    db.commit()
    row_id = row.id  # capture before the generator closes the session
    try:
        next(db_gen)
    except StopIteration:
        pass
    return row_id


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
        print(f"[abort] settings.llm_mode={settings.llm_mode}, expected 'real'")
        return 2
    print(
        f"\n[env] llm_mode={settings.llm_mode} "
        f"model={settings.default_llm_model_name} "
        f"sandbox={os.environ['CODING_AGENT_SANDBOX']}"
    )

    # Dedicated SQLite DB so the demo can't stomp production data.
    db_path = Path("/tmp/demo_coding_agent_real_llm.sqlite")
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
    # Keep run_mod.settings.llm_mode = "real" so the coding_agent uses
    # a real LLM inside the workload.
    job_dispatch_mod.JobDispatchService.dispatch = (
        lambda self, job_id: ("test", f"task-{job_id}")
    )

    # Workspace for the CSV demo.
    workspace = Path("/tmp/demo_coding_agent_ws")
    if workspace.exists():
        shutil.rmtree(workspace)
    orders_csv = _mk_orders_csv(workspace)
    output_csv = workspace / "daily_totals.csv"

    report: dict[str, Any] = {
        "env": {
            "llm_mode": settings.llm_mode,
            "model": settings.default_llm_model_name,
            "sandbox": os.environ["CODING_AGENT_SANDBOX"],
        }
    }
    t_start = time.time()

    with TestClient(app) as client:
        # --- Project + session ---
        proj = client.post(
            "/api/v1/projects",
            json={"name": "CodingAgent RealLLM Demo", "description": ""},
        )
        assert proj.status_code == 201, proj.text
        project_id = proj.json()["id"]
        sess = client.post(
            "/api/v1/front-chain/sessions", json={"project_id": project_id}
        )
        assert sess.status_code == 201, sess.text
        session_id = sess.json()["id"]
        print(f"[ok] project={project_id}  session={session_id}")

        # --- Turn: real LLM interpret → evaluate → compile ---
        intent_text = (
            f"I have a local CSV at {orders_csv}. Aggregate rows by "
            "product_line and write a CSV file with columns "
            "[product_line, order_count, total_revenue] sorted by "
            f"total_revenue descending. Write the result to {output_csv}. "
            "Use python."
        )
        turn_resp = client.post(
            f"/api/v1/front-chain/sessions/{session_id}/turn",
            json={"kind": "raw_intent", "raw_intent": intent_text},
        )
        if turn_resp.status_code != 201:
            print(f"[warn] turn returned {turn_resp.status_code}: {turn_resp.text[:200]}")
        turn_body = turn_resp.json() if turn_resp.status_code == 201 else {}
        report["turn"] = {
            "status": turn_body.get("status"),
            "stages_run": turn_body.get("stages_run", []),
        }
        print(f"[ok] turn status={turn_body.get('status')}")

        # --- Plan: deterministic planner (mock would be deterministic,
        #     real planner still deterministic; we want to observe what
        #     it bound to). If the turn didn't reach compile, skip plan.
        natural_planner_output: dict | None = None
        if turn_body.get("status") == "compilable":
            plan_resp = client.post(
                f"/api/v1/front-chain/sessions/{session_id}/plan"
            )
            if plan_resp.status_code == 201:
                natural_planner_output = plan_resp.json().get("planner_output")
                print(
                    f"[ok] natural plan: "
                    f"nodes={len(natural_planner_output.get('node_drafts', []))}"
                )
        # Record what the planner naturally bound to.
        if natural_planner_output:
            natural_bindings = []
            for draft in natural_planner_output.get("node_drafts", []):
                md = draft.get("planner_metadata", {}) or {}
                bound = md.get("bound_capability", {}) or {}
                natural_bindings.append(
                    {
                        "step_name": md.get("step_name"),
                        "selected_capability_id": bound.get("selected_capability_id"),
                        "selection_source": bound.get("selection_source"),
                        "coverage": bound.get("coverage"),
                    }
                )
            report["natural_planner_output"] = {
                "node_count": len(natural_planner_output.get("node_drafts", [])),
                "bindings": natural_bindings,
            }
        else:
            report["natural_planner_output"] = "unavailable (compile did not reach)"

        natural_picked_coding_agent = (
            natural_planner_output is not None
            and any(
                (d.get("planner_metadata", {}) or {})
                .get("bound_capability", {})
                .get("selected_capability_id") == "coding_agent"
                for d in natural_planner_output.get("node_drafts", [])
            )
        )

        # --- Override: ensure the chain exercises coding_agent ---
        if not natural_picked_coding_agent:
            print(
                "[info] natural planner did not pick coding_agent; "
                "overriding planner_output with a synthetic single-node "
                "coding_agent graph so the downstream demo is meaningful."
            )
        synth = _synthetic_coding_agent_planner_output(
            workspace=workspace, orders_csv=orders_csv, output_csv=output_csv
        )
        _seed_state_with_planner_output(
            client,
            project_id=project_id,
            session_id=session_id,
            planner_output=synth,
        )
        report["planner_output_used"] = {
            "source": "natural" if natural_picked_coding_agent else "synthetic_override",
            "reason": (
                "deterministic planner kept coding_agent as fallback (not preferred)"
                if not natural_picked_coding_agent
                else "n/a"
            ),
        }

        # --- Build ---
        build_resp = client.post(
            "/api/v1/builds",
            json={"project_id": project_id, "session_id": session_id},
        )
        assert build_resp.status_code == 201, build_resp.text
        build_id = build_resp.json()["id"]
        started = client.post(f"/api/v1/builds/{build_id}/start").json()
        job_exec_mod.JobExecutionService().execute_job(started["active_job_id"])
        final_build = client.get(f"/api/v1/builds/{build_id}").json()
        assert final_build["status"] == "completed", final_build.get("error_message")
        executable_id = final_build["executable_id"]
        exe = client.get(f"/api/v1/executables/{executable_id}").json()
        manifest = exe["manifest_json"]
        binding_summary = manifest["metadata"]["session_anchored_binding_summary"]

        # Load bindings.json to read the concrete binding row.
        source_path = Path(exe["source_path"])
        bindings_doc = json.loads((source_path / "bindings.json").read_text())
        first_binding = bindings_doc["bindings"][0]
        report["build"] = {
            "status": final_build["status"],
            "executable_id": executable_id,
            "manifest_compatibility_mode": manifest["compatibility"]["mode"],
            "binding_summary": binding_summary,
            "first_binding": {
                "step_name": first_binding.get("step_name"),
                "selected_capability_id": first_binding.get("selected_capability_id"),
                "execution_kind": first_binding.get("execution_kind"),
                "runtime_adapter": first_binding.get("runtime_adapter"),
                "handler_ref": first_binding.get("handler_ref"),
                "binding_status": first_binding.get("binding_status"),
            },
        }
        print(
            f"[ok] build={final_build['status']} "
            f"binding={first_binding.get('execution_kind')} "
            f"capability={first_binding.get('selected_capability_id')}"
        )

        # --- Run (real LLM inside coding_agent workload) ---
        run_resp = client.post(
            "/api/v1/runs",
            json={
                "project_id": project_id,
                "executable_id": executable_id,
                "input_snapshot_json": {},
            },
        )
        assert run_resp.status_code == 201, run_resp.text
        run_id = run_resp.json()["id"]
        print(f"[...] starting run={run_id} with real LLM (this calls DeepSeek)")
        started_run = client.post(f"/api/v1/runs/{run_id}/start")
        # Run failure surfaces as a RuntimeError from execute_job; we
        # want the fact table regardless, so swallow it here and let the
        # run record itself carry the signal.
        try:
            job_exec_mod.JobExecutionService().execute_job(
                started_run.json()["active_job_id"]
            )
        except RuntimeError as exc:
            print(f"[info] run raised RuntimeError (expected for failures): {exc}")
        final_run = client.get(f"/api/v1/runs/{run_id}").json()
        report["run"] = {
            "status": final_run["status"],
            "error_message": final_run.get("error_message"),
            "step_count": len(final_run.get("run_steps", [])),
        }
        print(f"[ok] run={final_run['status']}")

        # --- Collect step-level detail + coding_agent diagnostics.
        # On failure the scheduler's outer except overwrites the
        # step's output_payload with a generic failure shell, so we
        # reach into the sidecar JSON the coding_agent branch writes
        # right before raising to recover the structured diagnostics.
        from app.core.config import ROOT_DIR as _ROOT_DIR

        sidecar_dir = _ROOT_DIR / ".generated" / "coding_agent_failures"
        step_details = []
        for step in final_run.get("run_steps", []):
            out = step.get("output_payload_json") or {}
            sidecar_path = sidecar_dir / f"run-{run_id}-step-{step['id']}.json"
            if sidecar_path.exists():
                try:
                    out = json.loads(sidecar_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            struct = out.get("structured_output") or {}
            per_iter = struct.get("per_iteration") or []
            last_iter = per_iter[-1] if per_iter else {}
            step_details.append(
                {
                    "step_name": step["step_name"],
                    "status": step["status"],
                    "capability_family": out.get("capability_family"),
                    "adapter": out.get("adapter"),
                    "structured": {
                        "ok": struct.get("ok"),
                        "failure_kind": struct.get("failure_kind"),
                        "iterations_used": struct.get("iterations_used"),
                        "final_language": struct.get("final_language"),
                        "produced_files": [
                            {
                                "path": pf.get("path"),
                                "size_bytes": pf.get("size_bytes"),
                                "change": pf.get("change"),
                            }
                            for pf in (struct.get("produced_files") or [])
                        ],
                    },
                    "last_iteration_trace": {
                        "exit_code": last_iter.get("exit_code"),
                        "timed_out": last_iter.get("timed_out"),
                        "stderr_tail": (last_iter.get("stderr") or "")[-400:]
                        if last_iter
                        else None,
                        "stdout_tail": (last_iter.get("stdout") or "")[-400:]
                        if last_iter
                        else None,
                    },
                    "script_summary": {
                        "language": struct.get("final_language"),
                        "source_length": len(struct.get("final_script_source") or ""),
                        "source_head": (struct.get("final_script_source") or "")[:600],
                    },
                    "reasoning_summary": struct.get("reasoning_summary"),
                }
            )
        report["steps"] = step_details

        # --- Artifact: declared output file ---
        artifact_exists = output_csv.exists()
        artifact_preview = None
        if artifact_exists:
            artifact_preview = output_csv.read_text(encoding="utf-8")[:1500]
        report["artifact"] = {
            "path": str(output_csv),
            "exists": artifact_exists,
            "size_bytes": output_csv.stat().st_size if artifact_exists else 0,
            "content_preview": artifact_preview,
        }

    report["elapsed_seconds"] = round(time.time() - t_start, 1)

    # --- Pretty print the 6-category table ---
    print("\n" + "=" * 72)
    print("  Real-LLM coding_agent demo — result table")
    print("=" * 72)
    print(f"\nelapsed: {report['elapsed_seconds']}s  env: {report['env']}")

    print("\n(1) planner bound_capability — what the planner produced")
    if isinstance(report["natural_planner_output"], dict):
        for b in report["natural_planner_output"]["bindings"]:
            print(f"    - {b['step_name']}: {b['selected_capability_id']} "
                  f"(source={b['selection_source']}, coverage={b['coverage']})")
    else:
        print(f"    {report['natural_planner_output']}")
    print(f"    planner_output_used: {report['planner_output_used']}")

    print("\n(2) build manifest + binding")
    b = report["build"]
    print(f"    status={b['status']}  manifest.compatibility.mode={b['manifest_compatibility_mode']}")
    print(f"    binding_summary={b['binding_summary']}")
    print(f"    first_binding={b['first_binding']}")

    print("\n(3) run status")
    print(f"    status={report['run']['status']}  steps={report['run']['step_count']}")
    if report["run"].get("error_message"):
        print(f"    error: {report['run']['error_message']}")

    print("\n(4) real-LLM script — summary")
    for s in report["steps"]:
        print(
            f"    step={s['step_name']}  status={s['status']}  "
            f"capability={s['capability_family']}  adapter={s['adapter']}"
        )
        sc = s["structured"]
        print(
            f"      ok={sc['ok']}  failure_kind={sc['failure_kind']}  "
            f"iterations={sc['iterations_used']}  language={sc['final_language']}"
        )
        print(f"      script: {s['script_summary']['source_length']} chars, head:")
        for line in s["script_summary"]["source_head"].splitlines()[:12]:
            print(f"          {line}")

    print("\n(5) produced_files (as reported by the workload)")
    for s in report["steps"]:
        for pf in s["structured"]["produced_files"]:
            print(f"    - {pf['path']}  {pf['size_bytes']}B  change={pf['change']}")

    print("\n(6) artifact on disk")
    a = report["artifact"]
    print(f"    path={a['path']}  exists={a['exists']}  size={a['size_bytes']}B")
    if a["exists"]:
        print("    content:")
        for line in (a["content_preview"] or "").splitlines():
            print(f"        {line}")

    print("\n(7) last-iteration diagnostics (if any failed or retried)")
    for s in report["steps"]:
        li = s["last_iteration_trace"]
        if li.get("exit_code") not in (0, None) or s["structured"]["failure_kind"] != "ok":
            print(
                f"    step={s['step_name']} exit={li['exit_code']} "
                f"timed_out={li['timed_out']}  failure_kind={s['structured']['failure_kind']}"
            )
            if li.get("stderr_tail"):
                print("      stderr_tail:")
                for line in li["stderr_tail"].splitlines()[-10:]:
                    print(f"        {line}")

    # Save full JSON too.
    report_path = Path("/tmp/demo_coding_agent_real_llm_report.json")
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nfull json report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
