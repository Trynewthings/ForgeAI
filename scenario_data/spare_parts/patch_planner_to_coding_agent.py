"""One-time admin patch: rewrite planner_output.bound_capability so the
spare-parts pipeline can actually run.

Two classes of fix (both simulate planner repair, task 5):

  1. transform nodes mis-bound to specialised capabilities
     (validate_schema, filesystem_read). spec validators correctly
     block the build because these caps can't carry bounded_transform
     work. Patch them to coding_agent (generalist accepts any class).

  2. bounded_acquire node bound to filesystem_read but the IC locator
     is a DIRECTORY containing multiple files. filesystem_read is a
     single-file primitive; it can't iterate. Patch to coding_agent so
     the runtime lets the script walk the directory.

Idempotent — safe to re-run; only rewrites entries that disagree.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/Users/llk/agentsFactory/apps/api")

from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.db.models import FrontChainState  # noqa: E402

SESSION_ID = "eb2105c0-caa6-4a66-b42a-f888baeca2f5"


def main() -> int:
    db = SessionLocal()
    try:
        row = (
            db.query(FrontChainState)
            .filter(
                FrontChainState.session_id == SESSION_ID,
                FrontChainState.planner_output_json.isnot(None),
            )
            .order_by(FrontChainState.created_at.desc())
            .first()
        )
        if row is None or not row.planner_output_json:
            print("no planner-output state found")
            return 1

        po = row.planner_output_json
        # Build node_id → spec.runtime_class lookup so we know which nodes
        # are bounded_transform.
        runtime_classes_by_id: dict = {}
        if row.node_specs_json:
            for s in row.node_specs_json.get("specs") or []:
                rc = (s.get("execution_constraints") or {}).get(
                    "expected_runtime_class"
                )
                if rc:
                    runtime_classes_by_id[s["node_id"]] = rc

        # Detect whether the input locator is a directory — if so, the
        # acquire node's filesystem_read pick won't actually run.
        ic = (row.compile_context_json or {}).get("input_closure") or {}
        input_locator = (ic.get("resolved_source_locator") or "").rstrip("/")
        from pathlib import Path

        input_locator_is_directory = (
            bool(input_locator) and Path(input_locator).is_dir()
        )

        patched = []
        for node in po.get("node_drafts") or []:
            nid = node.get("draft_node_id")
            md = node.get("planner_metadata") or {}
            bc = md.get("bound_capability") or {}
            picked = bc.get("selected_capability_id")
            spec_class = runtime_classes_by_id.get(nid)

            should_patch = False
            reason = ""
            # Case 1: transform node bound to specialised capability.
            if (
                spec_class == "bounded_transform"
                and picked not in {"coding_agent", "general_agentic_reasoning", None}
            ):
                should_patch = True
                reason = "transform-on-specialised"
            # Case 2: acquire node bound to filesystem_read but locator
            # is a directory (filesystem_read is single-file only).
            elif (
                spec_class == "bounded_acquire"
                and picked == "filesystem_read"
                and input_locator_is_directory
            ):
                should_patch = True
                reason = "acquire-of-directory"

            if not should_patch:
                continue

            bc["selected_capability_id"] = "coding_agent"
            bc["selection_source"] = "admin_patch_planner_repair_simulation"
            cands = bc.get("candidate_ids") or []
            if "coding_agent" not in cands:
                cands.append("coding_agent")
                bc["candidate_ids"] = cands
            md["bound_capability"] = bc
            node["planner_metadata"] = md
            node["preferred_capability_ids"] = ["coding_agent"]
            patched.append((nid, picked, reason))

        if not patched:
            print("nothing to patch — planner_output already aligned")
            return 0

        row.planner_output_json = po
        flag_modified(row, "planner_output_json")
        db.commit()
        for nid, original, reason in patched:
            print(f"  patched {nid}: {original} -> coding_agent  ({reason})")
        print(f"\npatched {len(patched)} node(s)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
