from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import ROOT_DIR
from app.db.repositories.executable_repository import ExecutableRepository
from app.db.repositories.project_repository import ProjectRepository
from app.db.repositories.run_repository import RunRepository


class ProjectRuntimeCleanupService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.project_repository = ProjectRepository(db)
        self.executable_repository = ExecutableRepository(db)
        self.run_repository = RunRepository(db)

    def prune_project_runtime_records(self, project_id: str, *, keep_latest: int = 5) -> dict[str, int]:
        executables = self.executable_repository.list_by_project(project_id)
        runs = self.run_repository.list_by_project(project_id)

        deleted_executables = 0
        deleted_runs = 0

        for executable in executables[max(keep_latest, 0) :]:
            self._remove_executable_storage(executable.project_id, executable.source_path, executable.build_version)
            self.db.delete(executable)
            deleted_executables += 1

        for run in runs[max(keep_latest, 0) :]:
            self._remove_run_storage(run.project_id, run.id)
            self.db.delete(run)
            deleted_runs += 1

        self.db.commit()
        return {
            "deleted_executables": deleted_executables,
            "deleted_runs": deleted_runs,
            "kept_executables": min(len(executables), max(keep_latest, 0)),
            "kept_runs": min(len(runs), max(keep_latest, 0)),
        }

    def prune_all_projects_runtime_records(self, *, keep_latest: int = 5) -> list[dict[str, int | str]]:
        results: list[dict[str, int | str]] = []
        for project in self.project_repository.list_all():
            summary = self.prune_project_runtime_records(project.id, keep_latest=keep_latest)
            results.append({"project_id": project.id, **summary})
        return results

    def delete_project_generated_storage(self, project_id: str) -> None:
        for path in self._project_generated_roots(project_id):
            self._remove_generated_path(path)

    def _project_generated_roots(self, project_id: str) -> list[Path]:
        return [
            ROOT_DIR / ".generated" / "projects" / project_id,
            ROOT_DIR / ".generated" / "executables" / project_id,
            ROOT_DIR / ".generated" / "runs" / project_id,
        ]

    def _remove_executable_storage(self, project_id: str, source_path: str | None, build_version: str | None) -> None:
        candidate: Path | None = None
        if source_path:
            candidate = Path(source_path)
        elif build_version:
            candidate = ROOT_DIR / ".generated" / "executables" / project_id / build_version
        if candidate and candidate.exists():
            shutil.rmtree(candidate, ignore_errors=True)

    def _remove_run_storage(self, project_id: str, run_id: str) -> None:
        candidate = ROOT_DIR / ".generated" / "runs" / project_id / run_id
        self._remove_generated_path(candidate)

    def _remove_generated_path(self, path: Path) -> None:
        if not path.exists():
            return
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            return
        try:
            path.unlink(missing_ok=True)
        except TypeError:
            if path.exists():
                path.unlink()
