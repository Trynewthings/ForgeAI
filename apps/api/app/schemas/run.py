from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from app.core.enums import JobStatus, RunStatus, RunStepStatus


class RunCreateRequest(BaseModel):
    project_id: str
    blueprint_id: str
    executable_id: Optional[str] = None
    runtime_profile_id: Optional[str] = None
    reuse_latest_successful_input: bool = False
    triggered_by: Optional[str] = None
    status: RunStatus = RunStatus.PENDING
    input_snapshot_json: dict[str, Any] = Field(default_factory=dict)
    resolved_config_snapshot_json: dict[str, Any] = Field(default_factory=dict)
    summary: Optional[str] = None
    error_message: Optional[str] = None


class RunReplayRequest(BaseModel):
    input_snapshot_json: dict[str, Any] = Field(default_factory=dict)
    triggered_by: Optional[str] = None


RuntimeOperationState = Literal[
    "idle",
    "running",
    "waiting_for_confirmation",
    "waiting_for_intervention",
    "resumable",
    "resumed",
    "partially_completed",
    "failed_terminal",
    "blocked",
    "completed",
    "cancelled",
]
RuntimeRecoveryAction = Literal["retry_step", "resume_run", "restart_run", "abort_run"]
RuntimeConfirmationDecision = Literal["approve", "decline"]


class RuntimeRecoveryOptionResponse(BaseModel):
    action: RuntimeRecoveryAction
    label: str
    supported: bool = True
    reason: Optional[str] = None


class RuntimeCheckpointResponse(BaseModel):
    checkpoint_id: str
    checkpoint_summary: Optional[str] = None
    last_successful_step: Optional[str] = None
    pending_step: Optional[str] = None
    pending_step_authority_source: Optional[str] = None
    scheduler_kind: Optional[str] = None
    scheduler_authority_source: Optional[str] = None
    routing_source: Optional[str] = None
    routing_metadata: dict[str, Any] = Field(default_factory=dict)
    completed_step_names: list[str] = Field(default_factory=list)
    created_at: Optional[str] = None
    # DAG execution fields (optional, additive)
    pending_steps: list[str] = Field(default_factory=list)
    ready_steps: list[str] = Field(default_factory=list)
    running_steps: list[str] = Field(default_factory=list)
    blocked_step_names: list[str] = Field(default_factory=list)
    failed_step_names: list[str] = Field(default_factory=list)
    execution_round: Optional[int] = None
    graph_execution_kind: Optional[str] = None


class RuntimeConfirmationRequestResponse(BaseModel):
    run_id: str
    step_id: Optional[str] = None
    step_name: Optional[str] = None
    confirmation_required: bool = True
    confirmation_reason: str
    authority_source: Optional[str] = None
    runtime_adapter: Optional[str] = None
    handler_ref: Optional[str] = None
    policy_scope: Optional[str] = None
    requested_at: Optional[str] = None


class RuntimeInterventionStateResponse(BaseModel):
    run_id: str
    step_id: Optional[str] = None
    step_name: Optional[str] = None
    intervention_required: bool = True
    intervention_reason: str
    authority_source: Optional[str] = None
    intervention_legality_authority_source: Optional[str] = None
    blocked_reason: Optional[str] = None
    recovery_options: list[RuntimeRecoveryOptionResponse] = Field(default_factory=list)
    requested_at: Optional[str] = None


BlockedReasonClass = Literal[
    "blocked_by_failed_dependency",
    "blocked_by_human_gate",
]


class RuntimeRecoverySummaryResponse(BaseModel):
    run_id: str
    operation_state: RuntimeOperationState
    last_successful_step: Optional[str] = None
    pending_step: Optional[str] = None
    pending_step_authority_source: Optional[str] = None
    recovery_continuation_step: Optional[str] = None
    recovery_continuation_authority_source: Optional[str] = None
    blocked_reason: Optional[str] = None
    recovery_options: list[RuntimeRecoveryOptionResponse] = Field(default_factory=list)
    updated_at: Optional[str] = None
    # DAG execution fields (optional, additive)
    blocked_step_names: list[str] = Field(default_factory=list)
    retryable_step_names: list[str] = Field(default_factory=list)


