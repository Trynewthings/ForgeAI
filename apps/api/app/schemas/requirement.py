from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from app.core.enums import RequirementCompilerStatus, RequirementStatus
from app.schemas.requirement_payloads import (
    AcceptanceCriteriaPayload,
    ConstraintsPayload,
    empty_acceptance_criteria_payload,
    empty_constraints_payload,
    normalize_external_tools_payload,
    normalize_interactivity_level,
    normalize_optional_text,
    normalize_recurrence_mode,
)


class RequirementCreateRequest(BaseModel):
    project_id: str
    title: str = Field(min_length=1, max_length=255)
    summary: Optional[str] = None
    goal: Optional[str] = None
    target_users: Optional[str] = None
    input_description: Optional[str] = None
    output_description: Optional[str] = None
    domain: Optional[str] = None
    interactivity_level: Optional[str] = None
    human_review_required: bool = False
    recurrence_mode: Optional[str] = None
    constraints_json: ConstraintsPayload = Field(default_factory=empty_constraints_payload)
    acceptance_criteria_json: AcceptanceCriteriaPayload = Field(default_factory=empty_acceptance_criteria_payload)
    non_goals_json: dict[str, Any] = Field(default_factory=dict)
    priority_features_json: dict[str, Any] = Field(default_factory=dict)
    external_tools_needed_json: dict[str, Any] = Field(default_factory=dict)
    raw_conversation_json: dict[str, Any] = Field(default_factory=dict)
    structured_spec_json: dict[str, Any] = Field(default_factory=dict)
    status: RequirementStatus = RequirementStatus.DRAFT
    compiler_status: RequirementCompilerStatus = RequirementCompilerStatus.IDLE
    compiler_notes_json: dict[str, Any] = Field(default_factory=dict)
    compiler_open_questions_json: dict[str, Any] = Field(default_factory=dict)
    compiler_result_json: dict[str, Any] = Field(default_factory=dict)
    version: int = 1

    @field_validator("title", mode="before")
    @classmethod
    def _normalize_title(cls, value: Any) -> Any:
        if value is None:
            return value
        text = str(value).strip()
        if not text:
            raise ValueError("title cannot be blank")
        return text

    @field_validator(
        "summary",
        "goal",
        "target_users",
        "input_description",
        "output_description",
        "domain",
        mode="before",
    )
    @classmethod
    def _normalize_optional_text_fields(cls, value: Any) -> Any:
        return normalize_optional_text(value)

    @field_validator("interactivity_level", mode="before")
    @classmethod
    def _normalize_interactivity_level(cls, value: Any) -> Any:
        return normalize_interactivity_level(value)

    @field_validator("recurrence_mode", mode="before")
    @classmethod
    def _normalize_recurrence_mode(cls, value: Any) -> Any:
        return normalize_recurrence_mode(value)

    @field_validator("external_tools_needed_json", mode="before")
    @classmethod
    def _normalize_external_tools_needed_json(cls, value: Any) -> Any:
        return normalize_external_tools_payload(value)


class RequirementUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    summary: Optional[str] = None
    goal: Optional[str] = None
    target_users: Optional[str] = None
    input_description: Optional[str] = None
    output_description: Optional[str] = None
    domain: Optional[str] = None
    interactivity_level: Optional[str] = None
    human_review_required: Optional[bool] = None
    recurrence_mode: Optional[str] = None
    constraints_json: Optional[ConstraintsPayload] = None
    acceptance_criteria_json: Optional[AcceptanceCriteriaPayload] = None
    non_goals_json: Optional[dict[str, Any]] = None
    priority_features_json: Optional[dict[str, Any]] = None
    external_tools_needed_json: Optional[dict[str, Any]] = None
    raw_conversation_json: Optional[dict[str, Any]] = None
    structured_spec_json: Optional[dict[str, Any]] = None
    status: Optional[RequirementStatus] = None
    compiler_status: Optional[RequirementCompilerStatus] = None
    compiler_notes_json: Optional[dict[str, Any]] = None
    compiler_open_questions_json: Optional[dict[str, Any]] = None
    compiler_result_json: Optional[dict[str, Any]] = None
    version: Optional[int] = None

    @field_validator("title", mode="before")
    @classmethod
    def _normalize_title(cls, value: Any) -> Any:
        if value is None:
            return value
        text = str(value).strip()
        if not text:
            raise ValueError("title cannot be blank")
        return text

    @field_validator(
        "summary",
        "goal",
        "target_users",
        "input_description",
        "output_description",
        "domain",
        mode="before",
    )
    @classmethod
    def _normalize_optional_text_fields(cls, value: Any) -> Any:
        return normalize_optional_text(value)

    @field_validator("interactivity_level", mode="before")
    @classmethod
    def _normalize_interactivity_level(cls, value: Any) -> Any:
        return normalize_interactivity_level(value)

    @field_validator("recurrence_mode", mode="before")
    @classmethod
    def _normalize_recurrence_mode(cls, value: Any) -> Any:
        return normalize_recurrence_mode(value)

    @field_validator("external_tools_needed_json", mode="before")
    @classmethod
    def _normalize_external_tools_needed_json(cls, value: Any) -> Any:
        return normalize_external_tools_payload(value)


class RequirementResponse(BaseModel):
    id: str
    project_id: str
    title: str
    summary: Optional[str] = None
    goal: Optional[str] = None
    target_users: Optional[str] = None
    input_description: Optional[str] = None
    output_description: Optional[str] = None
    domain: Optional[str] = None
    interactivity_level: Optional[str] = None
    human_review_required: bool = False
    recurrence_mode: Optional[str] = None
    constraints_json: ConstraintsPayload = Field(default_factory=empty_constraints_payload)
    acceptance_criteria_json: AcceptanceCriteriaPayload = Field(default_factory=empty_acceptance_criteria_payload)
    non_goals_json: dict[str, Any] = Field(default_factory=dict)
    priority_features_json: dict[str, Any] = Field(default_factory=dict)
    external_tools_needed_json: dict[str, Any] = Field(default_factory=dict)
    raw_conversation_json: dict[str, Any] = Field(default_factory=dict)
    structured_spec_json: dict[str, Any] = Field(default_factory=dict)
    status: RequirementStatus
    compiler_status: RequirementCompilerStatus = RequirementCompilerStatus.IDLE
    compiler_notes_json: dict[str, Any] = Field(default_factory=dict)
    compiler_open_questions_json: dict[str, Any] = Field(default_factory=dict)
    compiler_result_json: dict[str, Any] = Field(default_factory=dict)
    version: int
    created_at: str
    updated_at: str
