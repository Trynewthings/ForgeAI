from __future__ import annotations

import argparse
import json

from app.core.database import SessionLocal
from app.services.project_runtime_cleanup_service import ProjectRuntimeCleanupService


def main() -> None:
    parser = argparse.ArgumentParser(description="Prune legacy executable/run records and generated files.")
    parser.add_argument("--project-id", dest="project_id", default=None)
    parser.add_argument("--keep-latest", dest="keep_latest", type=int, default=5)
    args = parser.parse_args()

    db = SessionLocal()
    try:
        service = ProjectRuntimeCleanupService(db)
        if args.project_id:
            result = service.prune_project_runtime_records(args.project_id, keep_latest=args.keep_latest)
            print(json.dumps({"project_id": args.project_id, **result}, ensure_ascii=False, indent=2))
        else:
            result = service.prune_all_projects_runtime_records(keep_latest=args.keep_latest)
            print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