class RuntimeStepOperationStateResponse(BaseModel):
    step_name: str
    operation_state: RuntimeOperationState
    scheduler_kind: Optional[str] = None
    scheduler_authority_source: Optional[str] = None
    confirmation_required: bool = False
    confirmation_reason: Optional[str] = None
    authority_source: Optional[str] = None
    intervention_required: bool = False
    intervention_reason: Optional[str] = None
    intervention_legality_authority_source: Optional[str] = None
    resume_supported: bool = False
    rerun_supported: bool = False
    rerun_authority_source: Optional[str] = None
    rerun_requires_human_approval: bool = False
    pending_step_authority_source: Optional[str] = None
    blocked_reason: Optional[str] = None
    blocked_reason_class: Optional[BlockedReasonClass] = None
    checkpoint_id: Optional[str] = None
    recovery_options: list[RuntimeRecoveryOptionResponse] = Field(default_factory=list)


class RuntimeOperationStateResponse(BaseModel):
    run_id: str
    operation_state: RuntimeOperationState = "idle"
    scheduler_kind: Optional[str] = None
    scheduler_authority_source: Optional[str] = None
    confirmation_required: bool = False
    confirmation_request: Optional[RuntimeConfirmationRequestResponse] = None
    intervention_required: bool = False
    intervention_state: Optional[RuntimeInterventionStateResponse] = None
    intervention_legality_authority_source: Optional[str] = None
    resume_supported: bool = False
    rerun_supported: bool = False
    rerun_authority_source: Optional[str] = None
    rerun_requires_human_approval: bool = False
    latest_checkpoint: Optional[RuntimeCheckpointResponse] = None
    recovery_summary: Optional[RuntimeRecoverySummaryResponse] = None
    step_states: list[RuntimeStepOperationStateResponse] = Field(default_factory=list)
    approved_step_names: list[str] = Field(default_factory=list)
    last_successful_step: Optional[str] = None
    pending_step: Optional[str] = None
    pending_step_authority_source: Optional[str] = None
    blocked_reason: Optional[str] = None
    operator_notes: Optional[str] = None
    updated_at: Optional[str] = None


class RunConfirmationActionRequest(BaseModel):
    decision: RuntimeConfirmationDecision
    step_name: Optional[str] = None
    operator_notes: Optional[str] = None
    auto_resume: bool = True


class RunResumeRequest(BaseModel):
    operator_notes: Optional[str] = None


class RunRecoveryRequest(BaseModel):
    action: RuntimeRecoveryAction
    step_name: Optional[str] = None
    operator_notes: Optional[str] = None
    override_input_json: dict[str, Any] = Field(default_factory=dict)


class RunStepResponse(BaseModel):
    id: str
    run_id: str
    step_name: str
    step_order: int
    node_name: Optional[str] = None
    status: RunStepStatus
    input_payload_json: dict[str, Any] = Field(default_factory=dict)
    output_payload_json: dict[str, Any] = Field(default_factory=dict)
    log_excerpt: Optional[str] = None
    token_usage_json: dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    operation_state: Optional[RuntimeStepOperationStateResponse] = None
    created_at: str
    updated_at: str


class RunResponse(BaseModel):
    id: str
    project_id: str
    blueprint_id: str
    executable_id: Optional[str] = None
    runtime_profile_id: Optional[str] = None
    active_job_id: Optional[str] = None
    active_job_status: Optional[JobStatus] = None
    triggered_by: Optional[str] = None
    status: RunStatus
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    input_snapshot_json: dict[str, Any] = Field(default_factory=dict)
    resolved_config_snapshot_json: dict[str, Any] = Field(default_factory=dict)
    runtime_operation: RuntimeOperationStateResponse = Field(default_factory=lambda: RuntimeOperationStateResponse(run_id=""))
    summary: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str
    updated_at: str
    run_steps: list[RunStepResponse] = Field(default_factory=list)
