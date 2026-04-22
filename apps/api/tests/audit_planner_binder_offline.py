"""Offline binder replay — no LLM, no network.

Runs the capability binder against a hand-constructed ``CompileContextResponse``
that mirrors what DeepSeek produced on the CSV aggregation task in the
P2.1 audit. Lets us iterate on binder fixes (envelope propagation,
generalist preserve slot, domain-affinity rescorer) without waiting on
or being blocked by DeepSeek network availability.

The subgoals below are copy-paste faithful to a successful run captured
in ``/tmp/audit_planner_csv_report.json`` (before the domain rescorer
landed). We only adjust if the taxonomy legitimately requires it.

Run:

    cd apps/api && source .venv/bin/activate && \\
        PYTHONPATH=. python tests/audit_planner_binder_offline.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LLM_MODE", "mock")
os.environ.setdefault("CORS_ORIGINS", '["http://localhost:1420"]')


def main() -> int:
    from app.schemas.solvability import SolvabilitySubgoal
    from app.services.capabilities import CapabilityRegistryService
    from app.services.workflow_planner.capability_binder import (
        PlannerCapabilityBinder,
    )
    from app.services.workflow_planner.planner_context_builder import (
        PlannerContext,
    )

    # Build envelope the same way planner_context_builder does, but
    # directly from the live registry (no compile_context needed).
    reg = CapabilityRegistryService()
    all_caps = reg.list_capabilities(context="build_planning")

    envelope_raw: list[dict] = []
    envelope_by_id: dict[str, dict] = {}
    for cap in all_caps:
        entry = {
            "capability_id": cap.capability_id,
            "logical_step_kinds": list(cap.supported_step_kinds or []),
            "supported_step_kinds": list(cap.supported_step_kinds or []),
            "requires_confirmation": bool(getattr(cap, "requires_confirmation", False)),
            "side_effect_class": "none",  # approximated; not used by domain rescorer
            "source_modalities_supported": list(
                getattr(cap, "source_modalities_supported", []) or []
            ),
            "tags": list(getattr(cap, "tags", []) or []),
            "capability_type": getattr(cap, "capability_type", None),
            "supported_actions": list(getattr(cap, "supported_actions", []) or []),
        }
        envelope_raw.append(entry)
        envelope_by_id[cap.capability_id] = entry

    ctx = PlannerContext(
        capability_envelope_raw=envelope_raw,
        capability_envelope_by_id=envelope_by_id,
        step_kind_preferences={},
        forbidden_side_effect_classes=[],
        # CSV demo's compile context (approximated).
        input_source_modality="csv",
        input_source_kind="local_file",
        input_locator_hint="/tmp/demo_coding_agent_ws/orders.csv",
        output_deliverable_form="csv_file",
    )

    # Subgoals copy-pasted from /tmp/audit_planner_csv_report.json
    # (DeepSeek's decomposition of the CSV aggregation task).
    subgoals = [
        SolvabilitySubgoal(
            subgoal_id="sg_1",
            label="Read source CSV data",
            description="Acquire the raw CSV data from the specified local file path.",
            subgoal_kind="acquire",
            required_capability_classes=["ingest"],
            input_evidence_kinds=["file_path"],
            output_evidence_kinds=["raw_csv_data"],
            graph_role="transform",
        ),
        SolvabilitySubgoal(
            subgoal_id="sg_2",
            label="Parse and aggregate order data",
            description="Parse CSV data, group by product line, compute order_count and total_revenue.",
            subgoal_kind="analyze",
            required_capability_classes=["transform"],
            input_evidence_kinds=["raw_csv_data"],
            output_evidence_kinds=["aggregated_data"],
            graph_role="transform",
        ),
        SolvabilitySubgoal(
            subgoal_id="sg_3",
            label="Sort aggregated results",
            description="Sort the aggregated data in descending order by total_revenue.",
            subgoal_kind="analyze",
            required_capability_classes=["rank"],
            input_evidence_kinds=["aggregated_data"],
            output_evidence_kinds=["sorted_aggregated_data"],
            graph_role="transform",
        ),
        SolvabilitySubgoal(
            subgoal_id="sg_4",
            label="Format final report",
            description="Format the sorted aggregated data as the final CSV structure.",
            subgoal_kind="synthesize",
            required_capability_classes=["summarize"],
            input_evidence_kinds=["sorted_aggregated_data"],
            output_evidence_kinds=["formatted_report"],
            graph_role="transform",
        ),
        SolvabilitySubgoal(
            subgoal_id="sg_5",
            label="Write output CSV file",
            description="Save the formatted CSV to the specified output path.",
            subgoal_kind="deliver",
            required_capability_classes=["persist"],
            input_evidence_kinds=["formatted_report", "output_path"],
            output_evidence_kinds=["final_csv_file"],
            graph_role="transform",
        ),
    ]

    pools = PlannerCapabilityBinder().narrow_for_subgoals(subgoals, ctx)

    print("\n" + "=" * 72)
    print("  Offline binder replay — CSV aggregation task")
    print("=" * 72)
    for sg in subgoals:
        pool = pools[sg.subgoal_id]
        print(f"\n-- {sg.subgoal_id}: {sg.label}")
        print(f"   input_evidence={sg.input_evidence_kinds}  output_evidence={sg.output_evidence_kinds}")
        print(f"   canonical_step_kinds={pool.canonical_step_kinds}")
        print(f"   pool ({len(pool.candidate_pool)}): {pool.candidate_pool}")
        print(f"   coverage={pool.coverage}  preferred={pool.preferred_capability_id}")

        # Show how each candidate is scoped (for visual confirmation).
        for cid in pool.candidate_pool:
            tags = envelope_by_id.get(cid, {}).get("tags") or []
            scope = [t for t in tags if t.endswith("_scoped") or t == "generalist_fallback"]
            marker = f" [{' '.join(scope)}]" if scope else ""
            print(f"     - {cid}{marker}")

    # Summary: how did the pool change vs the pre-P2.2-b snapshot?
    print("\n" + "=" * 72)
    print("  P2.2-b acceptance check")
    print("=" * 72)
    expected = {
        "sg_2": {
            "no_longer_first": {"repository_relationship_map", "repository_execution_surface_inspect"},
            "must_appear": {"coding_agent"},
        },
        "sg_4": {
            "no_longer_first": {"architecture_report_synthesis_llm", "architecture_report_synthesis", "architecture_report_brief"},
            "must_appear": {"coding_agent"},
        },
        "sg_5": {
            "no_longer_first": {"repository_evidence_bundle", "architecture_report_render"},
            "must_appear": {"coding_agent"},
        },
    }

    failures = []
    for sg_id, checks in expected.items():
        pool = pools[sg_id].candidate_pool
        if not pool:
            failures.append(f"{sg_id}: pool is empty")
            continue
        for cid in checks["must_appear"]:
            if cid not in pool:
                failures.append(f"{sg_id}: {cid} missing from pool {pool}")
        top = pool[0]
        if top in checks["no_longer_first"]:
            failures.append(
                f"{sg_id}: domain-mismatched {top!r} still at pool[0] — rescorer failed"
            )
    if failures:
        print("\nFAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nALL PASSED:")
    print("  * domain-scoped capabilities no longer lead the transform / summarize / persist pools")
    print("  * coding_agent is present (preserve slot) in every subgoal where indexed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
