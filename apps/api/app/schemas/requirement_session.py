from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.core.enums import RequirementCompletionState, RequirementMessageRole, RequirementSessionStatus
from app.schemas.capability_registry import (
    CapabilityGapResponse,
    CapabilityGapSummaryResponse,
    CapabilityResolutionRoleViewsResponse,
    CapabilityRequestResponse,
    CapabilityRequestSummaryResponse,
)
from app.schemas.requirement_compiler import RequirementAuthoringFeedbackResponse
from app.schemas.semantic_core import SemanticCore


class RequirementSessionCreateRequest(BaseModel):
    project_id: str


class RequirementMessageCreateRequest(BaseModel):
    content: str = Field(min_length=1)


class RequirementSessionResolutionRequest(BaseModel):
    axis: Literal["source_locator", "source_modality", "deliverable_form", "analysis_frame", "workflow_shape"]
    resolved_value: Any
    resolution_type: Literal[
        "user_provided_explicit_value",
        "user_confirmed_suggested_value",
        "system_inferred_and_confirmed",
    ] = "user_provided_explicit_value"


class RequirementMessageResponse(BaseModel):
    id: str
    role: RequirementMessageRole
    content: str
    created_at: str


class RequirementSessionFinalizeReasonResponse(BaseModel):
    code: str
    severity: Literal["blocking", "warning", "info"]
    source: str
    message: str
    target_field: Optional[str] = None


class RequirementSessionFinalizeStatusResponse(BaseModel):
    can_finalize: bool = False
    completion_state_preview: RequirementCompletionState = RequirementCompletionState.CLARIFICATION_REQUIRED
    summary: str = ""
    blocking_reasons: list[RequirementSessionFinalizeReasonResponse] = Field(default_factory=list)
    warning_reasons: list[RequirementSessionFinalizeReasonResponse] = Field(default_factory=list)
    next_action_keys: list[str] = Field(default_factory=list)


class RequirementDraftStructuredSpecPatchResponse(BaseModel):
    delivery_form: Optional[str] = None
    p0_features: Optional[list[str]] = None
    p1_features: Optional[list[str]] = None
    deployment: Optional[str] = None


class RequirementDraftFieldPatchResponse(BaseModel):
    title: Optional[str] = None
    summary: Optional[str] = None
    goal: Optional[str] = None
    target_users: Optional[str] = None
    input_description: Optional[str] = None
    output_description: Optional[str] = None
    domain: Optional[str] = None
    interactivity_level: Optional[str] = None
    human_review_required: Optional[bool] = None
    recurrence_mode: Optional[str] = None
    constraints_json: Optional[dict[str, Any]] = None
    acceptance_criteria_json: Optional[dict[str, Any]] = None
    external_tools_needed_json: Optional[dict[str, Any]] = None
    structured_spec_json: Optional[RequirementDraftStructuredSpecPatchResponse] = None


class RequirementDraftPatchResponse(BaseModel):
    field_patches: RequirementDraftFieldPatchResponse = Field(default_factory=RequirementDraftFieldPatchResponse)
    cleared_fields: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    ambiguities: list[str] = Field(default_factory=list)
    unresolved_points: list[str] = Field(default_factory=list)
    capability_mentions: list[str] = Field(default_factory=list)
    unsupported_capability_mentions: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    no_op_reason: Optional[str] = None


class RequirementDraftPatchStateResponse(BaseModel):
    applied: bool = False
    confidence: float = 0.0
    applied_fields: list[str] = Field(default_factory=list)
    rejected_fields: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    unresolved_points: list[str] = Field(default_factory=list)
    capability_mentions: list[str] = Field(default_factory=list)
    unsupported_capability_mentions: list[str] = Field(default_factory=list)
    capability_gaps: list[CapabilityGapResponse] = Field(default_factory=list)
    capability_gap_summary: CapabilityGapSummaryResponse = Field(default_factory=CapabilityGapSummaryResponse)
    capability_requests: list[CapabilityRequestResponse] = Field(default_factory=list)
    capability_request_summary: CapabilityRequestSummaryResponse = Field(default_factory=CapabilityRequestSummaryResponse)
    capability_resolution: Optional[CapabilityResolutionRoleViewsResponse] = None
    notes: list[str] = Field(default_factory=list)
    no_op_reason: Optional[str] = None


class RequirementSemanticFieldProvenanceResponse(BaseModel):
    source_turn_index: Optional[int] = None
    source_role: Optional[str] = None
    derivation_method: str = "deterministic_capture"
    confidence: float = 0.0


class RequirementAuthoringSemanticStateResponse(SemanticCore):
    version: str = "1.0"
    goal: Optional[str] = None
    target_users: list[str] = Field(default_factory=list)
    input_items: list[str] = Field(default_factory=list)
    output_items: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    domain: Optional[str] = None
    delivery_form: Optional[str] = None
    p0_features: list[str] = Field(default_factory=list)
    p1_features: list[str] = Field(default_factory=list)
    deployment: Optional[str] = None
    interactivity_level: Optional[str] = None
    recurrence_mode: Optional[str] = None
    external_tools: list[str] = Field(default_factory=list)
    human_review_required: bool = False
    pending_semantic_fields: list[str] = Field(default_factory=list)
    field_provenance: dict[str, RequirementSemanticFieldProvenanceResponse] = Field(default_factory=dict)


class RequirementSessionResponse(BaseModel):
    id: str
    project_id: str
    current_stage: str
    status: RequirementSessionStatus
    raw_user_input: Optional[str] = None
    requirement_draft_json: dict[str, Any] = Field(default_factory=dict)
    markdown_document: Optional[str] = None
    authoring_semantic_state: Optional[RequirementAuthoringSemanticStateResponse] = None
    can_finalize: bool = False
    missing_fields: list[str] = Field(default_factory=list)
    finalize_status: RequirementSessionFinalizeStatusResponse
    draft_patch_state: RequirementDraftPatchStateResponse = Field(default_factory=RequirementDraftPatchStateResponse)
    export_status: str = "not_exported"
    exported_requirement_id: Optional[str] = None
    exported_at: Optional[str] = None
    structured_path_hint: str = ""
    legacy_build_path_removed: bool = True
    authoring_feedback: Optional[RequirementAuthoringFeedbackResponse] = None
    messages: list[RequirementMessageResponse] = Field(default_factory=list)
    created_at: str
    updated_at: str


class RequirementFinalizeResponse(BaseModel):
    session: RequirementSessionResponse
    markdown_document: str
    requirement_draft_json: dict[str, Any]
