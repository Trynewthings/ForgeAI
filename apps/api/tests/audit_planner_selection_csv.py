"""P2.1 selection-trace audit — CSV aggregation task.

Replays the session→compile→plan pipeline under real DeepSeek and
dumps, per subgoal:

* subgoal text, subgoal_kind, required_capability_classes, input/output
  evidence_kinds, graph_role, human_review_required
* full narrower score trace: every candidate that entered the
  step_kind index, its score, and whether it survived top-k + the
  forbidden-side-effect filter
* the final CandidatePoolForPlanner.candidate_pool (post
  source-aware rescoring) with per-candidate metadata
* shape-LLM's capability_choice for the subgoal (+ the index it
  pointed at and the resulting selected_capability_id)
* whether coding_agent entered the pool; if so, its rank + why it
  wasn't selected; if not, why it was cut

Result: one JSON document + a human-readable console summary.
Sufficient to answer "Bind 错 / Shape 错 / 前链语义错".

Run:

    cd apps/api && source .venv/bin/activate && \\
        PYTHONPATH=. python tests/audit_planner_selection_csv.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["LLM_MODE"] = "real"
os.environ.setdefault("CORS_ORIGINS", '["http://localhost:1420","http://127.0.0.1:1420"]')


def _prepare_app_for_sqlite():
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy.orm import sessionmaker

    import app.services.build.build_execution_service as build_mod
    import app.services.job.job_dispatch_service as job_dispatch_mod
    import app.services.job.job_execution_service as job_exec_mod
    import app.services.run.run_execution_service as run_mod
    from app.core.database import get_db
    from app.db.base import Base
    from app.main import app

    db_path = Path("/tmp/audit_planner_csv.sqlite")
    try:
        db_path.unlink()
    except FileNotFoundError:
        pass
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_connection, _rec) -> None:
        c = dbapi_connection.cursor()
        c.execute("PRAGMA foreign_keys=ON")
        c.close()

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
    job_dispatch_mod.JobDispatchService.dispatch = (
        lambda self, job_id: ("test", f"task-{job_id}")
    )
    return app, TestingSessionLocal


def _manual_narrow_trace(
    *, subgoal, ctx
) -> dict[str, Any]:
    """Re-run the narrower's scoring loop so we can observe individual
    candidate scores per step_kind — the public CandidatePool only
    returns top-k ids, not the raw scores.
    """
    from app.services.front_chain.solvability_tool_binder_v2 import (
        _build_step_kind_index,
        _score_candidates,
    )
    from app.services.front_chain.solvability_step_kind_canonicalizer import (
        canonicalize_step_kinds,
    )
    from app.services.workflow_planner.capability_binder import _subgoal_to_draft

    draft = _subgoal_to_draft(subgoal)
    canonical_result = canonicalize_step_kinds(draft.required_step_kinds)
    canonical_kinds = canonical_result.canonical_step_kinds

    envelope = ctx.capability_envelope_raw
    index = _build_step_kind_index(envelope)

    per_step_kind: list[dict[str, Any]] = []
    for sk in canonical_kinds:
        ids_for_kind = list(index.get(sk, []))
        if not ids_for_kind:
            per_step_kind.append({"step_kind": sk, "indexed": [], "scored": []})
            continue
        scored = _score_candidates(
            candidate_ids=ids_for_kind,
            envelope=envelope,
            subgoal=draft,
            step_kind=sk,
        )
        per_step_kind.append(
            {
                "step_kind": sk,
                "indexed_count": len(ids_for_kind),
                "scored": [
                    {
                        "capability_id": cid,
                        "score": round(score, 3),
                    }
                    for cid, score in scored
                ],
            }
        )
    return {
        "canonical_step_kinds": list(canonical_kinds),
        "unresolved_oov": list(canonical_result.unresolved_oov),
        "per_step_kind": per_step_kind,
    }


def _locate_capability_in_narrow_trace(
    trace: dict[str, Any], capability_id: str
) -> dict[str, Any] | None:
    for block in trace["per_step_kind"]:
        for idx, item in enumerate(block["scored"]):
            if item["capability_id"] == capability_id:
                return {
                    "found_in_step_kind": block["step_kind"],
                    "raw_score": item["score"],
                    "raw_rank_among_step_kind": idx + 1,
                    "raw_rank_total": len(block["scored"]),
                }
    return None


def main() -> int:
    app, TestingSessionLocal = _prepare_app_for_sqlite()

    from fastapi.testclient import TestClient
    from app.core.config import settings
    from app.db.models import FrontChainSession, FrontChainState
    from app.schemas.requirement_compiler import CompileContextResponse
    from app.services.workflow_planner.capability_binder import (
        PlannerCapabilityBinder,
    )
    from app.services.workflow_planner.planner_context_builder import (
        build_planner_context,
    )
    from app.services.workflow_planner.shape_planner_llm import ShapePlannerLLM

    if settings.llm_mode != "real":
        print(f"[abort] llm_mode={settings.llm_mode}")
        return 2
    print(
        f"[env] llm_mode={settings.llm_mode}  model={settings.default_llm_model_name}"
    )
    t0 = time.time()

    # Prepare CSV
    workspace = Path("/tmp/audit_planner_ws")
    workspace.mkdir(parents=True, exist_ok=True)
    orders_csv = workspace / "orders.csv"
    orders_csv.write_text(
        "order_id,product_line,qty,unit_price\n1,widgets,3,12.5\n",
        encoding="utf-8",
    )
    output_csv = workspace / "daily.csv"
    intent = (
        f"I have a local CSV at {orders_csv}. Aggregate by product_line "
        "and write a CSV with [product_line, order_count, total_revenue] "
        f"sorted by total_revenue descending to {output_csv}. Use python."
    )

    with TestClient(app) as client:
        proj = client.post(
            "/api/v1/projects", json={"name": "audit", "description": ""}
        ).json()
        sess = client.post(
            "/api/v1/front-chain/sessions", json={"project_id": proj["id"]}
        ).json()
        turn = client.post(
            f"/api/v1/front-chain/sessions/{sess['id']}/turn",
            json={"kind": "raw_intent", "raw_intent": intent},
        )
        assert turn.status_code == 201, turn.text
        body = turn.json()
        if body["status"] != "compilable":
            print(f"[abort] turn did not reach compile: {body['status']}")
            return 3
        compile_context_json = body["compile_context"]
        print(
            f"[ok] compile: subgoals="
            f"{len(compile_context_json['solvability']['required_subgoals'])}"
        )

    compile_context = CompileContextResponse.model_validate(compile_context_json)
    subgoals = list(compile_context.solvability.required_subgoals)
    ctx = build_planner_context(compile_context)

    # Run the binder (deterministic)
    binder = PlannerCapabilityBinder()
    pools = binder.narrow_for_subgoals(subgoals, ctx)

    # Run the shape-LLM (real call)
    shape = ShapePlannerLLM()
    shape_proposal = shape.plan_shape(
        subgoals=subgoals,
        bindings=pools,
        allowed_node_semantic_types=list(ctx.allowed_node_semantic_types),
        forbidden_side_effect_classes=list(ctx.forbidden_side_effect_classes),
        require_human_gate_before=list(ctx.require_human_gate_before),
        require_verification_after=list(ctx.require_verification_after),
        candidate_shape=compile_context.solvability.graph_feasibility.candidate_shape,
    )
    print(
        f"[ok] shape-LLM: failed={shape_proposal.failed}  "
        f"capability_choice={shape_proposal.capability_choice}"
    )

    # Per-subgoal trace
    report_rows: list[dict[str, Any]] = []
    for sg in subgoals:
        pool = pools[sg.subgoal_id]
        narrow_trace = _manual_narrow_trace(subgoal=sg, ctx=ctx)
        coding_agent_in_trace = _locate_capability_in_narrow_trace(
            narrow_trace, "coding_agent"
        )
        coding_agent_in_pool = "coding_agent" in pool.candidate_pool
        coding_agent_rank = (
            pool.candidate_pool.index("coding_agent") + 1
            if coding_agent_in_pool
            else None
        )

        shape_pick_idx = shape_proposal.capability_choice.get(sg.subgoal_id)
        if shape_pick_idx is not None and 0 <= shape_pick_idx < len(pool.candidate_pool):
            selected_id = pool.candidate_pool[shape_pick_idx]
            selection_source = "shape_llm"
        else:
            selected_id = (
                pool.candidate_pool[0] if pool.candidate_pool else None
            )
            selection_source = "bind_fallback_pool0" if selected_id else "none"

        # --- right_pick / wrong_pick classification ---
        # Rules (in order):
        #   1. wrong_pick — the selected capability's scope tags don't
        #      overlap the detected task domain(s). This is the only
        #      case that's unambiguously bad: we picked a capability
        #      that the registry itself says is for a different domain.
        #   2. right_pick — everything else. Either the selected cap
        #      has no scope declared (neutral, generally safe), its
        #      scope matches the task domain, or it's the generalist
        #      fallback (always a legitimate option).
        #   3. uncertain — we couldn't detect ANY task domain AND the
        #      selected cap has scope. In that case we don't want to
        #      penalise a pick on pure uncertainty.
        #
        # Regressions on scoped-mismatch picks are the actionable
        # signal. Existing fault_class is kept in the row as a
        # diagnostic breadcrumb but the classifier is what we sum on.
        from app.services.workflow_planner.capability_binder import (
            _capability_scope_domains,
            _detect_task_domains,
        )

        selected_entry = ctx.capability_envelope_by_id.get(selected_id or "", {}) or {}
        selected_scope = _capability_scope_domains(selected_entry)
        task_domains = _detect_task_domains(subgoal=sg, ctx=ctx)
        selected_is_generalist = (
            "generalist_fallback" in (selected_entry.get("tags") or [])
        )

        if selected_id is None:
            pick_classification = "no_selection"
            pick_reason = "pool empty or no shape pick available"
        elif selected_is_generalist:
            pick_classification = "right_pick"
            pick_reason = "generalist fallback is always a legitimate option"
        elif not selected_scope:
            pick_classification = "right_pick"
            pick_reason = "selected capability has no domain scope — neutral fit"
        elif not task_domains:
            pick_classification = "uncertain"
            pick_reason = (
                "task domain could not be detected from subgoal text; "
                "cannot judge scoped pick"
            )
        elif selected_scope & task_domains:
            pick_classification = "right_pick"
            pick_reason = (
                f"selected scope {sorted(selected_scope)} overlaps task domain "
                f"{sorted(task_domains)}"
            )
        else:
            pick_classification = "wrong_pick"
            pick_reason = (
                f"selected capability is scoped to {sorted(selected_scope)} but task "
                f"domain is {sorted(task_domains)} — scope mismatch"
            )

        # Keep the old fault_class as a diagnostic breadcrumb — it
        # explains which subsystem (bind / shape / frontchain) would
        # need to move to change the outcome. Classification above is
        # what gets summed for regression monitoring.
        fault_class: str
        if selected_id == "coding_agent":
            fault_class = "selected_coding_agent"
        elif coding_agent_in_pool and shape_pick_idx is not None and shape_pick_idx != coding_agent_rank - 1:
            fault_class = "shape_chose_other_over_coding_agent"
        elif coding_agent_in_pool and coding_agent_rank != 1:
            fault_class = "bind_ranked_coding_agent_below_top"
        elif not coding_agent_in_pool and coding_agent_in_trace is not None:
            fault_class = "bind_cut_coding_agent_from_pool"
        elif not coding_agent_in_pool and coding_agent_in_trace is None:
            fault_class = "frontchain_semantics_no_step_kind_match"
        else:
            fault_class = "other"

        row = {
            "subgoal_id": sg.subgoal_id,
            "label": sg.label,
            "description": sg.description[:180] + ("..." if len(sg.description) > 180 else ""),
            "subgoal_kind": sg.subgoal_kind,
            "required_capability_classes": sg.required_capability_classes,
            "input_evidence_kinds": sg.input_evidence_kinds,
            "output_evidence_kinds": sg.output_evidence_kinds,
            "graph_role": sg.graph_role,
            "human_review_required": sg.human_review_required,
            "narrow_trace": narrow_trace,
            "pool_after_all_filters": {
                "candidate_pool": pool.candidate_pool,
                "preferred_capability_id": pool.preferred_capability_id,
                "coverage": pool.coverage,
                "canonical_step_kinds": pool.canonical_step_kinds,
                "reasoning": pool.reasoning,
            },
            "coding_agent_narrower_entry": coding_agent_in_trace,
            "coding_agent_in_pool": coding_agent_in_pool,
            "coding_agent_rank_in_pool": coding_agent_rank,
            "shape_pick_index": shape_pick_idx,
            "selected_capability_id": selected_id,
            "selection_source": selection_source,
            "fault_class": fault_class,
            "pick_classification": pick_classification,
            "pick_reason": pick_reason,
            "detected_task_domains": sorted(task_domains),
            "selected_scope_domains": sorted(selected_scope),
        }
        report_rows.append(row)

    out = {
        "intent": intent,
        "elapsed_seconds": round(time.time() - t0, 1),
        "shape_llm_failed": shape_proposal.failed,
        "shape_llm_fallback_reason": shape_proposal.fallback_reason,
        "shape_llm_reasoning_summary": shape_proposal.reasoning_summary,
        "subgoals": report_rows,
    }

    report_path = Path("/tmp/audit_planner_csv_report.json")
    report_path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")

    # Regression guard moved below the console summary — see the
    # AssertionError message for the diagnostic (by then we've already
    # printed the full trace, which is what you actually want to read).

    # --- Console summary ---
    print("\n" + "=" * 78)
    print("  Per-subgoal selection trace (CSV aggregation task)")
    print("=" * 78)
    for row in report_rows:
        print(f"\n-- {row['subgoal_id']}: {row['label']}")
        print(f"   kind={row['subgoal_kind']}  graph_role={row['graph_role']}")
        print(f"   required_capability_classes={row['required_capability_classes']}")
        print(f"   input_evidence={row['input_evidence_kinds']}")
        print(f"   output_evidence={row['output_evidence_kinds']}")
        print(f"   description: {row['description']}")
        print(f"   canonical step_kinds: {row['pool_after_all_filters']['canonical_step_kinds']}")
        print("   narrower raw scores:")
        for block in row["narrow_trace"]["per_step_kind"]:
            sk = block["step_kind"]
            print(f"     [step_kind={sk}]  indexed={block.get('indexed_count', 0)}")
            for item in block["scored"][:8]:
                print(f"        {item['score']:>5}  {item['capability_id']}")
        pool = row["pool_after_all_filters"]
        print(f"   final pool (after rescore + filters): {pool['candidate_pool']}")
        print(f"   coverage: {pool['coverage']}   preferred[0]: {pool['preferred_capability_id']}")
        if row["coding_agent_in_pool"]:
            print(
                f"   coding_agent: in pool at rank {row['coding_agent_rank_in_pool']}"
            )
        elif row["coding_agent_narrower_entry"]:
            print(
                f"   coding_agent: scored "
                f"{row['coding_agent_narrower_entry']['raw_score']} "
                f"(rank {row['coding_agent_narrower_entry']['raw_rank_among_step_kind']}"
                f"/{row['coding_agent_narrower_entry']['raw_rank_total']}) "
                f"— CUT before pool"
            )
        else:
            print("   coding_agent: not even indexed for these step_kinds")
        print(
            f"   shape_pick_index={row['shape_pick_index']}  "
            f"selected={row['selected_capability_id']}  "
            f"source={row['selection_source']}"
        )
        verdict_marker = {
            "right_pick": "✔",
            "wrong_pick": "✘",
            "uncertain": "·",
            "no_selection": "?",
        }.get(row["pick_classification"], "?")
        print(
            f"   >>> verdict: {verdict_marker} {row['pick_classification']}  "
            f"(task_domains={row['detected_task_domains']} "
            f"selected_scope={row['selected_scope_domains']})"
        )
        print(f"       reason: {row['pick_reason']}")
        print(f"       [fault-class breadcrumb: {row['fault_class']}]")

    # --- Classifier summary ---
    from collections import Counter

    verdicts = Counter(r["pick_classification"] for r in report_rows)
    faults = Counter(r["fault_class"] for r in report_rows)
    print("\n" + "=" * 78)
    print("  Classification histogram (actionable)")
    print("=" * 78)
    verdict_order = ["right_pick", "wrong_pick", "uncertain", "no_selection"]
    for cls in verdict_order:
        if cls in verdicts:
            marker = {"right_pick": "✔", "wrong_pick": "✘", "uncertain": "·"}.get(cls, "?")
            print(f"   {marker} {verdicts[cls]:>3}  {cls}")
    print("\n" + "=" * 78)
    print("  Fault-class breadcrumbs (diagnostic — which layer would move)")
    print("=" * 78)
    for cls, n in faults.most_common():
        print(f"   {n}  {cls}")
    print(f"\nfull json: {report_path}")
    print(f"elapsed: {out['elapsed_seconds']}s")

    # --- Regression guard: coding_agent must survive top-k for any
    # step_kind it claims to support. If the pool for such a subgoal
    # drops coding_agent, the envelope / scoring is still broken.
    # We run the assertion AFTER the summary so a failing audit still
    # prints the diagnostic trace.
    cut_after_indexed: list[str] = []
    for row in report_rows:
        if row["coding_agent_narrower_entry"] is not None and not row["coding_agent_in_pool"]:
            cut_after_indexed.append(
                f"{row['subgoal_id']} ({row['label']}) step_kinds="
                f"{row['pool_after_all_filters']['canonical_step_kinds']} "
                f"raw_rank="
                f"{row['coding_agent_narrower_entry']['raw_rank_among_step_kind']}/"
                f"{row['coding_agent_narrower_entry']['raw_rank_total']}"
            )
    if cut_after_indexed:
        print("\nREGRESSION GUARD A FAILED — coding_agent was indexed but cut:")
        for line in cut_after_indexed:
            print(f"  - {line}")
        print(
            "  The envelope is now populated, so this is a scorer / top_k "
            "design issue (coding_agent penalised for being generalist)."
        )
        return 1

    # --- Regression guard B: no wrong_pick subgoals.
    # A ``wrong_pick`` means shape-LLM picked a capability whose registry
    # scope tag contradicts the task's detected domain — a recognisable
    # failure mode. If this count climbs on a new run, we regressed.
    wrong_picks = [r for r in report_rows if r["pick_classification"] == "wrong_pick"]
    if wrong_picks:
        print("\nREGRESSION GUARD B FAILED — wrong_pick subgoals:")
        for r in wrong_picks:
            print(
                f"  - {r['subgoal_id']} ({r['label']}): "
                f"selected={r['selected_capability_id']} "
                f"scope={r['selected_scope_domains']} "
                f"task_domains={r['detected_task_domains']}"
            )
            print(f"    {r['pick_reason']}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
