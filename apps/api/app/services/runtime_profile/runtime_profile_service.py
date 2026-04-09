from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models import RuntimeProfile
from app.db.repositories.blueprint_repository import BlueprintRepository
from app.db.repositories.project_repository import ProjectRepository
from app.db.repositories.runtime_profile_repository import RuntimeProfileRepository
from app.schemas.runtime_profile import RuntimeProfileCreateRequest, RuntimeProfileResponse, RuntimeProfileUpdateRequest


def _iso(value) -> str:
    return value.isoformat() if value else ""


class RuntimeProfileService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.project_repository = ProjectRepository(db)
        self.blueprint_repository = BlueprintRepository(db)
        self.runtime_profile_repository = RuntimeProfileRepository(db)

    def create_runtime_profile(self, payload: RuntimeProfileCreateRequest) -> RuntimeProfileResponse:
        if self.project_repository.get_by_id(payload.project_id) is None:
            raise AppException("Project not found", status_code=404)
        if payload.blueprint_id is not None:
            blueprint = self.blueprint_repository.get_by_id(payload.blueprint_id)
            if blueprint is None:
                raise AppException("Blueprint not found", status_code=404)
            if blueprint.project_id != payload.project_id:
                raise AppException("Blueprint does not belong to the selected project", status_code=400)
        runtime_profile = RuntimeProfile(**payload.model_dump(mode="python", exclude_none=True))
        if runtime_profile.is_default:
            self._unset_existing_defaults(payload.project_id)
        self.runtime_profile_repository.create(runtime_profile)
        self.db.commit()
        return self._serialize(runtime_profile)

    def get_runtime_profile(self, runtime_profile_id: str) -> RuntimeProfileResponse:
        runtime_profile = self.runtime_profile_repository.get_by_id(runtime_profile_id)
        if runtime_profile is None:
            raise AppException("Runtime profile not found", status_code=404)
        return self._serialize(runtime_profile)

    def update_runtime_profile(self, runtime_profile_id: str, payload: RuntimeProfileUpdateRequest) -> RuntimeProfileResponse:
        runtime_profile = self.runtime_profile_repository.get_by_id(runtime_profile_id)
        if runtime_profile is None:
            raise AppException("Runtime profile not found", status_code=404)
        data = payload.model_dump(exclude_unset=True)
        if "blueprint_id" in data and data["blueprint_id"] is not None:
            blueprint = self.blueprint_repository.get_by_id(data["blueprint_id"])
            if blueprint is None:
                raise AppException("Blueprint not found", status_code=404)
            if blueprint.project_id != runtime_profile.project_id:
                raise AppException("Blueprint does not belong to the runtime profile project", status_code=400)
        if data.get("is_default") is True:
            self._unset_existing_defaults(runtime_profile.project_id)
        for field_name, value in data.items():
            setattr(runtime_profile, field_name, value)
        self.db.commit()
        self.db.refresh(runtime_profile)
        return self._serialize(runtime_profile)

    def list_project_runtime_profiles(self, project_id: str) -> list[RuntimeProfileResponse]:
        if self.project_repository.get_by_id(project_id) is None:
            raise AppException("Project not found", status_code=404)
        return [self._serialize(item) for item in self.runtime_profile_repository.list_by_project(project_id)]

    def list_blueprint_runtime_profiles(self, blueprint_id: str) -> list[RuntimeProfileResponse]:
        if self.blueprint_repository.get_by_id(blueprint_id) is None:
            raise AppException("Blueprint not found", status_code=404)
        return [self._serialize(item) for item in self.runtime_profile_repository.list_by_blueprint(blueprint_id)]

    def _serialize(self, runtime_profile: RuntimeProfile) -> RuntimeProfileResponse:
        return RuntimeProfileResponse(
            id=runtime_profile.id,
            project_id=runtime_profile.project_id,
            blueprint_id=runtime_profile.blueprint_id,
            name=runtime_profile.name,
            model_bindings_json=runtime_profile.model_bindings_json or {},
            skill_bindings_json=runtime_profile.skill_bindings_json or {},
            mcp_bindings_json=runtime_profile.mcp_bindings_json or {},
            env_vars_json=runtime_profile.env_vars_json or {},
            input_overrides_json=runtime_profile.input_overrides_json or {},
            resource_limits_json=runtime_profile.resource_limits_json or {},
            debug_options_json=runtime_profile.debug_options_json or {},
            is_default=runtime_profile.is_default,
            created_at=_iso(runtime_profile.created_at),
            updated_at=_iso(runtime_profile.updated_at),
        )

    def _unset_existing_defaults(self, project_id: str) -> None:
        for item in self.runtime_profile_repository.list_by_project(project_id):
            item.is_default = False
