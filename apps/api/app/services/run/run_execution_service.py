from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from threading import Thread
from typing import Any, Callable, Optional

from app.core.config import ROOT_DIR, settings
from app.core.database import SessionLocal
from app.core.enums import RunStatus, RunStepStatus
from app.core.logging import log_event
from app.db.models import Run, RunStep
from app.db.models.entities import Artifact, TokenUsageLog
from app.db.repositories.blueprint_repository import BlueprintRepository
from app.db.repositories.executable_repository import ExecutableRepository
from app.db.repositories.run_repository import RunRepository
from app.db.repositories.run_step_repository import RunStepRepository
from app.db.repositories.runtime_profile_repository import RuntimeProfileRepository
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.model_config_repository import ModelConfigRepository
from app.repositories.token_usage_log_repository import TokenUsageLogRepository
from app.schemas.executable_manifest import (
    ParsedManifest,
    ResolvedExecutionPlan,
    RuntimeInputValidationResult,
    RuntimePreflightResult,
)
from app.schemas.run import RuntimeRecoveryOptionResponse
from app.services.executable_manifest.parser import ExecutableManifestParser
from app.services.executable_manifest.resolver import ExecutableManifestResolver
from app.services.executable_manifest.validator import ExecutableManifestValidator
from app.services.run.case_workloads import (
    build_artifact_outputs,
    execute_browser_workload,
    execute_fetch_url_workload,
    execute_filesystem_workload,
    execute_python_workload,
    execute_terminal_workload,
    execute_template_runtime_workload,
    execute_web_search_workload,
)
from app.services.run.scheduler import (
    CompatibilityStepScheduler,
    SchedulerCurrentStepTracker,
    SchedulerExecutionContext,
    WorkflowGraphScheduler,
)
from app.services.provider_runtime import ProviderCallError, ProviderResponse, get_provider_adapter

logger = logging.getLogger(__name__)


