"""LLM Planner Shadow Evaluation Harness.

Runs real LLM planner against 3 domain scenarios, compares with deterministic planner,
and collects structured metrics.

Usage:
    cd apps/api
    source .venv/bin/activate
    python tests/shadow_evaluation.py

Requires: LLM_MODE=real in .env, DeepSeek API key configured.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure app is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Force real LLM mode for this evaluation
os.environ["LLM_MODE"] = "real"

from app.core.config import settings
from app.schemas.workflow_planner import WorkflowPlannerOutput
from app.services.workflow_planner_llm_service import (
    WorkflowPlannerLLMResult,
    WorkflowPlannerLLMService,
    WorkflowPlannerShadowComparator,
)
from app.services.workflow_planner_service import WorkflowPlannerService


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Phase 1 scenarios: explicit depends_on (structural mapping test)
# ---------------------------------------------------------------------------

SCENARIOS_EXPLICIT = [
    {
        "name": "repository_multimodule_explicit",
        "domain": "repository",
        "description": "Repository multi-module architecture analysis (explicit deps)",
        "steps": [
            {"step_name": "scan_repo", "node_name": "scanner", "logical_step_kind": "acquire", "purpose": "Scan repository structure and list modules"},
            {"step_name": "analyze_api", "node_name": "api_analyzer", "logical_step_kind": "analyze", "depends_on": ["scan_repo"], "purpose": "Analyze API module architecture"},
            {"step_name": "analyze_worker", "node_name": "worker_analyzer", "logical_step_kind": "analyze", "depends_on": ["scan_repo"], "purpose": "Analyze worker module structure"},
            {"step_name": "analyze_desktop", "node_name": "desktop_analyzer", "logical_step_kind": "analyze", "depends_on": ["scan_repo"], "purpose": "Analyze desktop module patterns"},
            {"step_name": "merge_report", "node_name": "merger", "logical_step_kind": "summarize", "depends_on": ["analyze_api", "analyze_worker", "analyze_desktop"], "purpose": "Synthesize multi-module architecture report"},
        ],
        "runtime_constraints": {
            "semantic_interpretation": {"semantic_core": {"target_object": "repository"}},
        },
    },
    {
        "name": "research_briefing_explicit",
        "domain": "web_resource",
        "description": "AI safety research briefing from web sources (explicit deps)",
        "steps": [
            {"step_name": "gather_sources", "node_name": "gatherer", "logical_step_kind": "acquire", "purpose": "Search and fetch AI safety research sources"},
            {"step_name": "analyze_academic", "node_name": "academic", "logical_step_kind": "analyze", "depends_on": ["gather_sources"], "purpose": "Analyze academic papers and citations"},
            {"step_name": "analyze_policy", "node_name": "policy", "logical_step_kind": "analyze", "depends_on": ["gather_sources"], "purpose": "Analyze policy documents and recommendations"},
            {"step_name": "synthesize_briefing", "node_name": "synthesizer", "logical_step_kind": "summarize", "depends_on": ["analyze_academic", "analyze_policy"], "purpose": "Synthesize structured research briefing with evidence"},
        ],
        "runtime_constraints": {
            "semantic_interpretation": {"semantic_core": {"target_object": "web_resource", "evidence_basis": "academic_and_policy"}},
        },
    },
]

# ---------------------------------------------------------------------------
# Phase 2 scenarios: NO depends_on — LLM must infer graph shape from semantics
# ---------------------------------------------------------------------------

SCENARIOS_NO_DEPS = [
    # --- LINEAR: should stay linear ---
    {
        "name": "repo_linear_no_deps",
        "domain": "repository",
        "description": "Simple linear repo scan → analyze → report (no depends_on)",
        "expected_shape": "linear",
        "expected_edge_count": 2,
        "steps": [
            {"step_name": "scan_repo", "node_name": "scanner", "logical_step_kind": "acquire", "purpose": "Scan repository file tree and identify key modules"},
            {"step_name": "analyze_architecture", "node_name": "analyzer", "logical_step_kind": "analyze", "purpose": "Analyze overall architecture patterns from scan results"},
            {"step_name": "write_report", "node_name": "reporter", "logical_step_kind": "summarize", "purpose": "Produce a structured architecture report from analysis"},
        ],
        "runtime_constraints": {
            "semantic_interpretation": {"semantic_core": {"target_object": "repository"}},
        },
    },
    {
        "name": "web_linear_no_deps",
        "domain": "web_resource",
        "description": "Simple linear: search → extract → summarize (no depends_on)",
        "expected_shape": "linear",
        "expected_edge_count": 2,
        "steps": [
            {"step_name": "search_sources", "node_name": "searcher", "logical_step_kind": "acquire", "purpose": "Search for relevant web articles on the topic"},
            {"step_name": "extract_evidence", "node_name": "extractor", "logical_step_kind": "analyze", "purpose": "Extract key claims and evidence from found articles"},
            {"step_name": "write_summary", "node_name": "writer", "logical_step_kind": "summarize", "purpose": "Write an evidence-backed summary from extracted findings"},
        ],
        "runtime_constraints": {
            "semantic_interpretation": {"semantic_core": {"target_object": "web_resource"}},
        },
    },
    # --- DIAMOND: should infer single-source → dual-branch → join ---
    {
        "name": "repo_diamond_no_deps",
        "domain": "repository",
        "description": "Diamond: scan repo → analyze frontend + analyze backend → merge (no depends_on)",
        "expected_shape": "diamond",
        "expected_edge_count": 4,
        "steps": [
            {"step_name": "scan_repo", "node_name": "scanner", "logical_step_kind": "acquire", "purpose": "Scan the full repository structure"},
            {"step_name": "analyze_frontend", "node_name": "fe_analyzer", "logical_step_kind": "analyze", "purpose": "Analyze the frontend module independently from scan results"},
            {"step_name": "analyze_backend", "node_name": "be_analyzer", "logical_step_kind": "analyze", "purpose": "Analyze the backend module independently from scan results"},
            {"step_name": "merge_report", "node_name": "merger", "logical_step_kind": "summarize", "purpose": "Merge frontend and backend analysis into a unified architecture report"},
        ],
        "runtime_constraints": {
            "semantic_interpretation": {"semantic_core": {"target_object": "repository"}},
        },
    },
    {
        "name": "web_diamond_no_deps",
        "domain": "web_resource",
        "description": "Diamond: gather → analyze academic + analyze industry → synthesize (no depends_on)",
        "expected_shape": "diamond",
        "expected_edge_count": 4,
        "steps": [
            {"step_name": "gather_sources", "node_name": "gatherer", "logical_step_kind": "acquire", "purpose": "Search and collect AI safety sources from multiple channels"},
            {"step_name": "analyze_academic", "node_name": "academic", "logical_step_kind": "analyze", "purpose": "Analyze academic research papers from gathered sources"},
            {"step_name": "analyze_industry", "node_name": "industry", "logical_step_kind": "analyze", "purpose": "Analyze industry reports and whitepapers from gathered sources"},
            {"step_name": "synthesize_briefing", "node_name": "synthesizer", "logical_step_kind": "summarize", "purpose": "Synthesize findings from both academic and industry analysis into a briefing"},
        ],
        "runtime_constraints": {
            "semantic_interpretation": {"semantic_core": {"target_object": "web_resource"}},
        },
    },
    # --- 3-WAY FAN-OUT: should infer scan → 3 parallel analyze → merge ---
    {
        "name": "repo_fanout3_no_deps",
        "domain": "repository",
        "description": "3-way fan-out: scan → analyze api + worker + desktop → merge (no depends_on)",
        "expected_shape": "fanout3",
        "expected_edge_count": 6,
        "steps": [
            {"step_name": "scan_repo", "node_name": "scanner", "logical_step_kind": "acquire", "purpose": "Scan the monorepo and identify all three application modules"},
            {"step_name": "analyze_api", "node_name": "api_analyzer", "logical_step_kind": "analyze", "purpose": "Analyze the API module architecture independently from scan"},
            {"step_name": "analyze_worker", "node_name": "worker_analyzer", "logical_step_kind": "analyze", "purpose": "Analyze the worker module architecture independently from scan"},
            {"step_name": "analyze_desktop", "node_name": "desktop_analyzer", "logical_step_kind": "analyze", "purpose": "Analyze the desktop module architecture independently from scan"},
            {"step_name": "merge_report", "node_name": "merger", "logical_step_kind": "summarize", "purpose": "Merge all three module analyses into a unified architecture report"},
        ],
        "runtime_constraints": {
            "semantic_interpretation": {"semantic_core": {"target_object": "repository"}},
        },
    },
    {
        "name": "web_fanout3_no_deps",
        "domain": "web_resource",
        "description": "3-way fan-out: gather → 3 parallel analysis tracks → synthesize (no depends_on)",
        "expected_shape": "fanout3",
        "expected_edge_count": 6,
        "steps": [
            {"step_name": "gather_sources", "node_name": "gatherer", "logical_step_kind": "acquire", "purpose": "Search and collect sources across academic, policy, and industry channels"},
            {"step_name": "analyze_academic", "node_name": "academic", "logical_step_kind": "analyze", "purpose": "Analyze academic papers independently from gathered sources"},
            {"step_name": "analyze_policy", "node_name": "policy", "logical_step_kind": "analyze", "purpose": "Analyze policy documents independently from gathered sources"},
            {"step_name": "analyze_industry", "node_name": "industry", "logical_step_kind": "analyze", "purpose": "Analyze industry reports independently from gathered sources"},
            {"step_name": "synthesize_briefing", "node_name": "synthesizer", "logical_step_kind": "summarize", "purpose": "Synthesize findings from all three analysis tracks into a comprehensive briefing"},
        ],
        "runtime_constraints": {
            "semantic_interpretation": {"semantic_core": {"target_object": "web_resource"}},
        },
    },
]

# Default: use no-deps scenarios
SCENARIOS = SCENARIOS_NO_DEPS


# ---------------------------------------------------------------------------
# Mock blueprint for direct planner call
# ---------------------------------------------------------------------------

class FakeBlueprint:
    """Minimal blueprint-like object for direct planner invocation."""

    def __init__(self, scenario: dict):
        self.id = f"eval_{scenario['name']}"
        self.project_id = "eval_project"
        self.name = scenario["description"]
        self.blueprint_type = "workflow"
        self.execution_steps_json = {"steps": scenario["steps"]}
        self.runtime_constraints_json = scenario.get("runtime_constraints", {})
        self.input_schema_json = {}
        self.output_schema_json = {}
        self.configurable_vars_json = {}
        self.artifacts_spec_json = {}
        self.execution_env_json = {}


# ---------------------------------------------------------------------------
# Evaluation runner
# ---------------------------------------------------------------------------

def run_scenario(scenario: dict, run_index: int = 1) -> dict[str, Any]:
    """Run a single scenario: deterministic + LLM planner, compare."""
    bp = FakeBlueprint(scenario)
    result: dict[str, Any] = {
        "scenario": scenario["name"],
        "domain": scenario["domain"],
        "run_index": run_index,
        "step_count": len(scenario["steps"]),
        "has_depends_on": any(isinstance(s.get("depends_on"), list) for s in scenario["steps"]),
    }

    # 1. Deterministic planner
    det_start = time.time()
    try:
        det_output = WorkflowPlannerService().plan(bp)
        result["deterministic"] = {
            "success": True,
            "node_count": len(det_output.node_drafts),
            "edge_count": len(det_output.edge_drafts),
            "entry_nodes": det_output.entry_node_ids,
            "exit_nodes": det_output.exit_node_ids,
            "draft_status": det_output.draft_status,
            "unresolved_count": len(det_output.unresolved_items),
            "latency_ms": round((time.time() - det_start) * 1000),
        }
    except Exception as exc:
        import traceback
        result["deterministic"] = {"success": False, "error": str(exc), "traceback": traceback.format_exc()}
        print(f"  [ERROR] Deterministic planner: {exc}")
        return result

    # 2. LLM planner
    llm_start = time.time()
    try:
        llm_result = WorkflowPlannerLLMService().plan(bp)
        result["llm"] = {
            "success": llm_result.success,
            "repaired": llm_result.repaired,
            "fallback_reason": llm_result.fallback_reason,
            "validation_issues": llm_result.validation_issues,
            "latency_ms": round((time.time() - llm_start) * 1000),
            "stage_trace": llm_result.stage_trace,
        }
        if llm_result.output is not None:
            result["llm"]["node_count"] = len(llm_result.output.node_drafts)
            result["llm"]["edge_count"] = len(llm_result.output.edge_drafts)
            result["llm"]["entry_nodes"] = llm_result.output.entry_node_ids
            result["llm"]["exit_nodes"] = llm_result.output.exit_node_ids
            result["llm"]["draft_status"] = llm_result.output.draft_status
            result["llm"]["unresolved_count"] = len(llm_result.output.unresolved_items)
            result["llm"]["planner_version"] = llm_result.output.planner_version
    except Exception as exc:
        import traceback
        result["llm"] = {"success": False, "error": str(exc), "traceback": traceback.format_exc()}
        print(f"  [ERROR] LLM planner: {exc}")
        return result

    # 3. Shadow comparison
    diff = WorkflowPlannerShadowComparator().compare(det_output, llm_result)
    result["comparison"] = diff

    # 4. Extract key metrics
    if diff.get("comparison_status") == "completed":
        llm_edge_count = diff.get("edge_count", {}).get("llm", 0)
        det_edge_count = diff.get("edge_count", {}).get("deterministic", 0)
        expected_edges = scenario.get("expected_edge_count")

        result["metrics"] = {
            "topology_match": diff.get("edge_topology_match", False),
            "entry_exit_match": diff.get("entry_exit", {}).get("entry_match", False) and diff.get("entry_exit", {}).get("exit_match", False),
            "node_count_match": diff.get("node_count", {}).get("match", False),
            "edge_count_match": diff.get("edge_count", {}).get("match", False),
            "draft_status_match": diff.get("draft_status", {}).get("match", False),
            "validator_repaired": llm_result.repaired,
            "fallback": llm_result.fallback_reason is not None,
        }

        # Over/under-parallelization detection (for no-depends_on scenarios)
        if expected_edges is not None and llm_result.output is not None:
            result["metrics"]["expected_edge_count"] = expected_edges
            result["metrics"]["llm_edge_count"] = llm_edge_count
            result["metrics"]["over_parallelized"] = llm_edge_count > expected_edges
            result["metrics"]["under_parallelized"] = llm_edge_count < expected_edges
            result["metrics"]["edge_count_correct"] = llm_edge_count == expected_edges

        # Shape classification
        expected_shape = scenario.get("expected_shape")
        if expected_shape and llm_result.output is not None:
            result["metrics"]["expected_shape"] = expected_shape
            result["metrics"]["shape_correct"] = _check_shape(llm_result.output, expected_shape)
    else:
        result["metrics"] = {
            "topology_match": False,
            "entry_exit_match": False,
            "node_count_match": False,
            "edge_count_match": False,
            "draft_status_match": False,
            "validator_repaired": False,
            "fallback": True,
        }

    return result


def _check_shape(output: WorkflowPlannerOutput, expected: str) -> bool:
    """Check if LLM output matches the expected graph shape."""
    nodes = output.node_drafts
    edges = output.edge_drafts
    if not nodes:
        return False

    node_ids = {n.draft_node_id for n in nodes}
    successors: dict[str, set[str]] = {nid: set() for nid in node_ids}
    predecessors: dict[str, set[str]] = {nid: set() for nid in node_ids}
    for e in edges:
        if e.from_node_id in node_ids and e.to_node_id in node_ids:
            successors[e.from_node_id].add(e.to_node_id)
            predecessors[e.to_node_id].add(e.from_node_id)

    max_fan_out = max((len(s) for s in successors.values()), default=0)
    max_fan_in = max((len(p) for p in predecessors.values()), default=0)
    entry_count = sum(1 for nid in node_ids if not predecessors[nid])
    exit_count = sum(1 for nid in node_ids if not successors[nid])

    if expected == "linear":
        return max_fan_out <= 1 and max_fan_in <= 1
    elif expected == "diamond":
        return max_fan_out == 2 and max_fan_in == 2 and entry_count == 1 and exit_count == 1
    elif expected == "fanout3":
        return max_fan_out == 3 and max_fan_in == 3 and entry_count == 1 and exit_count == 1
    return False


def compute_aggregate(results: list[dict]) -> dict[str, Any]:
    """Compute aggregate metrics across all runs."""
    total = len(results)
    if total == 0:
        return {}

    completed = [r for r in results if r.get("metrics")]
    by_domain: dict[str, list[dict]] = {}
    for r in results:
        domain = r["domain"]
        by_domain.setdefault(domain, []).append(r)

    def rate(items: list[dict], key: str) -> float:
        vals = [r["metrics"].get(key, False) for r in items if r.get("metrics")]
        return round(sum(1 for v in vals if v) / max(len(vals), 1), 3)

    aggregate = {
        "total_runs": total,
        "total_success": sum(1 for r in results if r.get("llm", {}).get("success")),
        "overall": {
            "topology_match_rate": rate(completed, "topology_match"),
            "entry_exit_match_rate": rate(completed, "entry_exit_match"),
            "node_count_match_rate": rate(completed, "node_count_match"),
            "edge_count_match_rate": rate(completed, "edge_count_match"),
            "validator_repair_rate": rate(completed, "validator_repaired"),
            "fallback_rate": rate(completed, "fallback"),
            "shape_correct_rate": rate(completed, "shape_correct"),
            "over_parallelization_rate": rate(completed, "over_parallelized"),
            "under_parallelization_rate": rate(completed, "under_parallelized"),
        },
        "by_domain": {},
        "by_shape": {},
    }

    for domain, items in by_domain.items():
        aggregate["by_domain"][domain] = {
            "runs": len(items),
            "success": sum(1 for r in items if r.get("llm", {}).get("success")),
            "topology_match_rate": rate(items, "topology_match"),
            "entry_exit_match_rate": rate(items, "entry_exit_match"),
            "shape_correct_rate": rate(items, "shape_correct"),
            "over_parallelization_rate": rate(items, "over_parallelized"),
            "under_parallelization_rate": rate(items, "under_parallelized"),
            "validator_repair_rate": rate(items, "validator_repaired"),
            "fallback_rate": rate(items, "fallback"),
            "avg_llm_latency_ms": round(
                sum(r.get("llm", {}).get("latency_ms", 0) for r in items) / max(len(items), 1)
            ),
        }

    # By expected shape
    by_shape: dict[str, list[dict]] = {}
    for r in results:
        shape = r.get("metrics", {}).get("expected_shape", "unknown")
        by_shape.setdefault(shape, []).append(r)
    for shape, items in by_shape.items():
        aggregate["by_shape"][shape] = {
            "runs": len(items),
            "shape_correct_rate": rate(items, "shape_correct"),
            "over_parallelization_rate": rate(items, "over_parallelized"),
            "under_parallelization_rate": rate(items, "under_parallelized"),
            "edge_count_correct_rate": rate(items, "edge_count_correct"),
        }

    return aggregate


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"\n{'='*70}")
    print(f"  WorkflowPlanner LLM Shadow Evaluation")
    print(f"  LLM_MODE={settings.llm_mode}")
    print(f"  Provider={settings.default_llm_provider}")
    print(f"  Model={settings.default_llm_model_name}")
    print(f"  Started={datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*70}\n")

    if settings.llm_mode != "real":
        print("WARNING: LLM_MODE is not 'real'. Results will be mock-based.")
        print("Set LLM_MODE=real in .env for meaningful evaluation.\n")

    all_results: list[dict] = []
    runs_per_scenario = 2  # Run each scenario twice for stability check

    for scenario in SCENARIOS:
        for run_idx in range(1, runs_per_scenario + 1):
            print(f"\n--- {scenario['name']} (run {run_idx}/{runs_per_scenario}) ---")
            result = run_scenario(scenario, run_idx)
            all_results.append(result)

            # Print per-run summary
            det = result.get("deterministic", {})
            llm = result.get("llm", {})
            metrics = result.get("metrics", {})

            print(f"  Deterministic: {det.get('node_count', '?')} nodes, {det.get('edge_count', '?')} edges, {det.get('latency_ms', '?')}ms")
            if llm.get("success"):
                print(f"  LLM:           {llm.get('node_count', '?')} nodes, {llm.get('edge_count', '?')} edges, {llm.get('latency_ms', '?')}ms")
                print(f"  Repaired:      {llm.get('repaired', False)}")
                print(f"  Issues:        {llm.get('validation_issues', [])}")
            else:
                print(f"  LLM:           FAILED — {llm.get('fallback_reason') or llm.get('error', 'unknown')}")

            if metrics:
                print(f"  Topology match: {metrics.get('topology_match')}")
                print(f"  Entry/exit:     {metrics.get('entry_exit_match')}")
                if "expected_shape" in metrics:
                    print(f"  Expected shape: {metrics['expected_shape']} → correct={metrics.get('shape_correct')}")
                if "expected_edge_count" in metrics:
                    print(f"  Edges: expected={metrics['expected_edge_count']}, got={metrics.get('llm_edge_count')}"
                          f"  over={metrics.get('over_parallelized')}, under={metrics.get('under_parallelized')}")

    # Aggregate
    aggregate = compute_aggregate(all_results)

    print(f"\n{'='*70}")
    print(f"  AGGREGATE RESULTS")
    print(f"{'='*70}")
    print(json.dumps(aggregate, indent=2, ensure_ascii=False))

    # Save results
    output_dir = Path(__file__).parent / "shadow_evaluation_results"
    output_dir.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"evaluation_{timestamp}.json"
    output_path.write_text(
        json.dumps({
            "metadata": {
                "llm_mode": settings.llm_mode,
                "provider": settings.default_llm_provider,
                "model": settings.default_llm_model_name,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "runs_per_scenario": runs_per_scenario,
                "scenario_count": len(SCENARIOS),
            },
            "aggregate": aggregate,
            "results": all_results,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\nResults saved to: {output_path}")


if __name__ == "__main__":
    main()
