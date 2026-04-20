from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.enums import ProjectStatus
from app.core.exceptions import AppException
from app.db.models.entities import Project
from app.repositories.build_task_repository import BuildTaskRepository
from app.repositories.project_repository import ProjectRepository
from app.schemas.project import (
    BuildTaskListItem,
    ProjectCreateRequest,
    ProjectDetailResponse,
    ProjectListResponse,
    ProjectResponse,
)
from app.services.project_runtime_cleanup_service import ProjectRuntimeCleanupService


def _iso(value) -> str:
    return value.isoformat() if value else ""


class ProjectService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.project_repository = ProjectRepository(db)
        self.build_task_repository = BuildTaskRepository(db)
        self.runtime_cleanup_service = ProjectRuntimeCleanupService(db)

    def create_project(self, payload: ProjectCreateRequest) -> ProjectResponse:
        project = Project(name=payload.name.strip(), description=payload.description, status=ProjectStatus.ACTIVE.value)
        self.project_repository.create(project)
        self.db.commit()
        return self._serialize_project(project)

    def list_projects(self) -> ProjectListResponse:
        projects = self.project_repository.list_all()
        return ProjectListResponse(items=[self._serialize_project(item) for item in projects], total=len(projects))

    def get_project(self, project_id: str) -> ProjectDetailResponse:
        project = self.project_repository.get_by_id(project_id, with_children=True)
        if project is None:
            raise AppException("Project not found", status_code=404)

        build_tasks = sorted(project.build_tasks, key=lambda item: item.created_at, reverse=True)

        return ProjectDetailResponse(
            **self._serialize_project(project).model_dump(),
            build_tasks=[
                BuildTaskListItem(
                    id=item.id,
                    status=item.status,
                    total_tokens=item.total_tokens,
                    created_at=_iso(item.created_at),
                    updated_at=_iso(item.updated_at),
                )
                for item in build_tasks
            ],
        )

    def delete_project(self, project_id: str) -> None:
        project = self.project_repository.get_by_id(project_id)
        if project is None:
            raise AppException("Project not found", status_code=404)
        self.project_repository.delete(project)
        self.db.commit()
        self.runtime_cleanup_service.delete_project_generated_storage(project_id)

    def _serialize_project(self, project: Project) -> ProjectResponse:
        return ProjectResponse(
            id=project.id,
            name=project.name,
            description=project.description,
            status=project.status,
            created_at=_iso(project.created_at),
            updated_at=_iso(project.updated_at),
        )
