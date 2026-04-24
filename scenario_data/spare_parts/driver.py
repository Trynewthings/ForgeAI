"""First-run driver for the spare-parts scenario.

Walks project -> session -> turn -> plan -> build -> run against the live
API on localhost:8000 and dumps each stage's payload to run_trace/ so we
can inspect exactly what the front-chain + planner + build produced.

No prompt engineering. Task prompt is the user's verbatim one-liner plus
the input/output file paths (minimum-necessary logistics).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://localhost:8000/api/v1"
ROOT = Path("/Users/llk/agentsFactory/scenario_data/spare_parts")
TRACE = ROOT / "run_trace"
TRACE.mkdir(exist_ok=True)

RAW_INTENT = (
    "基于配件主数据、实时库存、历史消耗、市场需求信号和供应商交期数据，"
    "对每个配件 SKU 进行库存风险判断、未来需求预测和补货决策，"
    "并生成可执行的补货队列，降低缺货与积压风险。\n\n"
    f"输入数据目录：{ROOT}/input/\n"
    f"输出文件：{ROOT}/output/replenishment_queue.csv"
)


def dump(stage: str, payload) -> Path:
    path = TRACE / f"{stage}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    print(f"  -> {path.relative_to(ROOT)}")
    return path


def post(client, path, body=None, *, ok=(200, 201)) -> dict:
    r = client.post(f"{BASE}{path}", json=body or {})
    if r.status_code not in ok:
        raise SystemExit(f"POST {path} -> HTTP {r.status_code}\n{r.text[:2000]}")
    return r.json()


def get(client, path, *, ok=(200,)) -> dict:
    r = client.get(f"{BASE}{path}")
    if r.status_code not in ok:
        raise SystemExit(f"GET {path} -> HTTP {r.status_code}\n{r.text[:2000]}")
    return r.json()


def poll(client, path, *, target_statuses, timeout_s=600, interval_s=3):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout_s:
        data = get(client, path)
        last = data
        status = data.get("status")
        if status in target_statuses:
            return data
        time.sleep(interval_s)
    raise SystemExit(
        f"poll {path} timed out after {timeout_s}s; "
        f"last_status={last.get('status') if last else None}"
    )


def main():
    with httpx.Client(timeout=60.0) as client:
        # ---------- Stage 0: project + session ----------
        print("\n[stage 0] create project + session")
        proj = post(client, "/projects", {"name": "spare-parts-scenario-1", "description": "first bare run"})
        project_id = proj["id"]
        print(f"  project_id={project_id}")

        sess = post(client, "/front-chain/sessions", {"project_id": project_id})
        session_id = sess["id"]
        print(f"  session_id={session_id}")

        # ---------- Stage 1: turn with raw_intent ----------
        print("\n[stage 1] front-chain turn (interpret + evaluate + compile)")
        turn = post(
            client,
            f"/front-chain/sessions/{session_id}/turn",
            {"kind": "raw_intent", "raw_intent": RAW_INTENT},
        )
        dump("01_turn_response", turn)
        print(f"  stages_run={turn.get('stages_run')}  session_status={turn.get('session_status')}")
        state_id = turn.get("state_id")
        if state_id:
            state = get(client, f"/front-chain/states/{state_id}")
            dump("01b_state_full", state)

        cc = turn.get("compile_context")
        if not cc:
            print("  ⚠ no compile_context — front-chain stopped before compile")
            summarize_blockers(turn)
            return
        dump("01c_compile_context", cc)
        solv = (cc.get("solvability") or {})
        print(f"  solvability.final_status={solv.get('final_status')}  confidence={solv.get('confidence')}")

        # ---------- Stage 2: plan ----------
        print("\n[stage 2] planner")
        plan = post(client, f"/front-chain/sessions/{session_id}/plan")
        dump("02_plan_response", plan)
        po = plan.get("planner_output") or {}
        drafts = po.get("node_drafts") or []
        edges = po.get("edge_drafts") or []
        print(f"  nodes={len(drafts)} edges={len(edges)} entries={po.get('entry_node_ids')} exits={po.get('exit_node_ids')}")
        for nd in drafts:
            cap = (nd.get("planner_metadata") or {}).get("bound_capability") or {}
            print(f"    - {nd.get('node_name'):30s} step={nd.get('step_name'):20s} cap={cap.get('selected_capability_id')}")

        # ---------- Stage 3: build ----------
        print("\n[stage 3] build (create + start + wait)")
        build = post(
            client,
            "/builds",
            {
                "project_id": project_id,
                "session_id": session_id,
                "build_options_json": {"target": "spare_parts_scenario"},
            },
        )
        build_id = build["id"]
        print(f"  build_id={build_id}")
        started = post(client, f"/builds/{build_id}/start")
        print(f"  job_id={started.get('active_job_id')}")
        build_final = poll(
            client,
            f"/builds/{build_id}",
            target_statuses={"completed", "failed"},
            timeout_s=300,
        )
        dump("03_build_final", build_final)
        print(f"  build.status={build_final['status']}")
        if build_final["status"] != "completed":
            print(f"  ✗ build failed: {build_final.get('error_message')}")
            return
        executable_id = build_final["executable_id"]
        exe = get(client, f"/executables/{executable_id}")
        dump("03b_executable", exe)
        manifest = exe.get("manifest_json") or {}
        binding = (manifest.get("metadata") or {}).get("session_anchored_binding_summary") or {}
        print(f"  runnable_steps={binding.get('runnable_steps')}  blocked={binding.get('blocked_steps')}")

        # ---------- Stage 4: run ----------
        print("\n[stage 4] run (create + start + wait)")
        run = post(
            client,
            "/runs",
            {
                "project_id": project_id,
                "executable_id": executable_id,
                "input_snapshot_json": {},
            },
        )
        run_id = run["id"]
        print(f"  run_id={run_id}  steps={[s['step_name'] for s in run.get('run_steps') or []]}")
        started_run = post(client, f"/runs/{run_id}/start")
        print(f"  run_job_id={started_run.get('active_job_id')}")
        run_final = poll(
            client,
            f"/runs/{run_id}",
            target_statuses={"completed", "failed", "waiting_for_intervention"},
            timeout_s=900,
        )
        dump("04_run_final", run_final)
        print(f"  run.status={run_final['status']}")

        # ---------- Stage 5: check output artifact ----------
        expected_out = ROOT / "output" / "replenishment_queue.csv"
        if expected_out.exists():
            print(f"\n[stage 5] ✓ output file exists: {expected_out}  size={expected_out.stat().st_size}B")
            head = expected_out.read_text(errors="replace").splitlines()[:6]
            for line in head:
                print(f"    {line}")
        else:
            print(f"\n[stage 5] ✗ output file missing at {expected_out}")


def summarize_blockers(turn):
    cc = turn.get("compile_context") or {}
    closure = cc.get("closure_report") or {}
    for k in ("problem_formulation_status", "input_closure_status", "output_closure_status"):
        print(f"  {k}={closure.get(k)}")
    for q in (turn.get("open_questions") or [])[:5]:
        print(f"  open_q: {q}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[interrupted]")
        sys.exit(130)
