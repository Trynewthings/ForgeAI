from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.enums import ProjectStatus
from app.core.exceptions import AppException
from app.db.models import Project
from app.db.repositories.project_repository import ProjectRepository
from app.schemas.project import (
    BuildTaskListItem,
    BuildListItem,
    BlueprintListItem,
    ExecutableListItem,
    ProjectCreateRequest,
    ProjectDetailResponse,
    ProjectListResponse,
    ProjectResponse,
    RequirementListItem,
    RunListItem,
    RuntimeProfileListItem,
)
from app.services.project_runtime_cleanup_service import ProjectRuntimeCleanupService


def _iso(value) -> str:
    return value.isoformat() if value else ""


class ProjectService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.project_repository = ProjectRepository(db)
        self.runtime_cleanup_service = ProjectRuntimeCleanupService(db)

    def create_project(self, payload: ProjectCreateRequest) -> ProjectResponse:
        project = Project(
            name=payload.name.strip(),
            description=payload.description,
            status=ProjectStatus.ACTIVE.value,
        )
        self.project_repository.create(project)
        self.db.commit()
        return self._serialize_project(project)

    def list_projects(self) -> ProjectListResponse:
        projects = self.project_repository.list_all()
        return ProjectListResponse(items=[self._serialize_project(project) for project in projects], total=len(projects))

    def get_project(self, project_id: str) -> ProjectDetailResponse:
        project = self.project_repository.get_by_id(project_id, with_v2_children=True)
        if project is None:
            raise AppException("Project not found", status_code=404)
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
                for item in sorted(project.build_tasks, key=lambda value: value.created_at, reverse=True)
            ],
            requirements=[
                RequirementListItem(
                    id=item.id,
                    title=item.title,
                    status=item.status,
                    version=item.version,
                    created_at=_iso(item.created_at),
                    updated_at=_iso(item.updated_at),
                )
                for item in sorted(project.requirements_v2, key=lambda value: value.created_at, reverse=True)
            ],
            blueprints=[
                BlueprintListItem(
                    id=item.id,
                    requirement_id=item.requirement_id,
                    blueprint_type=item.blueprint_type,
                    name=item.name,
                    status=item.status,
                    version=item.version,
                    created_at=_iso(item.created_at),
                    updated_at=_iso(item.updated_at),
                )
                for item in sorted(project.blueprints_v2, key=lambda value: value.created_at, reverse=True)
            ],
            builds=[
                BuildListItem(
                    id=item.id,
                    blueprint_id=item.blueprint_id,
                    executable_id=item.executable_id,
                    build_version=item.build_version,
                    status=item.status,
                    created_at=_iso(item.created_at),
                    updated_at=_iso(item.updated_at),
                )
                for item in sorted(project.builds_v2, key=lambda value: value.created_at, reverse=True)
            ],
            executables=[
                ExecutableListItem(
                    id=item.id,
                    blueprint_id=item.blueprint_id,
                    executable_type=item.executable_type,
                    build_version=item.build_version,
                    status=item.status,
                    created_at=_iso(item.created_at),
                    updated_at=_iso(item.updated_at),
                )
                for item in sorted(project.executables_v2, key=lambda value: value.created_at, reverse=True)
            ],
            runtime_profiles=[
                RuntimeProfileListItem(
                    id=item.id,
                    blueprint_id=item.blueprint_id,
                    name=item.name,
                    is_default=item.is_default,
                    created_at=_iso(item.created_at),
                    updated_at=_iso(item.updated_at),
                )
                for item in sorted(project.runtime_profiles_v2, key=lambda value: value.created_at, reverse=True)
            ],
            runs=[
                RunListItem(
                    id=item.id,
                    blueprint_id=item.blueprint_id,
                    executable_id=item.executable_id,
                    runtime_profile_id=item.runtime_profile_id,
                    status=item.status,
                    created_at=_iso(item.created_at),
                    updated_at=_iso(item.updated_at),
                )
                for item in sorted(project.runs_v2, key=lambda value: value.created_at, reverse=True)
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
