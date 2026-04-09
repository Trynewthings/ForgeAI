from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models import Requirement
from app.db.repositories.project_repository import ProjectRepository
from app.db.repositories.requirement_repository import RequirementRepository
from app.schemas.requirement_payloads import (
    normalize_external_tools_payload,
    normalize_interactivity_level,
    normalize_optional_text,
    normalize_acceptance_criteria_payload,
    normalize_constraints_payload,
    normalize_recurrence_mode,
)
from app.schemas.requirement import RequirementCreateRequest, RequirementResponse, RequirementUpdateRequest


def _iso(value) -> str:
    return value.isoformat() if value else ""


class RequirementService:
    MATERIAL_FIELDS = {
        "title",
        "summary",
        "goal",
        "target_users",
        "input_description",
        "output_description",
        "domain",
        "interactivity_level",
        "human_review_required",
        "recurrence_mode",
        "constraints_json",
        "acceptance_criteria_json",
        "non_goals_json",
        "priority_features_json",
        "external_tools_needed_json",
        "raw_conversation_json",
        "structured_spec_json",
    }

    def __init__(self, db: Session) -> None:
        self.db = db
        self.project_repository = ProjectRepository(db)
        self.requirement_repository = RequirementRepository(db)

    def create_requirement(self, payload: RequirementCreateRequest) -> RequirementResponse:
        project = self.project_repository.get_by_id(payload.project_id)
        if project is None:
            raise AppException("Project not found", status_code=404)
        data = payload.model_dump(mode="python", exclude_none=True)
        data["constraints_json"] = payload.constraints_json.model_dump(mode="python")
        data["acceptance_criteria_json"] = payload.acceptance_criteria_json.model_dump(mode="python")
        requirement = Requirement(**data)
        self.requirement_repository.create(requirement)
        self.db.commit()
        return self._serialize(requirement)

    def get_requirement(self, requirement_id: str) -> RequirementResponse:
        requirement = self.requirement_repository.get_by_id(requirement_id)
        if requirement is None:
            raise AppException("Requirement not found", status_code=404)
        return self._serialize(requirement)

    def update_requirement(self, requirement_id: str, payload: RequirementUpdateRequest) -> RequirementResponse:
        requirement = self.requirement_repository.get_by_id(requirement_id)
        if requirement is None:
            raise AppException("Requirement not found", status_code=404)
        updates = payload.model_dump(exclude_unset=True)
        material_change_detected = False
        for field_name, value in updates.items():
            if hasattr(value, "value"):
                value = value.value
            elif hasattr(value, "model_dump"):
                value = value.model_dump(mode="python")
            if field_name in self.MATERIAL_FIELDS and getattr(requirement, field_name) != value:
                material_change_detected = True
            setattr(requirement, field_name, value)
        if "version" not in updates and material_change_detected:
            requirement.version = int(requirement.version or 1) + 1
        self.db.commit()
        self.db.refresh(requirement)
        return self._serialize(requirement)

    def _serialize(self, requirement: Requirement) -> RequirementResponse:
        return RequirementResponse(
            id=requirement.id,
            project_id=requirement.project_id,
            title=requirement.title,
            summary=normalize_optional_text(requirement.summary),
            goal=normalize_optional_text(requirement.goal),
            target_users=normalize_optional_text(requirement.target_users),
            input_description=normalize_optional_text(requirement.input_description),
            output_description=normalize_optional_text(requirement.output_description),
            domain=normalize_optional_text(requirement.domain),
            interactivity_level=normalize_interactivity_level(requirement.interactivity_level),
            human_review_required=requirement.human_review_required,
            recurrence_mode=normalize_recurrence_mode(requirement.recurrence_mode),
            constraints_json=normalize_constraints_payload(requirement.constraints_json),
            acceptance_criteria_json=normalize_acceptance_criteria_payload(requirement.acceptance_criteria_json),
            non_goals_json=requirement.non_goals_json or {},
            priority_features_json=requirement.priority_features_json or {},
            external_tools_needed_json=normalize_external_tools_payload(requirement.external_tools_needed_json),
            raw_conversation_json=requirement.raw_conversation_json or {},
            structured_spec_json=requirement.structured_spec_json or {},
            status=requirement.status,
            compiler_status=requirement.compiler_status,
            compiler_notes_json=requirement.compiler_notes_json or {},
            compiler_open_questions_json=requirement.compiler_open_questions_json or {},
            compiler_result_json=requirement.compiler_result_json or {},
            version=requirement.version,
            created_at=_iso(requirement.created_at),
            updated_at=_iso(requirement.updated_at),
        )