class RunExecutionService:
    def __init__(self) -> None:
        self.delay_seconds = settings.build_step_delay_seconds
        self.provider_max_attempts = 2
        self.manifest_parser = ExecutableManifestParser()
        self.manifest_validator = ExecutableManifestValidator()
        self.manifest_resolver = ExecutableManifestResolver()

    def dispatch(self, run_id: str) -> str:
        thread = Thread(target=self.execute_run, args=(run_id,), daemon=True)
        thread.start()
        log_event(logger, logging.INFO, "run.dispatch.sent_to_thread", run_id=run_id)
        return "thread"

    def execute_run(self, run_id: str) -> None:
        db = SessionLocal()
        run_repository = RunRepository(db)
        run_step_repository = RunStepRepository(db)
        blueprint_repository = BlueprintRepository(db)
        executable_repository = ExecutableRepository(db)
        runtime_profile_repository = RuntimeProfileRepository(db)
        artifact_repository = ArtifactRepository(db)
        token_usage_log_repository = TokenUsageLogRepository(db)
        model_config_repository = ModelConfigRepository(db)

        current_step: Optional[RunStep] = None
        run: Optional[Run] = None
        runtime_profile = None
        executable = None
        parsed_manifest: Optional[ParsedManifest] = None
        resolved_plan: Optional[ResolvedExecutionPlan] = None
        input_validation = RuntimeInputValidationResult(valid=True, contract_mode="manifest_input_schema", schema_kind="none")
        preflight_result = RuntimePreflightResult(valid=True, execution_mode="executable-manifest")
        execution_mode = "executable-manifest"
        compatibility_mode = "native"
        failure_class: Optional[str] = None
        failure_stage: Optional[str] = None
        input_reuse: dict[str, Any] = {}
        runtime_operation: dict[str, Any] = {}
        runtime_scheduler: dict[str, Any] = {}
        requested_action = "start"
        requested_step_name: str | None = None
        approved_step_names: set[str] = set()
        scheduler_tracker = SchedulerCurrentStepTracker()
        try:
            run = run_repository.get_by_id(run_id, with_relations=True)
            if run is None:
                raise RuntimeError("Run not found")
            runtime_operation = self._get_runtime_operation_snapshot(run)
            requested_action = str(runtime_operation.get("requested_action") or "start")
            requested_step_name = runtime_operation.get("requested_step_name")
            approved_step_names = {str(item) for item in runtime_operation.get("approved_step_names", []) if str(item)}
            blueprint = blueprint_repository.get_by_id(run.blueprint_id)
            if blueprint is None:
                raise RuntimeError("Blueprint not found")
            executable = executable_repository.get_by_id(run.executable_id) if run.executable_id else None
            runtime_profile = (
                runtime_profile_repository.get_by_id(run.runtime_profile_id) if run.runtime_profile_id else None
            )

            if run.status == RunStatus.CANCELLED.value:
                return

            input_reuse = dict((run.resolved_config_snapshot_json or {}).get("input_reuse") or {})
            reused_input = input_reuse.get("reused_input", {}) if input_reuse.get("used") else {}

            if executable is None:
                raise RuntimeError("Executable is required for runtime execution.")
            execution_mode = "executable-manifest"
            compatibility_mode = "native"
            parsed_manifest = self.manifest_parser.parse(executable)
            validation = self.manifest_validator.validate(parsed_manifest)
            if not validation.valid:
                failure_class = "manifest_invalid"
                failure_stage = "preflight"
                preflight_result = RuntimePreflightResult(
                    valid=False,
                    execution_mode=execution_mode,
                    manifest_valid=False,
                    input_valid=True,
                    env_valid=True,
                    config_valid=True,
                    failure_class=failure_class,
                    failure_stage=failure_stage,
                    failure_detail=f"manifest validation failed: {'; '.join(validation.errors)}",
                    errors=list(validation.errors),
                    warnings=list(validation.warnings),
                    input_validation=input_validation,
                )
                run.resolved_config_snapshot_json = self._build_resolved_config_snapshot(
                    run=run,
                    runtime_profile=runtime_profile,
                    executable=executable,
                    parsed_manifest=parsed_manifest,
                resolved_plan=None,
                execution_mode=execution_mode,
                compatibility_mode=compatibility_mode,
                input_reuse=input_reuse,
                preflight_result=preflight_result,
                input_validation=input_validation,
                runtime_operation=runtime_operation,
            )
                self._persist_run_artifact(
                    artifact_repository=artifact_repository,
                    run=run,
                    executable_id=run.executable_id,
                    artifact_type="runtime_preflight",
                    content=preflight_result.model_dump_json(indent=2),
                    metadata={"execution_mode": execution_mode, "valid": False},
                )
                db.commit()
                raise RuntimeError(preflight_result.failure_detail or "manifest validation failed")
            resolved_plan = self.manifest_resolver.resolve(
                executable=executable,
                manifest=parsed_manifest,
                runtime_profile=runtime_profile,
                run_input=run.input_snapshot_json or {},
                reused_input=reused_input if isinstance(reused_input, dict) else {},
                input_reuse=input_reuse,
            )
            execution_mode = resolved_plan.execution_mode
            compatibility_mode = resolved_plan.compatibility_mode
            input_validation = resolved_plan.input_validation
            preflight_result = resolved_plan.preflight
            input_reuse = resolved_plan.input_reuse
            run.resolved_config_snapshot_json = self._build_resolved_config_snapshot(
                run=run,
                runtime_profile=runtime_profile,
                executable=executable,
                parsed_manifest=parsed_manifest,
                resolved_plan=resolved_plan,
                execution_mode=execution_mode,
                compatibility_mode=compatibility_mode,
                input_reuse=input_reuse,
                preflight_result=preflight_result,
                input_validation=input_validation,
                runtime_operation=runtime_operation,
            )
            self._persist_run_artifact(
                artifact_repository=artifact_repository,
                run=run,
                executable_id=run.executable_id,
                artifact_type="runtime_preflight",
                content=preflight_result.model_dump_json(indent=2),
                metadata={"execution_mode": execution_mode, "valid": preflight_result.valid},
            )
            if not preflight_result.valid:
                failure_class = preflight_result.failure_class
                failure_stage = preflight_result.failure_stage
                if self._preflight_requires_intervention(preflight_result):
                    runtime_operation = self._build_waiting_for_intervention_state(
                        run=run,
                        step_name=None,
                        blocked_reason=preflight_result.failure_detail or "; ".join(preflight_result.errors),
                        intervention_reason="Execution package requires operator intervention before runtime can continue.",
                        last_successful_step=None,
                        pending_step=None,
                        authority_source="runtime.compatibility_preflight_blocked",
                    )
                    run.status = RunStatus.RUNNING.value
                    run.summary = "Run is waiting for intervention before execution can begin."
                    run.error_message = None
                    run.resolved_config_snapshot_json = self._build_resolved_config_snapshot(
                        run=run,
                        runtime_profile=runtime_profile,
                        executable=executable,
                        parsed_manifest=parsed_manifest,
                        resolved_plan=resolved_plan,
                        execution_mode=execution_mode,
                        compatibility_mode=compatibility_mode,
                        input_reuse=input_reuse,
                        preflight_result=preflight_result,
                        input_validation=input_validation,
                        runtime_operation=runtime_operation,
                    )
                    self._persist_run_artifact(
                        artifact_repository=artifact_repository,
                        run=run,
                        executable_id=run.executable_id,
                        artifact_type="runtime_intervention_state",
                        content=json.dumps(runtime_operation, ensure_ascii=False, indent=2),
                        metadata={"execution_mode": execution_mode, "operation_state": "waiting_for_intervention"},
                    )
                    db.commit()
                    return
                db.commit()
                raise RuntimeError(preflight_result.failure_detail or "; ".join(preflight_result.errors))
            self._replace_run_steps_from_plan(
                run_step_repository,
                run,
                resolved_plan,
                preserve_existing=requested_action in {"resume_run", "retry_step"},
            )
            artifact_repository.create(
                Artifact(
                    build_task_id=None,
                    build_step_id=None,
                    build_id=None,
                    build_step_v2_id=None,
                    run_id=run.id,
                    run_step_id=None,
                    executable_id=run.executable_id,
                    artifact_type="resolved_execution_plan",
                    storage_kind="inline",
                    path_or_content=str(resolved_plan.model_dump_json(indent=2)),
                    metadata_json={"execution_mode": execution_mode},
                )
            )
            if parsed_manifest is not None:
                artifact_repository.create(
                    Artifact(
                        build_task_id=None,
                        build_step_id=None,
                        build_id=None,
                        build_step_v2_id=None,
                        run_id=run.id,
                        run_step_id=None,
                        executable_id=run.executable_id,
                        artifact_type="resolved_manifest",
                        storage_kind="inline",
                        path_or_content=str(parsed_manifest.model_dump_json(indent=2)),
                        metadata_json={"execution_mode": execution_mode},
                    )
                )

            db.commit()
            db.refresh(run)
            if run.status == RunStatus.CANCELLED.value:
                log_event(
                    logger,
                    logging.INFO,
                    "run.cancelled.before_execution",
                    run_id=run.id,
                    execution_mode=execution_mode,
                    compatibility_mode=compatibility_mode,
                )
                return

            run.status = RunStatus.RUNNING.value
            run.started_at = run.started_at or datetime.now(timezone.utc)
            run.error_message = None
            runtime_operation = self._apply_runtime_operation(
                run=run,
                patch={
                    "operation_state": "running",
                    "scheduler_kind": runtime_scheduler.get("active_scheduler"),
                    "scheduler_authority_source": runtime_scheduler.get("active_scheduler_authority_source"),
                    "confirmation_required": False,
                    "confirmation_request": None,
                    "intervention_required": False,
                    "intervention_state": None,
                    "blocked_reason": None,
                    "pending_step_authority_source": runtime_scheduler.get("pending_step_authority_source"),
                    "updated_at": self._timestamp(),
                },
            )
            db.commit()
            log_event(
                logger,
                logging.INFO,
                "run.started",
                run_id=run.id,
                blueprint_id=run.blueprint_id,
                executable_id=run.executable_id,
                runtime_profile_id=run.runtime_profile_id,
                execution_mode=execution_mode,
                compatibility_mode=compatibility_mode,
                llm_mode=settings.llm_mode,
            )

            steps = run_step_repository.list_by_run(run.id)
            self._prepare_steps_for_requested_action(
                run=run,
                steps=steps,
                requested_action=requested_action,
                requested_step_name=requested_step_name,
                resolved_plan=resolved_plan,
            )
            db.commit()
            steps = run_step_repository.list_by_run(run.id)
            runtime_scheduler = self._select_runtime_scheduler(resolved_plan=resolved_plan)
            snapshot = dict(run.resolved_config_snapshot_json or {})
            # Store JSON-safe subset (exclude non-serializable _dag_read_model)
            snapshot["runtime_scheduler"] = {k: v for k, v in runtime_scheduler.items() if not k.startswith("_")}
            # Store workflow_artifact early so DAG recovery can rebuild the graph
            # even when the scheduler fails mid-execution.
            if resolved_plan is not None and isinstance(resolved_plan.workflow_artifact, dict) and resolved_plan.workflow_artifact:
                snapshot["workflow_artifact"] = resolved_plan.workflow_artifact
            run.resolved_config_snapshot_json = snapshot
            db.commit()

            if runtime_scheduler.get("active_scheduler") == WorkflowGraphScheduler.scheduler_kind:
                scheduler = WorkflowGraphScheduler()
            else:
                scheduler = CompatibilityStepScheduler()

            scheduler_result = scheduler.run(
                service=self,
                context=SchedulerExecutionContext(
                    db=db,
                    run=run,
                    steps=steps,
                    blueprint_type=blueprint.blueprint_type,
                    runtime_profile=runtime_profile,
                    resolved_plan=resolved_plan,
                    execution_mode=execution_mode,
                approved_step_names=approved_step_names,
                artifact_repository=artifact_repository,
                token_usage_log_repository=token_usage_log_repository,
                model_config_repository=model_config_repository,
                run_step_repository=run_step_repository,
                scheduler_kind=str(runtime_scheduler.get("active_scheduler") or CompatibilityStepScheduler.scheduler_kind),
                scheduler_authority_source=str(
                    runtime_scheduler.get("active_scheduler_authority_source")
                    or "resolved_steps.compatibility_sequence"
                ),
                pending_step_authority_source=str(
                    runtime_scheduler.get("pending_step_authority_source")
                    or "resolved_steps.compatibility_sequence"
                ),
                ),
                tracker=scheduler_tracker,
                logger=logger,
            )
            current_step = scheduler_tracker.current_step
            approved_step_names = set(scheduler_result.approved_step_names)
            if scheduler_result.terminal_state != "completed":
                return
            accumulated_outputs = list(scheduler_result.accumulated_outputs)

            summary_text = self._build_run_summary(blueprint.name, accumulated_outputs, execution_mode)
            artifact_repository.create(
                Artifact(
                    build_task_id=None,
                    build_step_id=None,
                    build_id=None,
                    build_step_v2_id=None,
                    run_id=run.id,
                    run_step_id=None,
                    executable_id=run.executable_id,
                    artifact_type="run_summary",
                    storage_kind="inline",
                    path_or_content=summary_text,
                    metadata_json={"kind": "summary", "execution_mode": execution_mode},
                )
            )
            run.summary = summary_text
            run.status = RunStatus.COMPLETED.value
            run.ended_at = datetime.now(timezone.utc)
            self._apply_runtime_operation(
                run=run,
                patch={
                    "operation_state": "completed",
                    "scheduler_kind": runtime_scheduler.get("active_scheduler"),
                    "scheduler_authority_source": runtime_scheduler.get("active_scheduler_authority_source"),
                    "pending_step": None,
                    "pending_step_authority_source": runtime_scheduler.get("pending_step_authority_source"),
                    "confirmation_required": False,
                    "intervention_required": False,
                    "blocked_reason": None,
                    "updated_at": self._timestamp(),
                },
            )
            db.commit()
            log_event(logger, logging.INFO, "run.completed", run_id=run.id, execution_mode=execution_mode)
        except Exception as exc:
            db.rollback()
            if current_step is None and scheduler_tracker.current_step is not None:
                current_step = scheduler_tracker.current_step
            resolved_failure_class, resolved_failure_stage = self._classify_failure(
                error=exc,
                failure_class=failure_class,
                failure_stage=failure_stage,
                current_step=current_step,
            )
            if run is not None:
                persisted_steps = run_step_repository.list_by_run(run.id)
                completed_step_names = [item.step_name for item in persisted_steps if item.status == RunStepStatus.COMPLETED.value]
                last_successful_step = completed_step_names[-1] if completed_step_names else None
                pending_step = current_step.step_name if current_step is not None else None
                recovery_continuation_step: str | None = None
                recovery_continuation_authority: str = "resolved_steps.compatibility_sequence"
                if pending_step is not None:
                    recovery_continuation_step, recovery_continuation_authority = self._graph_aware_next_pending_step(
                        run=run,
                        current_step_name=pending_step,
                        fallback_steps=persisted_steps,
                    )
                failure_snapshot = dict(run.resolved_config_snapshot_json or {})
                runtime_operation = self._get_runtime_operation_snapshot(run)
                policy_bundle = self._resolve_runtime_policy_bundle(
                    resolved_plan=resolved_plan,
                    current_step=current_step,
                )
                escalation_to_human_gate = bool(policy_bundle["failure_escalation_to_human_gate"])
                escalation_authority_source = str(policy_bundle["failure_escalation_authority_source"])
                intervention_policy = dict(policy_bundle["intervention_policy"])
                intervention_legality_authority_source = str(policy_bundle["intervention_authority_source"])
                rerun_supported = bool(policy_bundle["rerun_allowed"])
                rerun_requires_human_approval = bool(policy_bundle["rerun_requires_human_approval"])
                rerun_authority_source = str(policy_bundle["rerun_authority_source"])
                resume_supported = self._intervention_resume_supported(intervention_policy) and (
                    last_successful_step is not None or escalation_to_human_gate
                )
                rerun_supported = rerun_supported and self._intervention_mode_allowed(intervention_policy, "rerun")
                recovery_options = self._recovery_options(
                    restart_only=current_step is None,
                    include_resume=resume_supported,
                    include_rerun=rerun_supported,
                )
                operation_state = "waiting_for_intervention" if escalation_to_human_gate else "failed_terminal"
                runtime_operation.update(
                    {
                        "operation_state": operation_state,
                        "scheduler_kind": runtime_scheduler.get("active_scheduler"),
                        "scheduler_authority_source": runtime_scheduler.get("active_scheduler_authority_source"),
                        "intervention_required": escalation_to_human_gate,
                        "intervention_state": (
                            {
                                "run_id": run.id,
                                "step_id": current_step.id if current_step is not None else None,
                                "step_name": current_step.step_name if current_step is not None else None,
                                "intervention_required": escalation_to_human_gate,
                                "intervention_reason": "Runtime execution stopped and requires operator action.",
                                "authority_source": escalation_authority_source,
                                "intervention_legality_authority_source": intervention_legality_authority_source,
                                "blocked_reason": str(exc),
                                "recovery_options": recovery_options,
                                "requested_at": self._timestamp(),
                            }
                            if escalation_to_human_gate and current_step is not None
                            else None
                        ),
                        "intervention_legality_authority_source": intervention_legality_authority_source,
                        "resume_supported": resume_supported,
                        "rerun_supported": rerun_supported,
                        "rerun_authority_source": rerun_authority_source,
                        "rerun_requires_human_approval": rerun_requires_human_approval,
                        "last_successful_step": last_successful_step,
                        "pending_step": pending_step,
                        "pending_step_authority_source": runtime_scheduler.get("pending_step_authority_source"),
                        "blocked_reason": str(exc),
                        "recovery_summary": {
                            "run_id": run.id,
                            "operation_state": operation_state,
                            "last_successful_step": last_successful_step,
                            "pending_step": pending_step,
                            "pending_step_authority_source": runtime_scheduler.get("pending_step_authority_source"),
                            "recovery_continuation_step": recovery_continuation_step,
                            "recovery_continuation_authority_source": recovery_continuation_authority,
                            "blocked_reason": str(exc),
                            "recovery_options": recovery_options,
                            "updated_at": self._timestamp(),
                        },
                        "updated_at": self._timestamp(),
                    }
                )
                failure_snapshot.update(
                    {
                        "execution_mode": execution_mode,
                        "compatibility_mode": compatibility_mode,
                        "executable_id": run.executable_id,
                        "resolved_input": failure_snapshot.get("resolved_input", run.input_snapshot_json or {}),
                        "resolved_env": failure_snapshot.get("resolved_env", {}),
                        "resolved_steps": failure_snapshot.get("resolved_steps", []),
                        "resolved_artifacts_spec": failure_snapshot.get("resolved_artifacts_spec", {}),
                        "resolved_observability": failure_snapshot.get("resolved_observability", {}),
                        "input_reuse": failure_snapshot.get("input_reuse", input_reuse),
                        "input_validation": failure_snapshot.get("input_validation", input_validation.model_dump(mode="python")),
                        "preflight": failure_snapshot.get("preflight", preflight_result.model_dump(mode="python")),
                        "failure_class": resolved_failure_class,
                        "failure_stage": resolved_failure_stage,
                        "failure_detail": str(exc),
                        "runtime_operation": runtime_operation,
                    }
                )
                run.status = RunStatus.FAILED.value
                run.error_message = str(exc)
                run.ended_at = datetime.now(timezone.utc)
                run.resolved_config_snapshot_json = failure_snapshot
            if current_step is not None:
                current_step.status = RunStepStatus.FAILED.value
                current_step.finished_at = datetime.now(timezone.utc)
                current_step.log_excerpt = (current_step.log_excerpt or "") + f"\n[failed] {exc}"
                current_step.output_payload_json = {
                    "step_name": current_step.step_name,
                    "status": "failed",
                    "fallback_used": False,
                    "failure_class": resolved_failure_class,
                    "failure_stage": resolved_failure_stage,
                    "failure_detail": str(exc),
                    "retry_attempts": 0,
                    "intervention_required": escalation_to_human_gate,
                    "authority_source": escalation_authority_source,
                    "intervention_legality_authority_source": intervention_legality_authority_source,
                    "resume_supported": resume_supported,
                    "rerun_supported": rerun_supported,
                    "rerun_authority_source": rerun_authority_source,
                    "rerun_requires_human_approval": rerun_requires_human_approval,
                    "recovery_options": recovery_options,
                    "operation_state": {
                        "step_name": current_step.step_name,
                        "operation_state": operation_state,
                        "scheduler_kind": runtime_scheduler.get("active_scheduler"),
                        "scheduler_authority_source": runtime_scheduler.get("active_scheduler_authority_source"),
                        "intervention_required": escalation_to_human_gate,
                        "intervention_reason": "Runtime execution stopped and requires operator action.",
                        "authority_source": escalation_authority_source,
                        "intervention_legality_authority_source": intervention_legality_authority_source,
                        "resume_supported": resume_supported,
                        "rerun_supported": rerun_supported,
                        "rerun_authority_source": rerun_authority_source,
                        "rerun_requires_human_approval": rerun_requires_human_approval,
                        "pending_step_authority_source": runtime_scheduler.get("pending_step_authority_source"),
                        "blocked_reason": str(exc),
                        "recovery_options": recovery_options,
                    },
                }
            if run is not None:
                self._persist_run_artifact(
                    artifact_repository=artifact_repository,
                    run=run,
                    executable_id=run.executable_id,
                    artifact_type="runtime_failure_report",
                    content=json.dumps(
                        {
                            "run_id": run.id,
                            "execution_mode": execution_mode,
                            "compatibility_mode": compatibility_mode,
                            "failure_class": resolved_failure_class,
                            "failure_stage": resolved_failure_stage,
                            "failure_detail": str(exc),
                            "preflight": preflight_result.model_dump(mode="python"),
                            "input_validation": input_validation.model_dump(mode="python"),
                            "runtime_operation": runtime_operation if run is not None else {},
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    metadata={
                        "execution_mode": execution_mode,
                        "failure_class": resolved_failure_class,
                        "failure_stage": resolved_failure_stage,
                    },
                    run_step_id=current_step.id if current_step is not None else None,
                )
                if current_step is not None:
                    self._persist_run_artifact(
                        artifact_repository=artifact_repository,
                        run=run,
                        executable_id=run.executable_id,
                        artifact_type="runtime_intervention_state",
                        content=json.dumps(runtime_operation, ensure_ascii=False, indent=2),
                        metadata={
                            "execution_mode": execution_mode,
                            "operation_state": operation_state,
                            "failure_class": resolved_failure_class,
                            "authority_source": escalation_authority_source,
                        },
                        run_step_id=current_step.id,
                    )
            db.commit()
            log_event(logger, logging.ERROR, "run.failed", run_id=run_id, error=str(exc))
        finally:
            db.close()

    def _execute_step(
        self,
        *,
        run: Run,
        blueprint_type: str,
        step: RunStep,
        runtime_profile,
        model_config_repository: ModelConfigRepository,
        execution_mode: str,
        previous_outputs_by_step: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], Optional[ProviderResponse], list[dict[str, Any]]]:
        step_definition = self._extract_step_definition(step)
        step_type = str(step_definition.get("step_type") or "llm_task").strip().lower()
        semantic_fields = self._planning_semantic_fields(step_definition)

        if step_type == "python_function":
            function_name = str(step_definition.get("function_name") or step.step_name)
            output_payload = execute_python_workload(
                function_name=function_name,
                step_name=step.step_name,
                run_input=run.input_snapshot_json or {},
                previous_outputs_by_step=previous_outputs_by_step,
                args=step_definition.get("args") if isinstance(step_definition.get("args"), dict) else {},
            )
            output_payload.update(
                {
                    "step_type": step_type,
                    "selected_capability_id": step_definition.get("selected_capability_id"),
                    "runtime_adapter": step_definition.get("runtime_adapter") or "builtin_python",
                    "handler_ref": step_definition.get("handler_ref") or function_name,
                    "provider": "python_function",
                    "model_name": function_name,
                    "mode": "deterministic",
                    "status": "completed",
                    "fallback_used": False,
                    "failure_class": None,
                    "retry_attempts": 0,
                    **semantic_fields,
                }
            )
            return output_payload, None, []

        if step_type == "web_search":
            output_payload = execute_web_search_workload(
                step_name=step.step_name,
                run_input=run.input_snapshot_json or {},
                previous_outputs_by_step=previous_outputs_by_step,
                args=step_definition.get("args") if isinstance(step_definition.get("args"), dict) else {},
            )
            output_payload.update(
                {
                    "step_type": step_type,
                    "selected_capability_id": step_definition.get("selected_capability_id"),
                    "runtime_adapter": step_definition.get("runtime_adapter") or "bounded_web_search",
                    "handler_ref": step_definition.get("handler_ref") or "web_search",
                    "provider": "bounded_web_search",
                    "model_name": "web_search",
                    "mode": "deterministic",
                    "status": "completed",
                    "fallback_used": False,
                    "failure_class": None,
                    "retry_attempts": 0,
                    **semantic_fields,
                }
            )
            return output_payload, None, []

        if step_type == "network_fetch":
            output_payload = execute_fetch_url_workload(
                step_name=step.step_name,
                run_input=run.input_snapshot_json or {},
                args=step_definition.get("args") if isinstance(step_definition.get("args"), dict) else {},
            )
            output_payload.update(
                {
                    "step_type": step_type,
                    "selected_capability_id": step_definition.get("selected_capability_id"),
                    "runtime_adapter": step_definition.get("runtime_adapter") or "bounded_network_fetch",
                    "handler_ref": step_definition.get("handler_ref") or "fetch_url",
                    "provider": "bounded_network_fetch",
                    "model_name": "fetch_url",
                    "mode": "deterministic",
                    "status": "completed",
                    "fallback_used": False,
                    "failure_class": None,
                    "retry_attempts": 0,
                    **semantic_fields,
                }
            )
            return output_payload, None, []

        if step_type == "filesystem":
            output_payload = execute_filesystem_workload(
                step_name=step.step_name,
                run_input=run.input_snapshot_json or {},
                previous_outputs_by_step=previous_outputs_by_step,
                args=step_definition.get("args") if isinstance(step_definition.get("args"), dict) else {},
            )
            output_payload.update(
                {
                    "step_type": step_type,
                    "selected_capability_id": step_definition.get("selected_capability_id"),
                    "runtime_adapter": step_definition.get("runtime_adapter") or "bounded_filesystem",
                    "handler_ref": step_definition.get("handler_ref") or "filesystem",
                    "provider": "bounded_filesystem",
                    "model_name": "filesystem",
                    "mode": "deterministic",
                    "status": "completed",
                    "fallback_used": False,
                    "failure_class": None,
                    "retry_attempts": 0,
                    **semantic_fields,
                }
            )
            return output_payload, None, []

        if step_type == "terminal_command":
            output_payload = execute_terminal_workload(
                step_name=step.step_name,
                run_input=run.input_snapshot_json or {},
                args=step_definition.get("args") if isinstance(step_definition.get("args"), dict) else {},
            )
            output_payload.update(
                {
                    "step_type": step_type,
                    "selected_capability_id": step_definition.get("selected_capability_id"),
                    "runtime_adapter": step_definition.get("runtime_adapter") or "bounded_terminal",
                    "handler_ref": step_definition.get("handler_ref") or "terminal_command",
                    "provider": "bounded_terminal",
                    "model_name": "terminal_command",
                    "mode": "deterministic",
                    "status": "completed",
                    "fallback_used": False,
                    "failure_class": None,
                    "retry_attempts": 0,
                    **semantic_fields,
                }
            )
            return output_payload, None, []

        if step_type == "browser_interaction":
            output_payload = execute_browser_workload(
                step_name=step.step_name,
                run_input=run.input_snapshot_json or {},
                previous_outputs_by_step=previous_outputs_by_step,
                args=step_definition.get("args") if isinstance(step_definition.get("args"), dict) else {},
            )
            output_payload.update(
                {
                    "step_type": step_type,
                    "selected_capability_id": step_definition.get("selected_capability_id"),
                    "runtime_adapter": step_definition.get("runtime_adapter") or "bounded_browser",
                    "handler_ref": step_definition.get("handler_ref") or "browser_interaction",
                    "provider": "bounded_browser",
                    "model_name": "browser_interaction",
                    "mode": "deterministic",
                    "status": "completed",
                    "fallback_used": False,
                    "failure_class": None,
                    "retry_attempts": 0,
                    **semantic_fields,
                }
            )
            return output_payload, None, []

        if step_type == "template_runtime":
            handler_ref = str(step_definition.get("handler_ref") or step.step_name)
            output_payload = execute_template_runtime_workload(
                handler_ref=handler_ref,
                step_name=step.step_name,
                run_input=run.input_snapshot_json or {},
                previous_outputs_by_step=previous_outputs_by_step,
                args=step_definition.get("args") if isinstance(step_definition.get("args"), dict) else {},
            )
            output_payload.update(
                {
                    "step_type": step_type,
                    "selected_capability_id": step_definition.get("selected_capability_id"),
                    "runtime_adapter": step_definition.get("runtime_adapter") or "materialized_template_runtime",
                    "handler_ref": handler_ref,
                    "provider": "materialized_template_runtime",
                    "model_name": handler_ref,
                    "mode": "deterministic",
                    "status": "completed",
                    "fallback_used": False,
                    "failure_class": None,
                    "retry_attempts": 0,
                    **semantic_fields,
                }
            )
            return output_payload, None, []

        if step_type == "blocked":
            blocked_reason = str(step_definition.get("blocked_reason") or "step is not runnable through the execution package")
            raise RuntimeError(blocked_reason)

        if step_type == "artifact_writer":
            writer_kind = str(step_definition.get("writer_kind") or step.step_name)
            writer_args = dict(step_definition.get("args") if isinstance(step_definition.get("args"), dict) else {})
            # Forward step-level fields to args for artifact writer consumption
            for key in ("output_filename", "task_prompt"):
                if key in step_definition and key not in writer_args:
                    writer_args[key] = step_definition[key]
            # If task_prompt is present, call LLM first to produce the content
            if step_definition.get("task_prompt") and settings.llm_mode == "real":
                llm_content = self._llm_produce_artifact_content(
                    run=run,
                    step=step,
                    step_definition=step_definition,
                    runtime_profile=runtime_profile,
                    model_config_repository=model_config_repository,
                    previous_outputs_by_step=previous_outputs_by_step,
                )
                if llm_content:
                    writer_args["_llm_produced_content"] = llm_content
            output_payload, artifacts = self._execute_artifact_writer_step(
                run=run,
                step=step,
                writer_kind=writer_kind,
                previous_outputs_by_step=previous_outputs_by_step,
                args=writer_args,
            )
            output_payload.update(semantic_fields)
            return output_payload, None, artifacts

        if self._step_supports_agentic_loop(step_definition):
            output_payload, provider_response, extra_artifacts = self._execute_agentic_step(
                run=run,
                step=step,
                step_definition=step_definition,
                runtime_profile=runtime_profile,
                model_config_repository=model_config_repository,
                execution_mode=execution_mode,
                previous_outputs_by_step=previous_outputs_by_step,
            )
            output_payload.update(semantic_fields)
            return output_payload, provider_response, extra_artifacts

        model_config, inline_overrides = self._resolve_model_config(
            runtime_profile,
            model_config_repository,
            step_definition=step_definition,
        )
        prompt = self._build_llm_prompt(
            blueprint_type=blueprint_type,
            step=step,
            execution_mode=execution_mode,
            step_definition=step_definition,
            previous_outputs_by_step=previous_outputs_by_step,
        )

        last_error: ProviderCallError | None = None
        for attempt in range(1, self.provider_max_attempts + 1):
            try:
                adapter, runtime_config = get_provider_adapter(model_config, overrides=inline_overrides)
                response = adapter.generate(prompt)
                output_payload = self._shape_llm_step_output(
                    step=step,
                    step_definition=step_definition,
                    response_content=response.content,
                    runtime_config={
                        "provider": runtime_config.provider,
                        "model_name": runtime_config.model_name,
                        "mode": runtime_config.llm_mode,
                        "source": runtime_config.source,
                    },
                    previous_outputs_by_step=previous_outputs_by_step,
                    run_input=run.input_snapshot_json or {},
                    fallback_reason=None,
                    retry_attempts=attempt,
                    fallback_used=False,
                    failure_class=None,
                )
                output_payload.update(semantic_fields)
                return output_payload, response, []
            except ProviderCallError as exc:
                last_error = exc
                if attempt < self.provider_max_attempts:
                    log_event(
                        logger,
                        logging.WARNING,
                        "run.step.provider_retry",
                        run_id=run.id,
                        step_id=step.id,
                        step_name=step.step_name,
                        attempt=attempt,
                        max_attempts=self.provider_max_attempts,
                        error=str(exc),
                    )
                    continue

        output_payload = self._shape_llm_step_output(
            step=step,
            step_definition=step_definition,
            response_content="",
            runtime_config={
                "provider": "fallback",
                "model_name": "fallback",
                "mode": settings.llm_mode,
                "source": "provider-fallback",
            },
            previous_outputs_by_step=previous_outputs_by_step,
            run_input=run.input_snapshot_json or {},
            fallback_reason=str(last_error) if last_error is not None else "provider call failed",
            retry_attempts=self.provider_max_attempts,
            fallback_used=True,
            failure_class="provider_error",
        )
        return output_payload, None, []

    def _resolve_model_config(
        self,
        runtime_profile,
        model_config_repository: ModelConfigRepository,
        *,
        step_definition: dict[str, Any],
    ):
        if runtime_profile is None:
            return None, self._inline_model_overrides(step_definition, allow_connection_overrides=True)
        bindings = runtime_profile.model_bindings_json or {}
        model_config_id = (
            step_definition.get("model_config_id")
            or bindings.get(step_definition.get("step_name") or "")
            or bindings.get("model_config_id")
            or bindings.get("default_model_config_id")
        )
        if model_config_id:
            model_config = model_config_repository.get_by_id(model_config_id)
            return model_config, self._inline_model_overrides(step_definition, allow_connection_overrides=False)
        return None, self._inline_model_overrides(step_definition, allow_connection_overrides=True)

    def _inline_model_overrides(
        self,
        step_definition: dict[str, Any],
        *,
        allow_connection_overrides: bool,
    ) -> dict[str, Any] | None:
        provider = step_definition.get("provider") if allow_connection_overrides else None
        model_name = step_definition.get("model_name") if allow_connection_overrides else None
        endpoint = step_definition.get("endpoint") if allow_connection_overrides else None
        api_key_ref = step_definition.get("api_key_ref") if allow_connection_overrides else None
        temperature = step_definition.get("temperature")
        max_tokens = step_definition.get("max_tokens")
        payload = {
            key: value
            for key, value in {
                "provider": provider,
                "model_name": model_name,
                "endpoint": endpoint,
                "api_key_ref": api_key_ref,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }.items()
            if value is not None
        }
        return payload or None

    def _extract_step_definition(self, step: RunStep) -> dict[str, Any]:
        resolved_plan_step = (step.input_payload_json or {}).get("resolved_plan_step", {})
        payload = resolved_plan_step.get("payload", {}) if isinstance(resolved_plan_step, dict) else {}
        step_definition = payload.get("step_definition", {}) if isinstance(payload, dict) else {}
        if isinstance(step_definition, dict) and step_definition:
            return step_definition
        return {
            "step_name": step.step_name,
            "node_name": step.node_name,
            "step_type": "llm_task",
        }

    def _build_llm_prompt(
        self,
        *,
        blueprint_type: str,
        step: RunStep,
        execution_mode: str,
        step_definition: dict[str, Any],
        previous_outputs_by_step: dict[str, dict[str, Any]],
    ) -> str:
        run_input = (step.input_payload_json or {}).get("run_input", {})
        system_prompt = step_definition.get("system_prompt") or "You are executing a blueprint-defined workload step."
        task_prompt = step_definition.get("task_prompt") or step_definition.get("instructions")
        if not task_prompt:
            task_prompt = (
                f"Execution mode: {execution_mode}\n"
                f"Blueprint type: {blueprint_type}\n"
                f"Step: {step.step_name}\n"
                "Produce a concise execution output summary."
            )
        prompt_parts = [
            system_prompt,
            "",
            task_prompt,
            "",
            f"Step definition: {json.dumps(step_definition, ensure_ascii=False, indent=2)}",
            f"Run input: {json.dumps(run_input, ensure_ascii=False, indent=2)}",
            f"Previous outputs: {json.dumps(previous_outputs_by_step, ensure_ascii=False, indent=2)}",
        ]
        if step_definition.get("expected_output"):
            prompt_parts.append(f"Expected output: {json.dumps(step_definition['expected_output'], ensure_ascii=False, indent=2)}")
        return "\n".join(prompt_parts)

    def _shape_llm_step_output(
        self,
        *,
        step: RunStep,
        step_definition: dict[str, Any],
        response_content: str,
        runtime_config: dict[str, Any],
        previous_outputs_by_step: dict[str, dict[str, Any]],
        run_input: dict[str, Any],
        fallback_reason: str | None,
        retry_attempts: int,
        fallback_used: bool,
        failure_class: str | None,
    ) -> dict[str, Any]:
        task_kind = str(step_definition.get("task_kind") or step.step_name)
        structured_output = self._parse_json_content(response_content)
        if structured_output is None:
            structured_output = self._fallback_structured_output(
                task_kind=task_kind,
                previous_outputs_by_step=previous_outputs_by_step,
                run_input=run_input,
                response_content=response_content,
                fallback_reason=fallback_reason,
            )

        content = response_content.strip()
        if not content:
            content = self._fallback_content(
                task_kind=task_kind,
                structured_output=structured_output,
                fallback_reason=fallback_reason,
            )
        summary = self._build_step_summary(
            task_kind=task_kind,
            structured_output=structured_output,
            fallback_reason=fallback_reason,
        )
        return {
            "step_name": step.step_name,
            "step_type": "llm_task",
            "task_kind": task_kind,
            "summary": summary,
            "content": content,
            "structured_output": structured_output,
            "provider": runtime_config["provider"],
            "model_name": runtime_config["model_name"],
            "mode": runtime_config["mode"],
            "source": runtime_config.get("source"),
            "status": "completed",
            "fallback_used": fallback_used,
            "failure_class": failure_class,
            "retry_attempts": retry_attempts,
            "failure_detail": fallback_reason,
        }

    def _execute_artifact_writer_step(
        self,
        *,
        run: Run,
        step: RunStep,
        writer_kind: str,
        previous_outputs_by_step: dict[str, dict[str, Any]],
        args: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        artifact_outputs = build_artifact_outputs(
            writer_kind=writer_kind,
            run_input=run.input_snapshot_json or {},
            previous_outputs_by_step=previous_outputs_by_step,
            args=args,
        )
        run_dir = ROOT_DIR / ".generated" / "runs" / run.project_id / run.id
        run_dir.mkdir(parents=True, exist_ok=True)
        persisted_artifacts: list[dict[str, Any]] = []
        for item in artifact_outputs:
            filename = item["filename"]
            content = item["content"]
            stored_content = json.dumps(content, indent=2, ensure_ascii=False) if item["content_format"] == "json" else str(content)
            file_path = run_dir / filename
            file_path.write_text(stored_content, encoding="utf-8")
            persisted_artifacts.append(
                {
                    "artifact_type": item["artifact_type"],
                    "stored_content": stored_content,
                    "metadata_json": {
                        "filename": filename,
                        "file_path": str(file_path),
                        "writer_kind": writer_kind,
                        "content_format": item["content_format"],
                    },
                }
            )
        return (
            {
                "step_name": step.step_name,
                "step_type": "artifact_writer",
                "summary": f"Wrote {len(persisted_artifacts)} artifacts for {writer_kind}.",
                "content": "\n".join(f"- {item['metadata_json']['filename']}" for item in persisted_artifacts),
                "structured_output": {
                    "writer_kind": writer_kind,
                    "artifacts": [item["metadata_json"] for item in persisted_artifacts],
                },
                "provider": "artifact_writer",
                "model_name": writer_kind,
                "mode": "deterministic",
                "status": "completed",
                "fallback_used": False,
                "failure_class": None,
                "retry_attempts": 0,
            },
            persisted_artifacts,
        )

    def _llm_produce_artifact_content(
        self,
        *,
        run: Run,
        step: RunStep,
        step_definition: dict[str, Any],
        runtime_profile,
        model_config_repository,
        previous_outputs_by_step: dict[str, dict[str, Any]],
    ) -> str | None:
        """Call LLM to produce artifact content before writing it."""
        try:
            model_config, inline_overrides = self._resolve_model_config(
                runtime_profile, model_config_repository, step_definition=step_definition,
            )
            adapter, _runtime_config = get_provider_adapter(model_config, overrides=inline_overrides)

            task_prompt = step_definition.get("task_prompt", "")
            # Build context from previous outputs
            prev_context = {}
            for sname, sout in previous_outputs_by_step.items():
                prev_context[sname] = {
                    "summary": sout.get("summary", ""),
                    "content": str(sout.get("content", ""))[:3000],
                }

            prompt = (
                "You are a report writer. Produce the final deliverable as markdown.\n\n"
                f"{task_prompt}\n\n"
                f"--- DATA FROM PREVIOUS STEPS ---\n"
                f"{json.dumps(prev_context, ensure_ascii=False, indent=2)}\n"
                f"--- END DATA ---\n\n"
                "Write the complete markdown report now. Output ONLY the markdown content."
            )
            response = adapter.generate(prompt)
            content = (response.content or "").strip()
            return content if content else None
        except Exception as exc:
            log_event(logger, logging.WARNING, "run.artifact_writer.llm_failed", error=str(exc))
            return None

    def _parse_json_content(self, response_content: str) -> dict[str, Any] | None:
        if not response_content.strip():
            return None
        candidates = [response_content.strip()]
        stripped = response_content.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            inner = stripped.split("\n", 1)[1].rsplit("\n", 1)[0] if "\n" in stripped else stripped
            candidates.append(inner.strip())
            if inner.startswith("json"):
                candidates.append(inner[4:].strip())
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    def _fallback_structured_output(
        self,
        *,
        task_kind: str,
        previous_outputs_by_step: dict[str, dict[str, Any]],
        run_input: dict[str, Any],
        response_content: str,
        fallback_reason: str | None,
    ) -> dict[str, Any]:
        if task_kind == "select_top_articles":
            articles = previous_outputs_by_step.get("fetch_articles", {}).get("structured_output", {}).get("articles", [])
            return {
                "selected_articles": articles[:3],
                "selection_reason": fallback_reason or "Fallback selection based on initial ranking.",
            }
        if task_kind == "summarize_articles":
            selected = previous_outputs_by_step.get("select_top_articles", {}).get("structured_output", {}).get("selected_articles", [])
            return {
                "summaries": [
                    {
                        "id": item.get("id"),
                        "title": item.get("title"),
                        "summary": f"{item.get('title')} matters because it impacts AI delivery teams.",
                    }
                    for item in selected
                ]
            }
        if task_kind == "generate_digest":
            selected = previous_outputs_by_step.get("select_top_articles", {}).get("structured_output", {}).get("selected_articles", [])
            return {
                "headline": "AI News Digest",
                "article_count": len(selected),
                "topic": run_input.get("topic") or "AI systems",
            }
        if task_kind == "filter_relevant":
            items = previous_outputs_by_step.get("normalize_items", {}).get("structured_output", {}).get("items", [])
            return {
                "relevant_items": items[:3],
                "filter_reason": fallback_reason or "Fallback kept the first three normalized items.",
            }
        if task_kind == "summarize_items":
            items = previous_outputs_by_step.get("filter_relevant", {}).get("structured_output", {}).get("relevant_items", [])
            return {
                "summaries": [
                    {
                        "id": item.get("id"),
                        "title": item.get("title"),
                        "summary": f"{item.get('title')} suggests stronger blueprint-to-runtime traceability.",
                    }
                    for item in items
                ]
            }
        if task_kind == "generate_report":
            items = previous_outputs_by_step.get("filter_relevant", {}).get("structured_output", {}).get("relevant_items", [])
            return {
                "headline": "AI Weekly Research Workflow Report",
                "item_count": len(items),
                "theme": run_input.get("theme") or "AI delivery",
            }
        if task_kind == "architecture_report_synthesis_llm":
            return {
                "report_markdown": response_content.strip() or "# Architecture Report\n\nNo report content was generated.",
                "citations": [],
                "fallback_reason": fallback_reason,
            }
        return {
            "raw_response": response_content,
            "fallback_reason": fallback_reason,
        }

    def _fallback_content(
        self,
        *,
        task_kind: str,
        structured_output: dict[str, Any],
        fallback_reason: str | None,
    ) -> str:
        if task_kind == "generate_digest":
            topic = structured_output.get("topic", "AI systems")
            count = structured_output.get("article_count", 0)
            return f"# AI News Digest\n\nTopic: {topic}\n\nSelected articles: {count}\n\nGenerated via fallback path."
        if task_kind == "generate_report":
            theme = structured_output.get("theme", "AI delivery")
            count = structured_output.get("item_count", 0)
            return f"# AI Weekly Research Report\n\nTheme: {theme}\n\nRelevant items: {count}\n\nGenerated via fallback path."
        if task_kind == "architecture_report_synthesis_llm":
            return structured_output.get("report_markdown") or "# Architecture Report\n\nGenerated via fallback path."
        if fallback_reason:
            return f"[provider-fallback] {task_kind}: {fallback_reason}"
        return json.dumps(structured_output, ensure_ascii=False, indent=2)

    def _build_step_summary(
        self,
        *,
        task_kind: str,
        structured_output: dict[str, Any],
        fallback_reason: str | None,
    ) -> str:
        if task_kind in {"select_top_articles", "filter_relevant"}:
            key = "selected_articles" if task_kind == "select_top_articles" else "relevant_items"
            return f"{task_kind} produced {len(structured_output.get(key, []))} items."
        if task_kind in {"summarize_articles", "summarize_items"}:
            return f"{task_kind} generated {len(structured_output.get('summaries', []))} summaries."
        if task_kind in {"generate_digest", "generate_report"}:
            return structured_output.get("headline") or f"{task_kind} completed."
        if task_kind == "architecture_report_synthesis_llm":
            return "Architecture report draft generated."
        if fallback_reason:
            return f"{task_kind} completed via fallback: {fallback_reason}"
        return f"{task_kind} completed."

    def _build_step_input(
        self,
        run: Run,
        runtime_profile,
        step: RunStep,
        outputs: list[dict[str, Any]],
        resolved_plan: Optional[ResolvedExecutionPlan],
    ) -> dict[str, Any]:
        previous_outputs_by_step = {item.get("step_name", f"step_{index + 1}"): item for index, item in enumerate(outputs)}
        payload = {
            "run_input": run.input_snapshot_json or {},
            "runtime_overrides": runtime_profile.input_overrides_json if runtime_profile else {},
            "debug_options": runtime_profile.debug_options_json if runtime_profile else {},
            "previous_outputs": outputs,
            "previous_outputs_by_step": previous_outputs_by_step,
            "step_name": step.step_name,
            "input_reuse": (resolved_plan.input_reuse if resolved_plan is not None else {}),
        }
        if resolved_plan is not None:
            matched = next((item for item in resolved_plan.resolved_steps if item.step_name == step.step_name), None)
            payload["resolved_plan_step"] = matched.model_dump() if matched is not None else {}
            payload["resolved_env"] = resolved_plan.resolved_env
            payload["resolved_input_sources"] = resolved_plan.resolved_input_sources
        return payload

    def _build_resolved_config_snapshot(
        self,
        *,
        run: Run,
        runtime_profile,
        executable,
        parsed_manifest: Optional[ParsedManifest],
        resolved_plan: Optional[ResolvedExecutionPlan],
        execution_mode: str,
        compatibility_mode: str,
        resolved_input_override: dict[str, Any] | None = None,
        resolved_env_override: dict[str, Any] | None = None,
        resolved_input_sources: dict[str, str] | None = None,
        input_reuse: dict[str, Any] | None = None,
        preflight_result: RuntimePreflightResult | None = None,
        input_validation: RuntimeInputValidationResult | None = None,
        runtime_operation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if runtime_profile is None:
            snapshot = {"runtime_profile": None, "llm_mode": settings.llm_mode}
        else:
            snapshot = {
                "runtime_profile_id": runtime_profile.id,
                "model_bindings_json": runtime_profile.model_bindings_json or {},
                "env_vars_json": runtime_profile.env_vars_json or {},
                "debug_options_json": runtime_profile.debug_options_json or {},
                "llm_mode": settings.llm_mode,
            }
        snapshot.update(
            {
                "execution_mode": execution_mode,
                "compatibility_mode": compatibility_mode,
                "executable_id": run.executable_id,
                "executable_build_version": getattr(executable, "build_version", None),
                "executable_source_path": getattr(executable, "source_path", None),
                "manifest": parsed_manifest.model_dump() if parsed_manifest is not None else None,
                "resolved_input": (
                    resolved_plan.resolved_input
                    if resolved_plan is not None
                    else resolved_input_override if resolved_input_override is not None else run.input_snapshot_json or {}
                ),
                "resolved_input_sources": (
                    resolved_plan.resolved_input_sources
                    if resolved_plan is not None
                    else resolved_input_sources or {}
                ),
                "resolved_env": (
                    resolved_plan.resolved_env
                    if resolved_plan is not None
                    else resolved_env_override if resolved_env_override is not None else {}
                ),
                "resolved_steps": [item.model_dump() for item in resolved_plan.resolved_steps] if resolved_plan is not None else [],
                "resolved_steps_compatibility_view": (
                    resolved_plan.resolved_steps_compatibility_view
                    if resolved_plan is not None
                    else {}
                ),
                "compatibility_execution_graph_projection": (
                    resolved_plan.compatibility_execution_graph_projection
                    if resolved_plan is not None
                    else {}
                ),
                "resolved_bindings": [item.model_dump() for item in resolved_plan.resolved_bindings] if resolved_plan is not None else [],
                "runtime_entry": resolved_plan.runtime_entry.model_dump(mode="python") if resolved_plan is not None and resolved_plan.runtime_entry is not None else {},
                "package_metadata": resolved_plan.package_metadata.model_dump(mode="python") if resolved_plan is not None and resolved_plan.package_metadata is not None else {},
                "workflow_artifact": resolved_plan.workflow_artifact if resolved_plan is not None else {},
                "workflow_native_explainability": resolved_plan.workflow_native_explainability if resolved_plan is not None else {},
                "resolved_artifacts_spec": resolved_plan.resolved_artifacts_spec if resolved_plan is not None else {},
                "resolved_observability": resolved_plan.resolved_observability if resolved_plan is not None else {},
                "input_reuse": resolved_plan.input_reuse if resolved_plan is not None else input_reuse or {},
                "input_validation": (
                    resolved_plan.input_validation.model_dump(mode="python")
                    if resolved_plan is not None
                    else input_validation.model_dump(mode="python") if input_validation is not None else {}
                ),
                "preflight": (
                    resolved_plan.preflight.model_dump(mode="python")
                    if resolved_plan is not None
                    else preflight_result.model_dump(mode="python") if preflight_result is not None else {}
                ),
                "runtime_operation": runtime_operation or {},
            }
        )
        return snapshot

    def _select_runtime_scheduler(self, *, resolved_plan: ResolvedExecutionPlan | None) -> dict[str, Any]:
        workflow_graph_candidate = WorkflowGraphScheduler().describe_support(resolved_plan=resolved_plan)
        # Separate non-serializable objects from the JSON-safe candidate dict
        dag_read_model = workflow_graph_candidate.pop("_dag_read_model", None)
        resolved_step_names = (
            [item.step_name for item in resolved_plan.resolved_steps]
            if resolved_plan is not None
            else []
        )
        read_model = workflow_graph_candidate.get("read_model")
        candidate_step_names = read_model.get("ordered_step_names", []) if isinstance(read_model, dict) else []

        # --- Check linear eligibility (exact order match) ---
        linear_eligible = bool(
            workflow_graph_candidate.get("supported")
            and isinstance(candidate_step_names, list)
            and candidate_step_names == resolved_step_names
            and len(set(candidate_step_names)) == len(candidate_step_names)
        )

        # --- Check parallel DAG eligibility (step name coverage, not order) ---
        dag_eligible = False
        if (
            not linear_eligible
            and dag_read_model is not None
            and dag_read_model.has_parallelism
            and workflow_graph_candidate.get("supported")
        ):
            dag_step_names = dag_read_model.ordered_step_names()
            dag_eligible = bool(
                set(dag_step_names) == set(resolved_step_names)
                and len(dag_step_names) == len(set(dag_step_names))
            )

        execution_trial_eligible = linear_eligible or dag_eligible

        if execution_trial_eligible:
            if dag_eligible and not linear_eligible:
                authority_source = "workflow_artifact.workflow_graph.dag_order"
                selection_reason = (
                    "workflow graph scheduler selected because parallel DAG workflow_graph step names "
                    "fully cover resolved_steps and can drive execution with DAG-backed structure truth."
                )
            else:
                authority_source = "workflow_artifact.workflow_graph.linear_order"
                selection_reason = (
                    "workflow graph scheduler selected because linear workflow_graph ordering is fully "
                    "aligned with resolved_steps and can drive the same execution path with graph-backed structure truth."
                )
            result = {
                "active_scheduler": WorkflowGraphScheduler.scheduler_kind,
                "selection_reason": selection_reason,
                "active_scheduler_authority_source": authority_source,
                "pending_step_authority_source": authority_source,
                "workflow_graph_candidate": {
                    **workflow_graph_candidate,
                    "execution_trial_eligible": True,
                },
            }
            if dag_read_model is not None:
                result["_dag_read_model"] = dag_read_model
            return result
        result = {
            "active_scheduler": CompatibilityStepScheduler.scheduler_kind,
            "selection_reason": "compatibility step scheduler remains authoritative because workflow graph execution trial eligibility was not satisfied.",
            "active_scheduler_authority_source": "resolved_steps.compatibility_sequence",
            "pending_step_authority_source": "resolved_steps.compatibility_sequence",
            "workflow_graph_candidate": {
                **workflow_graph_candidate,
                "execution_trial_eligible": False,
                "resolved_step_names": resolved_step_names,
            },
        }
        if dag_read_model is not None:
            result["_dag_read_model"] = dag_read_model
        return result

    def _token_usage_dict(
        self,
        provider_response: Optional[ProviderResponse],
        input_payload: dict[str, Any],
        output_payload: dict[str, Any],
    ) -> dict[str, Any]:
        if provider_response is not None:
            provider = provider_response.raw_metadata.get("provider", "provider")
            model_name = provider_response.raw_metadata.get("model", output_payload.get("model_name", "unknown-model"))
            total_tokens = provider_response.total_tokens
            prompt_tokens = provider_response.prompt_tokens
            completion_tokens = provider_response.completion_tokens
        else:
            prompt_text = str(input_payload)
            output_text = str(output_payload)
            prompt_tokens = max(24, len(prompt_text) // 4)
            completion_tokens = max(24, len(output_text) // 4)
            total_tokens = prompt_tokens + completion_tokens
            provider = output_payload.get("provider", "fallback")
            model_name = output_payload.get("model_name", "fallback")
        return {
            "provider": provider,
            "model_name": model_name,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "cost_estimate": float(Decimal(total_tokens) * Decimal("0.000001")),
        }

    def _build_run_summary(self, blueprint_name: str, outputs: list[dict[str, Any]], execution_mode: str) -> str:
        lines = [f"Run summary for blueprint: {blueprint_name}", f"Execution mode: {execution_mode}", ""]
        for item in outputs:
            lines.append(f"- {item.get('step_name')}: {item.get('summary')}")
        return "\n".join(lines)

    def _mark_step_cancelled(
        self,
        step: RunStep,
        *,
        scheduler_kind: str | None = None,
        scheduler_authority_source: str | None = None,
        pending_step_authority_source: str | None = None,
    ) -> None:
        step.status = RunStepStatus.FAILED.value
        step.finished_at = datetime.now(timezone.utc)
        step.log_excerpt = (step.log_excerpt or "") + "\n[cancelled] run cancelled before execution"
        step.output_payload_json = {
            "step_name": step.step_name,
            "status": "cancelled",
            "fallback_used": False,
            "failure_class": "cancelled",
            "failure_stage": "execution",
            "failure_detail": "run cancelled before execution",
            "retry_attempts": 0,
            "operation_state": {
                "step_name": step.step_name,
                "operation_state": "cancelled",
                "scheduler_kind": scheduler_kind,
                "scheduler_authority_source": scheduler_authority_source,
                "pending_step_authority_source": pending_step_authority_source,
            },
        }

    def _persist_run_artifact(
        self,
        *,
        artifact_repository: ArtifactRepository,
        run: Run,
        executable_id: str | None,
        artifact_type: str,
        content: str,
        metadata: dict[str, Any],
        run_step_id: str | None = None,
    ) -> None:
        artifact_repository.create(
            Artifact(
                build_task_id=None,
                build_step_id=None,
                build_id=None,
                build_step_v2_id=None,
                run_id=run.id,
                run_step_id=run_step_id,
                executable_id=executable_id,
                artifact_type=artifact_type,
                storage_kind="inline",
                path_or_content=content,
                metadata_json=metadata,
            )
        )

    def _classify_failure(
        self,
        *,
        error: Exception,
        failure_class: str | None,
        failure_stage: str | None,
        current_step: RunStep | None,
    ) -> tuple[str, str]:
        if failure_class is not None and failure_stage is not None:
            return failure_class, failure_stage

        if isinstance(error, ProviderCallError):
            return "provider_error", "execution" if current_step is not None else "preflight"

        message = str(error).lower()
        if "cancelled" in message:
            return "cancelled", "execution"
        if "manifest validation failed" in message or "manifest" in message and "not found" in message:
            return "manifest_invalid", "preflight"
        if "required runtime env vars" in message:
            return "runtime_env_missing", "preflight"
        if "runtime input" in message or "is required" in message or "is not allowed" in message:
            return "runtime_input_invalid", "preflight"
        if current_step is not None:
            step_definition = self._extract_step_definition(current_step)
            step_type = str(step_definition.get("step_type") or "llm_task").strip().lower()
            if step_type == "artifact_writer":
                return "artifact_write_error", "execution"
        if current_step is None:
            return "step_execution_error", "preflight"
        return "step_execution_error", "execution"

    def _replace_run_steps_from_plan(
        self,
        run_step_repository: RunStepRepository,
        run: Run,
        resolved_plan: ResolvedExecutionPlan,
        *,
        preserve_existing: bool = False,
    ) -> None:
        existing_steps = run_step_repository.list_by_run(run.id)
        desired_names = [item.step_name for item in resolved_plan.resolved_steps]
        existing_names = [item.step_name for item in existing_steps]

        if preserve_existing and existing_steps and existing_names == desired_names:
            for index, (existing, resolved_step) in enumerate(zip(existing_steps, resolved_plan.resolved_steps)):
                existing.step_order = index + 1
                existing.node_name = resolved_step.node_name
            return

        run_step_repository.delete_by_run(run.id)
        run_step_repository.create_many(
            [
                RunStep(
                    run_id=run.id,
                    step_name=item.step_name,
                    step_order=index + 1,
                    node_name=item.node_name,
                    status=RunStepStatus.PENDING.value,
                )
                for index, item in enumerate(resolved_plan.resolved_steps)
            ]
        )

    def _get_runtime_operation_snapshot(self, run: Run) -> dict[str, Any]:
        resolved_snapshot = dict(run.resolved_config_snapshot_json or {})
        snapshot = dict(resolved_snapshot.get("runtime_operation") or {})
        runtime_scheduler = dict(resolved_snapshot.get("runtime_scheduler") or {})
        snapshot.setdefault("run_id", run.id)
        snapshot.setdefault("operation_state", "idle")
        snapshot.setdefault("scheduler_kind", runtime_scheduler.get("active_scheduler"))
        snapshot.setdefault("scheduler_authority_source", runtime_scheduler.get("active_scheduler_authority_source"))
        snapshot.setdefault("confirmation_required", False)
        snapshot.setdefault("intervention_required", False)
        snapshot.setdefault("intervention_legality_authority_source", None)
        snapshot.setdefault("resume_supported", False)
        snapshot.setdefault("rerun_supported", False)
        snapshot.setdefault("rerun_authority_source", None)
        snapshot.setdefault("rerun_requires_human_approval", False)
        snapshot.setdefault("step_states", [])
        snapshot.setdefault("approved_step_names", [])
        snapshot.setdefault("pending_step_authority_source", runtime_scheduler.get("pending_step_authority_source"))
        return snapshot

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _apply_runtime_operation(self, *, run: Run, patch: dict[str, Any]) -> dict[str, Any]:
        snapshot = dict(run.resolved_config_snapshot_json or {})
        runtime_operation = self._get_runtime_operation_snapshot(run)
        runtime_operation.update(patch)
        runtime_operation["run_id"] = run.id
        snapshot["runtime_operation"] = runtime_operation
        run.resolved_config_snapshot_json = snapshot
        return runtime_operation

    def _preflight_requires_intervention(self, preflight_result: RuntimePreflightResult) -> bool:
        return preflight_result.failure_class == "execution_package_blocked"

    def _step_definition_for_name(self, resolved_plan: ResolvedExecutionPlan | None, step_name: str) -> dict[str, Any]:
        if resolved_plan is not None:
            matched = next((item for item in resolved_plan.resolved_steps if item.step_name == step_name), None)
            if matched is not None and matched.payload and isinstance(matched.payload.get("step_definition"), dict):
                return matched.payload["step_definition"]
        return {"step_name": step_name, "step_type": "llm_task"}

    def _planning_semantic_fields(self, step_definition: dict[str, Any]) -> dict[str, Any]:
        return {
            "semantic_role": step_definition.get("semantic_role"),
            "consumes_evidence_types": list(step_definition.get("consumes_evidence_types") or []),
            "produces_evidence_types": list(step_definition.get("produces_evidence_types") or []),
            "deliverable_intents": list(step_definition.get("deliverable_intents") or []),
            "planning_dependencies": list(step_definition.get("planning_dependencies") or []),
        }

    # -----------------------------------------------------------------------
    # Agentic loop execution
    # -----------------------------------------------------------------------

    _AGENTIC_FORBIDDEN_STEP_TYPES = frozenset({
        "python_function", "template_runtime", "blocked", "artifact_writer",
        "web_search", "network_fetch", "filesystem", "terminal_command", "browser_interaction",
    })
    _AGENTIC_ALLOWED_SIDE_EFFECTS = frozenset({"none", "external_read", ""})

    def _step_supports_agentic_loop(self, step_definition: dict[str, Any]) -> bool:
        """Check hard eligibility rules for agentic_loop execution mode.

        All must hold:
        - step_type == "llm_task" (or absent, defaulting to llm_task)
        - args.available_tools is a non-empty list
        - writer_kind is None
        - step is not a forbidden step type
        - side_effect_class is none or external_read
        """
        step_type = str(step_definition.get("step_type") or "llm_task").strip().lower()
        if step_type != "llm_task":
            return False
        if step_type in self._AGENTIC_FORBIDDEN_STEP_TYPES:
            return False
        args = step_definition.get("args")
        if not isinstance(args, dict):
            return False
        available_tools = args.get("available_tools")
        if not isinstance(available_tools, list) or not available_tools:
            return False
        if step_definition.get("writer_kind"):
            return False
        side_effect = str(step_definition.get("side_effect_class") or "none").strip().lower()
        if side_effect not in self._AGENTIC_ALLOWED_SIDE_EFFECTS:
            return False
        return True

    def _execute_agentic_step(
        self,
        *,
        run: Run,
        step: RunStep,
        step_definition: dict[str, Any],
        runtime_profile,
        model_config_repository: ModelConfigRepository,
        execution_mode: str,
        previous_outputs_by_step: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], Optional[ProviderResponse], list[dict[str, Any]]]:
        from app.services.run.node_loop_executor import (
            NodeLoopConfig,
            execute_node_loop,
        )

        model_config, inline_overrides = self._resolve_model_config(
            runtime_profile,
            model_config_repository,
            step_definition=step_definition,
        )
        adapter, runtime_config = get_provider_adapter(model_config, overrides=inline_overrides)

        args = step_definition.get("args") or {}
        loop_config_raw = args.get("loop_config") or {}
        config = NodeLoopConfig(
            max_iterations=int(loop_config_raw.get("max_iterations", 10)),
            max_duration_seconds=int(loop_config_raw.get("max_duration_seconds", 120)),
            max_total_tokens=int(loop_config_raw.get("max_total_tokens", 50_000)),
            available_tools=list(args.get("available_tools") or []),
            require_final_answer=bool(loop_config_raw.get("require_final_answer", True)),
            allow_partial_on_budget=bool(loop_config_raw.get("allow_partial_on_budget", True)),
        )

        system_prompt = step_definition.get("system_prompt") or "You are executing a workload step with tool access."
        task_prompt = step_definition.get("task_prompt") or step_definition.get("instructions") or ""
        # Always inject run_input and previous outputs for step continuity
        context_block = (
            f"\n\n--- CONTEXT ---\n"
            f"Step: {step.step_name}\n"
            f"Run input: {json.dumps(run.input_snapshot_json or {}, ensure_ascii=False)}\n"
        )
        if previous_outputs_by_step:
            # Include summaries + content from previous steps (truncated for context budget)
            prev_summaries = {}
            for sname, sout in previous_outputs_by_step.items():
                prev_summaries[sname] = {
                    "summary": sout.get("summary", ""),
                    "content": str(sout.get("content", ""))[:2000],
                }
            context_block += f"Previous step outputs:\n{json.dumps(prev_summaries, ensure_ascii=False, indent=2)}\n"
        context_block += "--- END CONTEXT ---"

        if not task_prompt:
            task_prompt = (
                f"Step: {step.step_name}\n"
                f"Execution mode: {execution_mode}\n"
                "Use the available tools to complete this step, then provide a final answer."
            )
        task_prompt = task_prompt + context_block

        tool_definitions = self._build_agentic_tool_definitions(config.available_tools)
        tool_executors = self._build_agentic_tool_executors(
            available_tools=config.available_tools,
            run_input=run.input_snapshot_json or {},
            previous_outputs_by_step=previous_outputs_by_step,
            step_definition=step_definition,
        )

        result = execute_node_loop(
            config=config,
            adapter=adapter,
            system_prompt=system_prompt,
            task_prompt=task_prompt,
            tool_definitions=tool_definitions,
            tool_executors=tool_executors,
        )

        output_payload = {
            "step_name": step.step_name,
            "step_type": "llm_task",
            "execution_mode_detail": "agentic_loop",
            "summary": result.final_output.get("summary", ""),
            "content": result.final_output.get("content", ""),
            "structured_output": result.final_output,
            "iteration_count": result.iteration_count,
            "iteration_trace": [
                {
                    "iteration_index": it.iteration_index,
                    "action": it.action,
                    "tool_name": it.tool_name,
                    "tool_args": it.tool_args,
                    "observation_summary": it.observation_summary,
                    "tool_status": it.tool_status,
                    "tool_error": it.tool_error,
                    "tool_execution_duration_ms": it.tool_execution_duration_ms,
                    "tokens_used": it.tokens_used,
                    "duration_ms": it.duration_ms,
                }
                for it in result.iteration_trace
            ],
            "budget_exhausted": result.budget_exhausted,
            "provider": runtime_config.provider,
            "model_name": runtime_config.model_name,
            "mode": runtime_config.llm_mode,
            "source": runtime_config.source,
            "status": "completed",
            "fallback_used": False,
            "failure_class": None,
            "retry_attempts": 0,
            "selected_capability_id": step_definition.get("selected_capability_id"),
            "runtime_adapter": step_definition.get("runtime_adapter") or "agentic_loop",
            "handler_ref": step_definition.get("handler_ref") or "node_loop_executor",
        }
        # Approximate a ProviderResponse for token accounting
        provider_response = ProviderResponse(
            content=result.final_output.get("content", ""),
            prompt_tokens=result.total_tokens_used // 2,
            completion_tokens=result.total_tokens_used - result.total_tokens_used // 2,
            total_tokens=result.total_tokens_used,
            raw_metadata={
                "mode": runtime_config.llm_mode,
                "provider": runtime_config.provider,
                "model": runtime_config.model_name,
                "agentic_loop": True,
                "iteration_count": result.iteration_count,
                "budget_exhausted": result.budget_exhausted,
            },
        )
        return output_payload, provider_response, []

    _AGENTIC_TOOL_REGISTRY: dict[str, dict[str, Any]] = {
        "web_search": {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search for information using a text query. Returns a list of relevant results from bounded corpus or network sources.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query."},
                        "limit": {"type": "integer", "description": "Max number of results (1-10)."},
                    },
                    "required": ["query"],
                },
            },
        },
        "fetch_url": {
            "type": "function",
            "function": {
                "name": "fetch_url",
                "description": "Fetch the content of a URL or local file path. Returns the text content of the resource.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL or local file path to fetch."},
                    },
                    "required": ["url"],
                },
            },
        },
        "read_file": {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read the text content of a file. Returns the file content (truncated to max_bytes).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative file path within the workspace."},
                        "max_bytes": {"type": "integer", "description": "Max bytes to read (default 12000, max 50000)."},
                    },
                    "required": ["path"],
                },
            },
        },
        "browse": {
            "type": "function",
            "function": {
                "name": "browse",
                "description": "Browse a web page or local HTML file (read-only). Supports extracting text, inspecting links, or opening a page.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL or local file path to browse."},
                        "action": {
                            "type": "string",
                            "enum": ["extract_text", "inspect_links", "open_page"],
                            "description": "Browse action (default: extract_text). Only read-only actions are allowed.",
                        },
                    },
                    "required": ["url"],
                },
            },
        },
    }

    def _build_agentic_tool_definitions(self, available_tools: list[str]) -> list[dict[str, Any]]:
        return [
            self._AGENTIC_TOOL_REGISTRY[name]
            for name in available_tools
            if name in self._AGENTIC_TOOL_REGISTRY
        ]

    def _build_agentic_tool_executors(
        self,
        *,
        available_tools: list[str],
        run_input: dict[str, Any],
        previous_outputs_by_step: dict[str, dict[str, Any]],
        step_definition: dict[str, Any],
    ) -> dict[str, Callable]:
        executors: dict[str, Callable] = {}
        step_args = step_definition.get("args") if isinstance(step_definition.get("args"), dict) else {}

        if "web_search" in available_tools:
            def _exec_web_search(_tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
                merged_args = {**step_args, **tool_args}
                if "query" in tool_args:
                    merged_args["query"] = tool_args["query"]
                return execute_web_search_workload(
                    step_name="web_search",
                    run_input=run_input,
                    previous_outputs_by_step=previous_outputs_by_step,
                    args=merged_args,
                )
            executors["web_search"] = _exec_web_search

        if "fetch_url" in available_tools:
            def _exec_fetch_url(_tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
                merged_args = {**step_args, **tool_args}
                if "url" in tool_args:
                    merged_args["url"] = tool_args["url"]
                return execute_fetch_url_workload(
                    step_name="fetch_url",
                    run_input=run_input,
                    args=merged_args,
                )
            executors["fetch_url"] = _exec_fetch_url

        if "read_file" in available_tools:
            def _exec_read_file(_tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
                merged_args = {**step_args, "action": "read", **tool_args}
                return execute_filesystem_workload(
                    step_name="read_file",
                    run_input=run_input,
                    previous_outputs_by_step=previous_outputs_by_step,
                    args=merged_args,
                )
            executors["read_file"] = _exec_read_file

        if "browse" in available_tools:
            _BROWSE_ALLOWED_ACTIONS = {"extract_text", "inspect_links", "open_page"}

            def _exec_browse(_tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
                action = str(tool_args.get("action") or "extract_text").strip().lower()
                if action not in _BROWSE_ALLOWED_ACTIONS:
                    raise ValueError(f"browse tool only allows read-only actions: {_BROWSE_ALLOWED_ACTIONS}")
                merged_args = {**step_args, "action": action, **tool_args}
                if "url" in tool_args:
                    merged_args["url"] = tool_args["url"]
                return execute_browser_workload(
                    step_name="browse",
                    run_input=run_input,
                    previous_outputs_by_step=previous_outputs_by_step,
                    args=merged_args,
                )
            executors["browse"] = _exec_browse

        return executors

    def _step_requires_human_gate(
        self,
        *,
        resolved_plan: ResolvedExecutionPlan | None,
        step_name: str,
        step_definition: dict[str, Any],
    ) -> tuple[bool, str | None]:
        workflow_gate_step_names = self._workflow_artifact_gate_step_names(resolved_plan)
        if workflow_gate_step_names and step_name in workflow_gate_step_names:
            return True, "workflow_artifact.execution_contract.human_gate_policy"
        if step_definition.get("requires_confirmation"):
            return True, "resolved_steps.compatibility_step_definition"
        return False, None

    def _workflow_artifact_gate_step_names(self, resolved_plan: ResolvedExecutionPlan | None) -> set[str]:
        if resolved_plan is None or not isinstance(resolved_plan.workflow_artifact, dict):
            return set()
        execution_contract = resolved_plan.workflow_artifact.get("execution_contract", {})
        if not isinstance(execution_contract, dict):
            return set()
        human_gate_policy = execution_contract.get("human_gate_policy", {})
        if not isinstance(human_gate_policy, dict):
            return set()
        required_gate_node_ids = human_gate_policy.get("required_gate_node_ids", [])
        if not isinstance(required_gate_node_ids, list):
            return set()

        gate_step_names: set[str] = set()
        for node_id in required_gate_node_ids:
            if not isinstance(node_id, str) or not node_id.startswith("node_"):
                continue
            try:
                node_index = int(node_id.split("_", 1)[1]) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= node_index < len(resolved_plan.resolved_steps):
                gate_step_names.add(resolved_plan.resolved_steps[node_index].step_name)
        return gate_step_names

    def _resolve_runtime_policy_bundle(
        self,
        *,
        resolved_plan: ResolvedExecutionPlan | None,
        current_step: RunStep | None,
    ) -> dict[str, Any]:
        intervention_policy = self._execution_contract_policy(
            resolved_plan=resolved_plan,
            policy_name="intervention_legality",
        )
        intervention_source = "workflow_artifact.execution_contract.intervention_legality"
        if not (isinstance(intervention_policy, dict) and isinstance(intervention_policy.get("allowed_modes"), list)):
            intervention_policy = self._shadow_compatibility_policy(
                resolved_plan=resolved_plan,
                shadow_section="intervention_legality",
            )
            intervention_source = "resolved_steps_and_bindings.compatibility_projection"
        if not (isinstance(intervention_policy, dict) and isinstance(intervention_policy.get("allowed_modes"), list)):
            intervention_policy = {
                "allowed_modes": ["inspect", "approve", "reject", "rerun"],
                "required_before_execute": False,
                "required_before_commit": False,
                "editable_fields": [],
                "skip_policy": "forbidden",
                "resume_policy": "resume_from_current",
            }
            intervention_source = "runtime.compatibility_default.intervention_legality"

        if current_step is None:
            return {
                "failure_escalation_to_human_gate": False,
                "failure_escalation_authority_source": "runtime.compatibility_no_current_step",
                "intervention_policy": intervention_policy,
                "intervention_authority_source": intervention_source,
                "rerun_allowed": False,
                "rerun_requires_human_approval": False,
                "rerun_authority_source": "runtime.compatibility_no_current_step",
            }

        failure_policy = self._execution_contract_policy(
            resolved_plan=resolved_plan,
            policy_name="failure_escalation_policy",
        )
        failure_source = "workflow_artifact.execution_contract.failure_escalation_policy"
        if not (isinstance(failure_policy, dict) and "escalate_to_human_gate" in failure_policy):
            failure_policy = self._shadow_compatibility_policy(
                resolved_plan=resolved_plan,
                shadow_section="failure_escalation_hints",
            )
            failure_source = "resolved_steps_and_bindings.compatibility_projection"
        if not (isinstance(failure_policy, dict) and "escalate_to_human_gate" in failure_policy):
            failure_policy = {"escalate_to_human_gate": True}
            failure_source = "runtime.compatibility_default.failure_escalation"

        rerun_policy = self._execution_contract_policy(
            resolved_plan=resolved_plan,
            policy_name="rerun_legality_policy",
        )
        rerun_source = "workflow_artifact.execution_contract.rerun_legality_policy"
        if not (isinstance(rerun_policy, dict) and "rerun_allowed" in rerun_policy):
            rerun_policy = self._shadow_compatibility_policy(
                resolved_plan=resolved_plan,
                shadow_section="rerun_legality",
            )
            rerun_source = "resolved_steps_and_bindings.compatibility_projection"
        if not (isinstance(rerun_policy, dict) and "rerun_allowed" in rerun_policy):
            rerun_policy = {
                "rerun_allowed": True,
                "rerun_requires_human_approval": False,
            }
            rerun_source = "runtime.compatibility_default.rerun_legality"

        return {
            "failure_escalation_to_human_gate": bool(failure_policy.get("escalate_to_human_gate")),
            "failure_escalation_authority_source": failure_source,
            "intervention_policy": intervention_policy,
            "intervention_authority_source": intervention_source,
            "rerun_allowed": bool(rerun_policy.get("rerun_allowed")),
            "rerun_requires_human_approval": bool(rerun_policy.get("rerun_requires_human_approval")),
            "rerun_authority_source": rerun_source,
        }

    def _execution_contract_policy(
        self,
        *,
        resolved_plan: ResolvedExecutionPlan | None,
        policy_name: str,
    ) -> dict[str, Any]:
        if resolved_plan is None or not isinstance(resolved_plan.workflow_artifact, dict):
            return {}
        execution_contract = resolved_plan.workflow_artifact.get("execution_contract", {})
        if not isinstance(execution_contract, dict):
            return {}
        policy = execution_contract.get(policy_name, {})
        return policy if isinstance(policy, dict) else {}

    def _shadow_compatibility_policy(
        self,
        *,
        resolved_plan: ResolvedExecutionPlan | None,
        shadow_section: str,
    ) -> dict[str, Any]:
        if resolved_plan is None:
            return {}
        explainability = resolved_plan.workflow_native_explainability or {}
        if not isinstance(explainability, dict):
            return {}
        shadow = explainability.get("workflow_native_shadow_authority", {})
        if not isinstance(shadow, dict):
            return {}
        section = shadow.get(shadow_section, {})
        if not isinstance(section, dict):
            return {}
        compatibility = section.get("compatibility_projection", {})
        return compatibility if isinstance(compatibility, dict) else {}

    def _intervention_mode_allowed(self, policy: dict[str, Any], mode: str) -> bool:
        allowed_modes = policy.get("allowed_modes")
        if not isinstance(allowed_modes, list):
            return False
        return mode in {str(item) for item in allowed_modes}

    def _intervention_resume_supported(self, policy: dict[str, Any]) -> bool:
        return str(policy.get("resume_policy") or "resume_from_current").strip() != "forbidden"

    def _pause_for_confirmation(
        self,
        *,
        db,
        run: Run,
        step: RunStep,
        step_definition: dict[str, Any],
        artifact_repository: ArtifactRepository,
        execution_mode: str,
        last_successful_step: str | None,
        authority_source: str,
    ) -> dict[str, Any]:
        runtime_scheduler = dict((run.resolved_config_snapshot_json or {}).get("runtime_scheduler") or {})
        confirmation_reason = (
            f"Step '{step.step_name}' requires confirmation before executing "
            f"{step_definition.get('runtime_adapter') or step_definition.get('step_type') or 'runtime action'}."
        )
        recovery_options = [
            RuntimeRecoveryOptionResponse(action="resume_run", label="Approve and continue").model_dump(mode="python"),
            RuntimeRecoveryOptionResponse(action="abort_run", label="Abort run").model_dump(mode="python"),
        ]
        step.output_payload_json = {
            "step_name": step.step_name,
            "status": "waiting_for_confirmation",
            "confirmation_required": True,
            "confirmation_reason": confirmation_reason,
            "authority_source": authority_source,
            "resume_supported": True,
            "rerun_supported": False,
            "recovery_options": recovery_options,
            "operation_state": {
                "step_name": step.step_name,
                "operation_state": "waiting_for_confirmation",
                "scheduler_kind": runtime_scheduler.get("active_scheduler"),
                "scheduler_authority_source": runtime_scheduler.get("active_scheduler_authority_source"),
                "confirmation_required": True,
                "confirmation_reason": confirmation_reason,
                "authority_source": authority_source,
                "resume_supported": True,
                "rerun_supported": False,
                "pending_step_authority_source": runtime_scheduler.get("pending_step_authority_source"),
                "recovery_options": recovery_options,
            },
        }
        step.log_excerpt = f"[waiting_for_confirmation] {step.step_name}"
        step.status = RunStepStatus.PENDING.value
        step.started_at = None
        step.finished_at = None
        runtime_operation = self._apply_runtime_operation(
            run=run,
            patch={
                "operation_state": "waiting_for_confirmation",
                "scheduler_kind": runtime_scheduler.get("active_scheduler"),
                "scheduler_authority_source": runtime_scheduler.get("active_scheduler_authority_source"),
                "confirmation_required": True,
                "confirmation_request": {
                    "run_id": run.id,
                    "step_id": step.id,
                    "step_name": step.step_name,
                    "confirmation_required": True,
                    "confirmation_reason": confirmation_reason,
                    "runtime_adapter": step_definition.get("runtime_adapter"),
                    "handler_ref": step_definition.get("handler_ref"),
                    "policy_scope": step_definition.get("policy_scope"),
                    "authority_source": authority_source,
                    "requested_at": self._timestamp(),
                },
                "resume_supported": True,
                "rerun_supported": False,
                "last_successful_step": last_successful_step,
                "pending_step": step.step_name,
                "pending_step_authority_source": runtime_scheduler.get("pending_step_authority_source"),
                "blocked_reason": None,
                "recovery_summary": {
                    "run_id": run.id,
                    "operation_state": "waiting_for_confirmation",
                    "last_successful_step": last_successful_step,
                    "pending_step": step.step_name,
                    "pending_step_authority_source": runtime_scheduler.get("pending_step_authority_source"),
                    "recovery_continuation_step": step.step_name,
                    "recovery_continuation_authority_source": runtime_scheduler.get("pending_step_authority_source"),
                    "blocked_reason": None,
                    "recovery_options": recovery_options,
                    "updated_at": self._timestamp(),
                },
                "updated_at": self._timestamp(),
            },
        )
        run.summary = f"Waiting for confirmation before '{step.step_name}'."
        self._persist_run_artifact(
            artifact_repository=artifact_repository,
            run=run,
            executable_id=run.executable_id,
            artifact_type="runtime_confirmation_request",
            content=json.dumps(runtime_operation, ensure_ascii=False, indent=2),
            metadata={"execution_mode": execution_mode, "operation_state": "waiting_for_confirmation"},
            run_step_id=step.id,
        )
        db.commit()
        return runtime_operation

    def _build_checkpoint(
        self,
        *,
        run: Run,
        completed_step_names: list[str],
        last_successful_step: str,
        pending_step: str | None,
        pending_step_authority_source: str | None = None,
        scheduler_kind: str | None = None,
        scheduler_authority_source: str | None = None,
    ) -> dict[str, Any]:
        checkpoint_id = f"{run.id}:{last_successful_step}:{len(completed_step_names)}"
        is_graph_routed = scheduler_kind == WorkflowGraphScheduler.scheduler_kind
        routing_source = "workflow_graph" if is_graph_routed else "compatibility_step_sequence"
        routing_metadata = self._build_checkpoint_routing_metadata(
            run=run,
            is_graph_routed=is_graph_routed,
            pending_step=pending_step,
            pending_step_authority_source=pending_step_authority_source,
            completed_step_names=completed_step_names,
        )
        return {
            "checkpoint_id": checkpoint_id,
            "checkpoint_summary": f"Completed '{last_successful_step}' and can continue from '{pending_step or 'end'}'.",
            "last_successful_step": last_successful_step,
            "pending_step": pending_step,
            "pending_step_authority_source": pending_step_authority_source,
            "scheduler_kind": scheduler_kind,
            "scheduler_authority_source": scheduler_authority_source,
            "routing_source": routing_source,
            "routing_metadata": routing_metadata,
            "completed_step_names": completed_step_names,
            "created_at": self._timestamp(),
        }

    def _build_checkpoint_routing_metadata(
        self,
        *,
        run: Run,
        is_graph_routed: bool,
        pending_step: str | None,
        pending_step_authority_source: str | None,
        completed_step_names: list[str],
    ) -> dict[str, Any]:
        if not is_graph_routed:
            return {
                "routing_kind": "compatibility_step_sequence",
                "graph_authority_active": False,
                "pending_step_determined_by": pending_step_authority_source or "resolved_steps.compatibility_sequence",
                "total_completed": len(completed_step_names),
            }
        runtime_scheduler = dict((run.resolved_config_snapshot_json or {}).get("runtime_scheduler") or {})
        read_model = (runtime_scheduler.get("workflow_graph_candidate") or {}).get("read_model")
        total_graph_steps = 0
        remaining_graph_steps = 0
        if isinstance(read_model, dict):
            ordered = read_model.get("ordered_step_names", [])
            if isinstance(ordered, list):
                total_graph_steps = len(ordered)
                completed_set = set(completed_step_names)
                remaining_graph_steps = sum(1 for name in ordered if name not in completed_set)
        return {
            "routing_kind": "workflow_graph",
            "graph_authority_active": True,
            "graph_read_model_kind": read_model.get("read_model_kind") if isinstance(read_model, dict) else None,
            "pending_step_determined_by": pending_step_authority_source or "workflow_artifact.workflow_graph.linear_order",
            "total_completed": len(completed_step_names),
            "total_graph_steps": total_graph_steps,
            "remaining_graph_steps": remaining_graph_steps,
        }

    def _is_graph_scheduler_authoritative(self, runtime_scheduler: dict[str, Any]) -> bool:
        if runtime_scheduler.get("active_scheduler") != WorkflowGraphScheduler.scheduler_kind:
            return False
        candidate = runtime_scheduler.get("workflow_graph_candidate")
        if not isinstance(candidate, dict):
            return False
        return bool(candidate.get("execution_trial_eligible"))

    def _resolve_runtime_step_order(
        self,
        *,
        run: Run,
        steps: list[RunStep],
    ) -> tuple[list[RunStep], str, bool]:
        ordered_step_names, authority_source, graph_authoritative = self._resolve_runtime_step_order_names(
            run=run,
            fallback_step_names=[step.step_name for step in steps],
        )
        if not graph_authoritative:
            return steps, authority_source, False
        steps_by_name = {step.step_name: step for step in steps}
        if not all(name in steps_by_name for name in ordered_step_names):
            return steps, "resolved_steps.compatibility_sequence", False
        if len(ordered_step_names) != len(steps):
            return steps, "resolved_steps.compatibility_sequence", False
        return [steps_by_name[name] for name in ordered_step_names], authority_source, True

    def _resolve_runtime_step_order_names(
        self,
        *,
        run: Run,
        fallback_step_names: list[str] | None = None,
    ) -> tuple[list[str], str, bool]:
        runtime_scheduler = dict((run.resolved_config_snapshot_json or {}).get("runtime_scheduler") or {})
        if not self._is_graph_scheduler_authoritative(runtime_scheduler):
            return list(fallback_step_names or []), "resolved_steps.compatibility_sequence", False
        read_model = (runtime_scheduler.get("workflow_graph_candidate") or {}).get("read_model")
        if not isinstance(read_model, dict):
            return list(fallback_step_names or []), "resolved_steps.compatibility_sequence", False
        ordered_step_names = read_model.get("ordered_step_names", [])
        if not isinstance(ordered_step_names, list):
            return list(fallback_step_names or []), "resolved_steps.compatibility_sequence", False
        return list(ordered_step_names), "workflow_artifact.workflow_graph.linear_order", True

    def _graph_ordered_steps(self, run: Run, steps: list[RunStep]) -> list[RunStep]:
        ordered_steps, _authority_source, _graph_authoritative = self._resolve_runtime_step_order(
            run=run,
            steps=steps,
        )
        return ordered_steps

    def _graph_aware_next_pending_step(
        self,
        *,
        run: Run,
        current_step_name: str,
        fallback_steps: list[RunStep] | None = None,
    ) -> tuple[str | None, str]:
        """Determine the next step after current_step_name using graph authority when available.

        Returns (next_step_name_or_none, authority_source).

        Preconditions for graph authority:
        - active_scheduler == workflow_graph_scheduler_v1
        - execution_trial_eligible == true
        - current_step_name exists in graph linear read model
        """
        ordered_step_names, authority_source, _graph_authoritative = self._resolve_runtime_step_order_names(
            run=run,
            fallback_step_names=[step.step_name for step in (fallback_steps or [])],
        )
        current_index = next(
            (index for index, item in enumerate(ordered_step_names) if item == current_step_name),
            -1,
        )
        if current_index < 0 and fallback_steps:
            fallback_ordered_step_names = [step.step_name for step in fallback_steps]
            current_index = next(
                (index for index, item in enumerate(fallback_ordered_step_names) if item == current_step_name),
                -1,
            )
            if current_index >= 0 and current_index + 1 < len(fallback_ordered_step_names):
                return fallback_ordered_step_names[current_index + 1], "resolved_steps.compatibility_sequence"
        if current_index >= 0 and current_index + 1 < len(ordered_step_names):
            return ordered_step_names[current_index + 1], authority_source
        return None, authority_source

    def _reset_step_state(self, step: RunStep) -> None:
        step.status = RunStepStatus.PENDING.value
        step.started_at = None
        step.finished_at = None
        step.input_payload_json = None
        step.output_payload_json = None
        step.log_excerpt = None
        step.token_usage_json = None

    def _prepare_steps_for_requested_action(
        self,
        *,
        run: Run,
        steps: list[RunStep],
        requested_action: str,
        requested_step_name: str | None,
        resolved_plan: ResolvedExecutionPlan | None = None,
    ) -> None:
        if requested_action in {"start", "restart_run"}:
            for step in steps:
                if step.status != RunStepStatus.PENDING.value:
                    self._reset_step_state(step)
            return

        # Check if we have a parallel DAG context
        dag_model = self._rebuild_dag_model_for_recovery(run, resolved_plan)

        if dag_model is not None and dag_model.has_parallelism:
            self._prepare_steps_dag(
                steps=steps,
                dag_model=dag_model,
                requested_action=requested_action,
                requested_step_name=requested_step_name,
            )
            return

        # --- Linear fallback (original logic) ---
        ordered, _authority_source, _graph_authoritative = self._resolve_runtime_step_order(
            run=run,
            steps=steps,
        )

        if requested_action == "retry_step":
            target_name = requested_step_name or next((step.step_name for step in ordered if step.status == RunStepStatus.FAILED.value), None)
            matched = False
            for step in ordered:
                if step.step_name == target_name:
                    matched = True
                if matched:
                    self._reset_step_state(step)
            return

        if requested_action == "resume_run":
            matched_failed = False
            for step in ordered:
                if step.status == RunStepStatus.FAILED.value:
                    matched_failed = True
                if matched_failed:
                    self._reset_step_state(step)

    def _rebuild_dag_model_for_recovery(
        self,
        run: Run,
        resolved_plan: ResolvedExecutionPlan | None,
    ) -> DAGReadModel | None:
        """Rebuild DAGReadModel from resolved_plan or run snapshot for recovery operations."""
        from app.services.run.scheduler import DAGReadModel, WorkflowGraphScheduler

        # Try from resolved_plan first
        if resolved_plan is not None and isinstance(resolved_plan.workflow_artifact, dict):
            wg = resolved_plan.workflow_artifact.get("workflow_graph", {})
            if isinstance(wg, dict):
                return WorkflowGraphScheduler()._build_dag_read_model(workflow_graph=wg)

        # Fallback: try from run snapshot
        snapshot = run.resolved_config_snapshot_json or {}
        candidate = (snapshot.get("runtime_scheduler") or {}).get("workflow_graph_candidate") or {}
        dag_summary = candidate.get("dag_read_model_summary")
        if isinstance(dag_summary, dict) and dag_summary.get("has_parallelism"):
            # We have a summary but no full model — try to rebuild from workflow artifact in snapshot
            wa = snapshot.get("workflow_artifact") or {}
            wg = wa.get("workflow_graph") if isinstance(wa, dict) else None
            if isinstance(wg, dict):
                return WorkflowGraphScheduler()._build_dag_read_model(workflow_graph=wg)
        return None

    def _prepare_steps_dag(
        self,
        *,
        steps: list[RunStep],
        dag_model: DAGReadModel,
        requested_action: str,
        requested_step_name: str | None,
    ) -> None:
        """DAG-aware step preparation for retry/resume.

        Rules:
        - retry_step: reset ONLY the failed step + its dependency cone (downstream blocked nodes).
          Does NOT touch independent branches.
        - resume_run: does NOT auto-unblock blocked_by_failed_dependency nodes.
          Only resets steps that are in waiting_for_intervention or blocked_by_human_gate state.
          The DAG executor will recompute the ready set on re-entry.
        """
        steps_by_name = {step.step_name: step for step in steps}
        node_id_by_step = {v: k for k, v in dag_model.step_name_by_node.items()}

        if requested_action == "retry_step":
            # Find target failed step
            target_name = requested_step_name
            if not target_name:
                for nid in dag_model.topological_order:
                    sname = dag_model.step_name_by_node.get(nid, "")
                    if sname in steps_by_name and steps_by_name[sname].status == RunStepStatus.FAILED.value:
                        target_name = sname
                        break
            if not target_name or target_name not in steps_by_name:
                return

            target_node_id = node_id_by_step.get(target_name)
            if not target_node_id:
                # Not a DAG node — fall back to resetting just the target
                self._reset_step_state(steps_by_name[target_name])
                return

            # Reset the failed step itself
            self._reset_step_state(steps_by_name[target_name])

            # Reset its downstream dependency cone (blocked descendants only)
            cone = dag_model.dependency_cone(target_node_id)
            for cone_nid in cone:
                cone_step_name = dag_model.step_name_by_node.get(cone_nid, "")
                if cone_step_name in steps_by_name:
                    cone_step = steps_by_name[cone_step_name]
                    # Only reset if the step was blocked or failed due to this branch
                    if cone_step.status in (RunStepStatus.FAILED.value, RunStepStatus.PENDING.value):
                        self._reset_step_state(cone_step)
                    elif cone_step.status != RunStepStatus.COMPLETED.value:
                        self._reset_step_state(cone_step)
            return

        if requested_action == "resume_run":
            # resume_run in DAG context: do NOT auto-unblock failed-dependency nodes.
            # Only reset steps that are in an intervention/gate-blocked state.
            # The DAG executor will recompute ready_set from completed/failed state.
            for step in steps:
                if step.status == RunStepStatus.FAILED.value:
                    # Check if this was an intervention-resumable failure
                    op_state = (step.output_payload_json or {}).get("operation_state", {})
                    if isinstance(op_state, dict) and op_state.get("operation_state") == "waiting_for_intervention":
                        self._reset_step_state(step)
                    # Otherwise: do NOT reset failed steps — user must use retry_step
            return

    def _recovery_options(self, *, restart_only: bool, include_resume: bool, include_rerun: bool) -> list[dict[str, Any]]:
        options: list[RuntimeRecoveryOptionResponse] = []
        if include_resume:
            options.append(RuntimeRecoveryOptionResponse(action="resume_run", label="Resume run"))
        if not restart_only and include_rerun:
            options.append(RuntimeRecoveryOptionResponse(action="retry_step", label="Retry failed step"))
        options.append(RuntimeRecoveryOptionResponse(action="restart_run", label="Restart run"))
        options.append(RuntimeRecoveryOptionResponse(action="abort_run", label="Abort run"))
        return [item.model_dump(mode="python") for item in options]

    def _build_waiting_for_intervention_state(
        self,
        *,
        run: Run,
        step_name: str | None,
        blocked_reason: str,
        intervention_reason: str,
        last_successful_step: str | None,
        pending_step: str | None,
        authority_source: str,
        intervention_legality_authority_source: str = "runtime.compatibility_default.intervention_legality",
        resume_supported: bool | None = None,
        rerun_supported: bool | None = None,
    ) -> dict[str, Any]:
        effective_resume_supported = (last_successful_step is not None) if resume_supported is None else resume_supported
        effective_rerun_supported = (step_name is not None) if rerun_supported is None else rerun_supported
        recovery_options = self._recovery_options(
            restart_only=step_name is None,
            include_resume=effective_resume_supported,
            include_rerun=effective_rerun_supported,
        )
        runtime_scheduler = dict((run.resolved_config_snapshot_json or {}).get("runtime_scheduler") or {})
        scheduler_kind = runtime_scheduler.get("active_scheduler")
        scheduler_authority_source = runtime_scheduler.get("active_scheduler_authority_source")
        pending_step_authority_source = runtime_scheduler.get("pending_step_authority_source")
        return {
            "run_id": run.id,
            "operation_state": "waiting_for_intervention",
            "scheduler_kind": scheduler_kind,
            "scheduler_authority_source": scheduler_authority_source,
            "confirmation_required": False,
            "confirmation_request": None,
            "intervention_required": True,
            "intervention_legality_authority_source": intervention_legality_authority_source,
            "intervention_state": {
                "run_id": run.id,
                "step_id": None,
                "step_name": step_name,
                "intervention_required": True,
                "intervention_reason": intervention_reason,
                "authority_source": authority_source,
                "intervention_legality_authority_source": intervention_legality_authority_source,
                "blocked_reason": blocked_reason,
                "recovery_options": recovery_options,
                "requested_at": self._timestamp(),
            },
            "resume_supported": effective_resume_supported,
            "rerun_supported": effective_rerun_supported,
            "rerun_authority_source": "runtime.compatibility_intervention_step_presence" if step_name is not None else "runtime.compatibility_no_current_step",
            "rerun_requires_human_approval": False,
            "last_successful_step": last_successful_step,
            "pending_step": pending_step or step_name,
            "pending_step_authority_source": pending_step_authority_source,
            "blocked_reason": blocked_reason,
            "recovery_summary": {
                "run_id": run.id,
                "operation_state": "waiting_for_intervention",
                "last_successful_step": last_successful_step,
                "pending_step": pending_step or step_name,
                "pending_step_authority_source": pending_step_authority_source,
                "recovery_continuation_step": None,
                "recovery_continuation_authority_source": None,
                "blocked_reason": blocked_reason,
                "recovery_options": recovery_options,
                "updated_at": self._timestamp(),
            },
            "updated_at": self._timestamp(),
        }
