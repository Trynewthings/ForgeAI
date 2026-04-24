"""Admin patch: bump spec.execution_constraints.max_iterations on
transform nodes so coding_agent has enough budget to "look + decide +
write" (2+ iterations needed; default 1 is too tight for compute work).

Idempotent.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/Users/llk/agentsFactory/apps/api")

from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.db.models import FrontChainState  # noqa: E402

SESSION_ID = "eb2105c0-caa6-4a66-b42a-f888baeca2f5"

# Per-runtime-class min iterations.
_MIN_ITERATIONS = {
    "bounded_acquire": 2,
    "bounded_transform": 5,
    "bounded_persist": 2,
    "bounded_verify": 1,
    "bounded_agentic": 5,
}


def main() -> int:
    db = SessionLocal()
    try:
        row = (
            db.query(FrontChainState)
            .filter(
                FrontChainState.session_id == SESSION_ID,
                FrontChainState.node_specs_json.isnot(None),
            )
            .order_by(FrontChainState.created_at.desc())
            .first()
        )
        if row is None:
            print("no node_specs_json on this session")
            return 1

        specs = row.node_specs_json.get("specs") or []
        patched = []
        for s in specs:
            ec = s.get("execution_constraints") or {}
            cls = ec.get("expected_runtime_class")
            current = ec.get("max_iterations") or 1
            target = _MIN_ITERATIONS.get(cls, current)
            if current < target:
                ec["max_iterations"] = target
                s["execution_constraints"] = ec
                patched.append((s["node_id"], cls, current, target))

        if not patched:
            print("nothing to patch")
            return 0

        row.node_specs_json["specs"] = specs
        flag_modified(row, "node_specs_json")
        db.commit()
        for nid, cls, before, after in patched:
            print(f"  patched {nid:36s} ({cls:18s}) max_iterations: {before} -> {after}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
