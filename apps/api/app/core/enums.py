from __future__ import annotations

from enum import Enum


class ProjectStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class RequirementSessionStage(str, Enum):
    DISCOVERY = "discovery"
    REFINEMENT = "refinement"
    READY = "ready"
    FINALIZED = "finalized"


class RequirementSessionStatus(str, Enum):
    ACTIVE = "active"
    READY = "ready"
    FINALIZED = "finalized"
    ARCHIVED = "archived"


class RequirementStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    APPROVED = "approved"
    ARCHIVED = "archived"


class RequirementCompilerStatus(str, Enum):
    IDLE = "idle"
    NEEDS_INPUT = "needs_input"
    COMPILED = "compiled"
    FAILED = "failed"


class RequirementCompilerMode(str, Enum):
    DETERMINISTIC = "deterministic"
    LLM_ASSISTED = "llm_assisted"


class RequirementCompletionState(str, Enum):
    READY_TO_COMPILE = "ready_to_compile"
    COMPILABLE_WITH_WARNINGS = "compilable_with_warnings"
    CLARIFICATION_REQUIRED = "clarification_required"


class ClarificationAnswerType(str, Enum):
    TEXT = "text"
    LONG_TEXT = "long_text"
    BOOLEAN = "boolean"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"
    LIST = "list"


class ClarificationFreshnessStatus(str, Enum):
    CURRENT = "current"
    STALE = "stale"
    PARTIALLY_STALE = "partially_stale"


class DraftReadinessStatus(str, Enum):
    DRAFT_ONLY = "draft_only"
    BLOCKED_BY_CLARIFICATION = "blocked_by_clarification"
    COMPILABLE_WITH_WARNINGS = "compilable_with_warnings"
    READY_FOR_BUILD = "ready_for_build"


class ConstraintType(str, Enum):
    POLICY = "policy"
    SECURITY = "security"
    COMPLIANCE = "compliance"
    RESOURCE = "resource"
    INTEGRATION = "integration"
    PERFORMANCE = "performance"
    FORMAT = "format"
    BUSINESS = "business"
    OTHER = "other"
    LEGACY = "legacy"
    CONTENT_PRESERVATION = "content_preservation"
    CONTENT_ADDITION = "content_addition"
    STYLE_PRESERVATION = "style_preservation"
    INPUT_INTEGRITY = "input_integrity"
    CALCULATION_SCOPE = "calculation_scope"
    SOURCE_QUALITY = "source_quality"
    SCHEDULE = "schedule"
    TONE_POLICY = "tone_policy"
    AUTOMATION_BOUNDARY = "automation_boundary"
    PERSPECTIVE_SEPARATION = "perspective_separation"
    INPUT_SCOPE = "input_scope"


class ConstraintLevel(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class AcceptanceType(str, Enum):
    FUNCTIONAL = "functional"
    QUALITY = "quality"
    PERFORMANCE = "performance"
    SECURITY = "security"
    REVIEW = "review"
    ARTIFACT = "artifact"
    BUSINESS = "business"
    OTHER = "other"
    LEGACY = "legacy"
    FORMAT = "format"
    SEMANTIC = "semantic"
    COMPLETENESS = "completeness"
    HUMAN_REVIEW = "human_review"
    POLICY = "policy"


class AcceptanceLevel(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class RequirementMessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class BuildTaskStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class BuildStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ModelProviderType(str, Enum):
    OPENAI = "openai"
    OPENAI_COMPATIBLE = "openai-compatible"
    LOCAL_ENDPOINT = "local-endpoint"


class BlueprintType(str, Enum):
    SINGLE_SERVICE = "single_service"
    WORKFLOW = "workflow"
    MULTI_AGENT = "multi_agent"


class BlueprintStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    ARCHIVED = "archived"


class ExecutableType(str, Enum):
    REPO = "repo"
    WORKFLOW_BUNDLE = "workflow_bundle"
    AGENT_GRAPH = "agent_graph"


class ExecutableStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    ARCHIVED = "archived"


class BuildStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    # API-only projection. The DB never persists this — the run row stays
    # RUNNING when the runtime is blocked on an operator decision. The
    # serializer derives this value from runtime_operation.operation_state
    # so the frontend can branch on a single status field.
    WAITING_FOR_INTERVENTION = "waiting_for_intervention"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class JobType(str, Enum):
    BUILD = "build"
    RUN = "run"


class JobStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_BUILD_STATUSES = {BuildTaskStatus.COMPLETED.value, BuildTaskStatus.FAILED.value}
TERMINAL_V2_BUILD_STATUSES = {BuildStatus.COMPLETED.value, BuildStatus.FAILED.value}
