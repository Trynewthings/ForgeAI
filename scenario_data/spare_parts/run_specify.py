"""Run the spec pass against the seeded spare-parts session using the
live Postgres DB and real LLM, bypass the HTTP layer so we don't need
to restart the running uvicorn processes.

Dumps the resulting NodeExecutionSpecBundle to run_trace/05_node_specs.json
and prints a compact summary.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import sys as _sys

_sys.path.insert(0, "/Users/llk/agentsFactory/apps/api")

from app.core.database import SessionLocal  # noqa: E402
from app.services.node_spec.node_spec_service import EdgeContractFailure  # noqa: E402
from app.services.node_spec.session_orchestration import specify_for_session  # noqa: E402

ROOT = Path("/Users/llk/agentsFactory/scenario_data/spare_parts")
TRACE = ROOT / "run_trace"

# From the previous bare run trace.
SESSION_ID = "eb2105c0-caa6-4a66-b42a-f888baeca2f5"


def main() -> int:
    db = SessionLocal()
    try:
        try:
            serialized, row = specify_for_session(SESSION_ID, db=db)
            db.commit()
        except EdgeContractFailure as exc:
            db.rollback()
            serialized = exc.bundle.model_dump(mode="json")
            failure_path = TRACE / "05b_node_specs_edge_failed.json"
            failure_path.write_text(json.dumps(serialized, ensure_ascii=False, indent=2))
            print(f"!! EDGE CONTRACT FAILED — bundle dumped to {failure_path}")
            report = serialized.get("edge_contract_report") or {}
            print(f"   checked_edges={report.get('checked_edges')}  "
                  f"skipped_edges={report.get('skipped_edges')}  "
                  f"issues={len(report.get('issues') or [])}")
            for i, issue in enumerate(report.get("issues") or []):
                print(f"   [{i+1}] {issue['kind']:25s} {issue['from_node_id']} → {issue['to_node_id']}")
                print(f"       {issue['detail']}")
                if issue.get("missing_fields"):
                    print(f"       missing_fields: {issue['missing_fields']}")
            return 2
    finally:
        db.close()

    out_path = TRACE / "05_node_specs.json"
    out_path.write_text(json.dumps(serialized, ensure_ascii=False, indent=2, default=str))
    print(f"wrote {out_path}  size={out_path.stat().st_size}B")
    print()
    edge_report = serialized.get("edge_contract_report") or {}
    print(f"edge_contract    : checked={edge_report.get('checked_edges')}  "
          f"skipped={edge_report.get('skipped_edges')}  "
          f"issues={len(edge_report.get('issues') or [])}  "
          f"PASSED={not edge_report.get('issues')}")
    trace = serialized.get("llm_trace") or {}
    print(f"llm calls        : {trace.get('call_count')}")
    bundle_attempts = trace.get("bundle_retry_attempts") or []
    print(f"bundle retries   : {len(bundle_attempts)} (cap={trace.get('bundle_max_retries')})")
    for i, a in enumerate(bundle_attempts):
        print(f"   [retry {i+1}] respec={a['respec_node_ids']}  "
              f"issues_before={a['issues_before']} -> issues_after={a['issues_after']}  "
              f"calls={a['per_attempt_calls']}")
    print(f"spec_version     : {serialized['spec_version']}")
    print(f"spec count       : {len(serialized['specs'])}")
    print(f"skipped_node_ids : {serialized['skipped_node_ids']}")
    print(f"profiled_files   : {len(serialized['profiling_summary'].get('files') or [])}")
    print(f"llm call_count   : {serialized['llm_trace'].get('call_count')}")
    print()
    for spec in serialized["specs"]:
        print(f"== {spec['node_id']}  (runtime_class={spec['execution_constraints']['expected_runtime_class']}) ==")
        print(f"  semantic_goal     : {spec['semantic_goal']}")
        ic = spec["input_contract"]
        oc = spec["output_contract"]
        print(f"  inputs.required   : {len(ic.get('required') or [])}")
        for slot in ic.get("required") or []:
            print(f"      - {slot['name']:24s} kind={slot['kind']:18s} src={slot.get('source_hint') or '-'}")
            if slot.get("schema_hint"):
                print(f"        schema_hint: {slot['schema_hint']}")
        po = oc["primary_output"]
        print(f"  output.primary    : {po['name']:24s} kind={po['kind']:18s}")
        if po.get("schema_hint"):
            print(f"        schema_hint: {po['schema_hint']}")
        if po.get("locator_hint"):
            print(f"        locator_hint: {po['locator_hint']}")
        print(f"  business_rules    : {len(spec['business_rules'])}")
        for rule in spec["business_rules"][:3]:
            print(f"      - [{rule.get('category','?')}] {rule['rule']}")
        print(f"  acceptance        : {len(spec['acceptance_criteria'])}")
        for c in spec["acceptance_criteria"][:3]:
            print(f"      - [{c['kind']}] {c['statement']}")
        print(f"  sketch            : {len(spec.get('implementation_sketch') or [])} bullets")
        for s in (spec.get("implementation_sketch") or [])[:6]:
            print(f"      • {s}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
