from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from app.core.enums import ClarificationAnswerType, ClarificationFreshnessStatus, RequirementCompletionState


class ClarificationOptionResponse(BaseModel):
    value: str
    label: str


class ClarificationItemResponse(BaseModel):
    id: str
    question: str
    rationale: str
    target_field: str
    answer_type: ClarificationAnswerType
    options: list[ClarificationOptionResponse] = Field(default_factory=list)
    answer: Optional[Any] = None
    answered_at: Optional[str] = None
    answered_against_version: Optional[int] = None
    answered_against_content_hash: Optional[str] = None
    is_stale: bool = False
    source: str = "deterministic"
    required: bool = True
    priority: int = 0
    blocking_semantic_axis: Optional[str] = None
    graph_closure_reason: Optional[str] = None


class RequirementClarificationStateResponse(BaseModel):
    requirement_id: str
    completion_state: RequirementCompletionState
    freshness_status: ClarificationFreshnessStatus = ClarificationFreshnessStatus.CURRENT
    current_requirement_version: int = 1
    current_content_hash: str = ""
    stale_item_ids: list[str] = Field(default_factory=list)
    items: list[ClarificationItemResponse] = Field(default_factory=list)
    answered_count: int = 0
    outstanding_count: int = 0
    generated_at: Optional[str] = None
    updated_at: Optional[str] = None
    generation_mode: str = "deterministic"
    last_used_answer_ids: list[str] = Field(default_factory=list)
    last_merged_target_fields: list[str] = Field(default_factory=list)


class ClarificationAnswerSubmissionItem(BaseModel):
    id: str
    answer: Optional[Any] = None


class ClarificationAnswerSubmissionRequest(BaseModel):
    answers: list[ClarificationAnswerSubmissionItem] = Field(default_factory=list)
