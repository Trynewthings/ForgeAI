"""Reset state.planner_output_json to the original first-run planner
emission (with the wrong capability picks) so we can validate that
task 7's auto-repair fires correctly.

This is the OPPOSITE of patch_planner_to_coding_agent.py: we want
the broken state restored so the repair has something to fix.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/llk/agentsFactory/apps/api")

from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.db.models import FrontChainState  # noqa: E402

SESSION_ID = "eb2105c0-caa6-4a66-b42a-f888baeca2f5"
ORIGINAL_PLAN_PATH = Path(
    "/Users/llk/agentsFactory/scenario_data/spare_parts/run_trace/02_plan_response.json"
)


def main() -> int:
    if not ORIGINAL_PLAN_PATH.exists():
        print(f"original plan trace not found: {ORIGINAL_PLAN_PATH}")
        return 1
    original = json.loads(ORIGINAL_PLAN_PATH.read_text())
    original_planner_output = original.get("planner_output")
    if not original_planner_output:
        print("plan trace has no planner_output payload")
        return 1

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
        if row is None:
            print("no planner-output state for session")
            return 1

        row.planner_output_json = original_planner_output
        flag_modified(row, "planner_output_json")
        db.commit()
        print(f"reset planner_output for session {SESSION_ID}")
        for n in row.planner_output_json.get("node_drafts") or []:
            bc = (n.get("planner_metadata") or {}).get("bound_capability") or {}
            print(
                f"  {n['draft_node_id']:36s} cap={bc.get('selected_capability_id')} "
                f"src={bc.get('selection_source')}"
            )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
