from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from app.core.enums import RunStatus, RunStepStatus
from app.core.logging import log_event
from app.db.models import Run, RunStep
from app.db.models.entities import Artifact, TokenUsageLog
from app.schemas.run import RuntimeRecoveryOptionResponse

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from app.db.repositories.run_step_repository import RunStepRepository
    from app.repositories.artifact_repository import ArtifactRepository
    from app.repositories.token_usage_log_repository import TokenUsageLogRepository
    from app.repositories.model_config_repository import ModelConfigRepository
    from app.schemas.executable_manifest import ResolvedExecutionPlan
    from app.services.run.run_execution_service import RunExecutionService


@dataclass
class SchedulerCurrentStepTracker:
    current_step: Optional[RunStep] = None


@dataclass
class SchedulerExecutionContext:
    db: Session
    run: Run
    steps: list[RunStep]
    blueprint_type: str
    runtime_profile: Any
    resolved_plan: ResolvedExecutionPlan | None
    execution_mode: str
    approved_step_names: set[str]
    artifact_repository: ArtifactRepository
    token_usage_log_repository: TokenUsageLogRepository
    model_config_repository: ModelConfigRepository
    run_step_repository: RunStepRepository
    scheduler_kind: str
    scheduler_authority_source: str
    pending_step_authority_source: str


@dataclass
class SchedulerRunResult:
    terminal_state: str
    accumulated_outputs: list[dict[str, Any]] = field(default_factory=list)
    outputs_by_step: dict[str, dict[str, Any]] = field(default_factory=dict)
    approved_step_names: set[str] = field(default_factory=set)


@dataclass
class DAGNodeExecutionResult:
    """Result from executing a single node in a worker thread."""

    node_id: str
    step_name: str
    success: bool
    output_payload: dict[str, Any]
    provider_response: Any  # ProviderResponse | None
    extra_artifacts: list[dict[str, Any]]
    error_message: str | None = None


def _run_steps_in_order(
    *,
    service: RunExecutionService,
    context: SchedulerExecutionContext,
    tracker: SchedulerCurrentStepTracker,
    logger,
    ordered_steps: list[RunStep],
) -> SchedulerRunResult:
    accumulated_outputs: list[dict[str, Any]] = []
    outputs_by_step: dict[str, dict[str, Any]] = {}
    for preserved_step in context.steps:
        if preserved_step.status == RunStepStatus.COMPLETED.value and isinstance(
            preserved_step.output_payload_json,
            dict,
        ):
            accumulated_outputs.append(preserved_step.output_payload_json)
            outputs_by_step[preserved_step.step_name] = preserved_step.output_payload_json

    for step in ordered_steps:
        tracker.current_step = step
        if step.status == RunStepStatus.COMPLETED.value:
            continue
        context.db.refresh(context.run)
        if context.run.status == RunStatus.CANCELLED.value:
            service._mark_step_cancelled(  # noqa: SLF001
                step,
                scheduler_kind=context.scheduler_kind,
                scheduler_authority_source=context.scheduler_authority_source,
                pending_step_authority_source=context.pending_step_authority_source,
            )
            context.db.commit()
            log_event(logger, logging.INFO, "run.cancelled.mid_execution", run_id=context.run.id, step_id=step.id)
            return SchedulerRunResult(
                terminal_state="cancelled",
                accumulated_outputs=accumulated_outputs,
                outputs_by_step=outputs_by_step,
                approved_step_names=set(context.approved_step_names),
            )

        step_definition = service._step_definition_for_name(context.resolved_plan, step.step_name)  # noqa: SLF001
        requires_human_gate, gate_authority_source = service._step_requires_human_gate(  # noqa: SLF001
            resolved_plan=context.resolved_plan,
            step_name=step.step_name,
            step_definition=step_definition,
        )
        if requires_human_gate and step.step_name not in context.approved_step_names:
            runtime_operation = service._pause_for_confirmation(  # noqa: SLF001
                db=context.db,
                run=context.run,
                step=step,
                step_definition=step_definition,
                artifact_repository=context.artifact_repository,
                execution_mode=context.execution_mode,
                last_successful_step=accumulated_outputs[-1].get("step_name") if accumulated_outputs else None,
                authority_source=gate_authority_source or "resolved_steps.compatibility_step_definition",
            )
            approved_step_names = {
                str(item) for item in runtime_operation.get("approved_step_names", []) if str(item)
            }
            return SchedulerRunResult(
                terminal_state="paused_for_confirmation",
                accumulated_outputs=accumulated_outputs,
                outputs_by_step=outputs_by_step,
                approved_step_names=approved_step_names,
            )

        step.status = RunStepStatus.RUNNING.value
        step.started_at = datetime.now(timezone.utc)
        step.input_payload_json = service._build_step_input(  # noqa: SLF001
            context.run,
            context.runtime_profile,
            step,
            accumulated_outputs,
            context.resolved_plan,
        )
        step.log_excerpt = f"[started] {step.step_name}"
        context.db.commit()
        log_event(
            logger,
            logging.INFO,
            "run.step.started",
            run_id=context.run.id,
            step_id=step.id,
            step_name=step.step_name,
            step_order=step.step_order,
            execution_mode=context.execution_mode,
        )

        output_payload, provider_response, extra_artifacts = service._execute_step(  # noqa: SLF001
            run=context.run,
            blueprint_type=context.blueprint_type,
            step=step,
            runtime_profile=context.runtime_profile,
            model_config_repository=context.model_config_repository,
            execution_mode=context.execution_mode,
            previous_outputs_by_step=outputs_by_step,
        )
        token_usage = service._token_usage_dict(provider_response, step.input_payload_json or {}, output_payload)  # noqa: SLF001

        step.output_payload_json = output_payload
        graph_pending_step, graph_pending_authority = service._graph_aware_next_pending_step(  # noqa: SLF001
            run=context.run,
            current_step_name=step.step_name,
            fallback_steps=ordered_steps,
        )
        checkpoint = service._build_checkpoint(  # noqa: SLF001
            run=context.run,
            completed_step_names=[*outputs_by_step.keys(), step.step_name],
            last_successful_step=step.step_name,
            pending_step=graph_pending_step,
            pending_step_authority_source=graph_pending_authority,
            scheduler_kind=context.scheduler_kind,
            scheduler_authority_source=context.scheduler_authority_source,
        )
        step.output_payload_json["operation_state"] = {
            "step_name": step.step_name,
            "operation_state": "completed",
            "scheduler_kind": context.scheduler_kind,
            "scheduler_authority_source": context.scheduler_authority_source,
            "checkpoint_id": checkpoint["checkpoint_id"],
            "resume_supported": True,
            "rerun_supported": True,
            "pending_step_authority_source": graph_pending_authority,
        }
        step.token_usage_json = token_usage
        step.log_excerpt = (
            f"[started] {step.step_name}\n"
            f"[completed] {output_payload.get('summary', 'step completed')}"
        )
        step.finished_at = datetime.now(timezone.utc)
        step.status = RunStepStatus.COMPLETED.value
        service._apply_runtime_operation(  # noqa: SLF001
            run=context.run,
            patch={
                "operation_state": "partially_completed",
                "scheduler_kind": context.scheduler_kind,
                "scheduler_authority_source": context.scheduler_authority_source,
                "last_successful_step": step.step_name,
                "pending_step": graph_pending_step,
                "pending_step_authority_source": graph_pending_authority,
                "resume_supported": True,
                "rerun_supported": True,
                "latest_checkpoint": checkpoint,
                "recovery_summary": {
                    "run_id": context.run.id,
                    "operation_state": "partially_completed",
                    "last_successful_step": step.step_name,
                    "pending_step": graph_pending_step,
                    "pending_step_authority_source": graph_pending_authority,
                    "recovery_continuation_step": graph_pending_step,
                    "recovery_continuation_authority_source": graph_pending_authority,
                    "blocked_reason": None,
                    "recovery_options": service._recovery_options(  # noqa: SLF001
                        restart_only=False,
                        include_resume=True,
                        include_rerun=True,
                    ),
                    "updated_at": service._timestamp(),  # noqa: SLF001
                },
                "updated_at": service._timestamp(),  # noqa: SLF001
            },
        )
        context.db.commit()
        accumulated_outputs.append(output_payload)
        outputs_by_step[step.step_name] = output_payload

        context.artifact_repository.create(
            Artifact(
                build_task_id=None,
                build_step_id=None,
                run_id=context.run.id,
                run_step_id=step.id,
                executable_id=context.run.executable_id,
                artifact_type=f"{step.step_name}_output",
                storage_kind="inline",
                path_or_content=output_payload.get("content") or output_payload.get("summary") or "",
                metadata_json={
                    "step_name": step.step_name,
                    "execution_mode": context.execution_mode,
                    "status": output_payload.get("status"),
                    "step_type": output_payload.get("step_type"),
                    "selected_capability_id": output_payload.get("selected_capability_id"),
                    "runtime_adapter": output_payload.get("runtime_adapter"),
                    "handler_ref": output_payload.get("handler_ref"),
                    "fallback_used": output_payload.get("fallback_used", False),
                    "failure_class": output_payload.get("failure_class"),
                    "retry_attempts": output_payload.get("retry_attempts", 0),
                },
            )
        )
        iteration_trace = output_payload.get("iteration_trace")
        if isinstance(iteration_trace, list) and iteration_trace:
            context.artifact_repository.create(
                Artifact(
                    build_task_id=None,
                    build_step_id=None,
                    build_id=None,
                    build_step_v2_id=None,
                    run_id=context.run.id,
                    run_step_id=step.id,
                    executable_id=context.run.executable_id,
                    artifact_type="runtime_node_loop_trace",
                    storage_kind="inline",
                    path_or_content=json.dumps(iteration_trace, ensure_ascii=False, indent=2),
                    metadata_json={
                        "step_name": step.step_name,
                        "execution_mode": context.execution_mode,
                        "execution_mode_detail": output_payload.get("execution_mode_detail"),
                        "iteration_count": output_payload.get("iteration_count", len(iteration_trace)),
                        "budget_exhausted": output_payload.get("budget_exhausted", False),
                    },
                )
            )
        for artifact_definition in extra_artifacts:
            context.artifact_repository.create(
                Artifact(
                    build_task_id=None,
                    build_step_id=None,
                    build_id=None,
                    build_step_v2_id=None,
                    run_id=context.run.id,
                    run_step_id=step.id,
                    executable_id=context.run.executable_id,
                    artifact_type=artifact_definition["artifact_type"],
                    storage_kind="inline",
                    path_or_content=artifact_definition["stored_content"],
                    metadata_json=artifact_definition["metadata_json"],
                )
            )
        context.token_usage_log_repository.create(
            TokenUsageLog(
                build_task_id=None,
                build_step_id=None,
                build_id=None,
                build_step_v2_id=None,
                run_id=context.run.id,
                run_step_id=step.id,
                provider=token_usage["provider"],
                model_name=token_usage["model_name"],
                prompt_tokens=token_usage["prompt_tokens"],
                completion_tokens=token_usage["completion_tokens"],
                total_tokens=token_usage["total_tokens"],
                cost_estimate=token_usage["cost_estimate"],
            )
        )
        context.db.commit()
        service._persist_run_artifact(  # noqa: SLF001
            artifact_repository=context.artifact_repository,
            run=context.run,
            executable_id=context.run.executable_id,
            artifact_type="runtime_checkpoint",
            content=json.dumps(checkpoint, ensure_ascii=False, indent=2),
            metadata={
                "execution_mode": context.execution_mode,
                "checkpoint_id": checkpoint["checkpoint_id"],
                "last_successful_step": checkpoint["last_successful_step"],
            },
            run_step_id=step.id,
        )
        context.db.commit()
        log_event(
            logger,
            logging.INFO,
            "run.step.completed",
            run_id=context.run.id,
            step_id=step.id,
            step_name=step.step_name,
            total_tokens=token_usage["total_tokens"],
            execution_mode=context.execution_mode,
        )
        time.sleep(service.delay_seconds)

    return SchedulerRunResult(
        terminal_state="completed",
        accumulated_outputs=accumulated_outputs,
        outputs_by_step=outputs_by_step,
        approved_step_names=set(context.approved_step_names),
    )


def _execute_node_worker(
    *,
    service: RunExecutionService,
    run: Run,
    blueprint_type: str,
    step: RunStep,
    runtime_profile: Any,
    execution_mode: str,
    previous_outputs_by_step: dict[str, dict[str, Any]],
    node_id: str,
) -> DAGNodeExecutionResult:
    """Execute a single node in a worker thread.

    Creates its own DB session for model config lookups.
    All other data is accessed read-only from pre-loaded ORM attributes.
    The caller (coordinator thread) handles all state mutations.
    """
    from app.core.database import SessionLocal
    from app.repositories.model_config_repository import ModelConfigRepository

    thread_db = SessionLocal()
    try:
        thread_model_config_repo = ModelConfigRepository(thread_db)
        output_payload, provider_response, extra_artifacts = service._execute_step(  # noqa: SLF001
            run=run,
            blueprint_type=blueprint_type,
            step=step,
            runtime_profile=runtime_profile,
            model_config_repository=thread_model_config_repo,
            execution_mode=execution_mode,
            previous_outputs_by_step=previous_outputs_by_step,
        )
        return DAGNodeExecutionResult(
            node_id=node_id,
            step_name=step.step_name,
            success=True,
            output_payload=output_payload,
            provider_response=provider_response,
            extra_artifacts=extra_artifacts,
        )
    except Exception as exc:
        return DAGNodeExecutionResult(
            node_id=node_id,
            step_name=step.step_name,
            success=False,
            output_payload={
                "step_name": step.step_name,
                "status": "failed",
                "summary": f"Node execution failed: {exc}",
                "failure_class": "execution_error",
                "failure_detail": str(exc),
                "fallback_used": False,
                "retry_attempts": 0,
            },
            provider_response=None,
            extra_artifacts=[],
            error_message=str(exc),
        )
    finally:
        thread_db.close()


def _run_steps_dag(
    *,
    service: RunExecutionService,
    context: SchedulerExecutionContext,
    tracker: SchedulerCurrentStepTracker,
    logger,
    dag_model: DAGReadModel,
) -> SchedulerRunResult:
    """Execute steps according to DAG dependencies with parallel fan-out.

    Design rules:
    - Parallel execution only for node execution (ThreadPoolExecutor)
    - All state mutations (step status, checkpoint, run state) on coordinator thread
    - blocked carries reason_class: blocked_by_failed_dependency or blocked_by_human_gate
    - Strict all-success join: fan-in node runs only when ALL predecessors complete
    - execution_round is scheduler dispatch wave id, not attempt count
    - Independent branches continue when a sibling branch fails
    - resume_run does NOT auto-unblock blocked_by_failed_dependency nodes
    """
    steps_by_name: dict[str, RunStep] = {step.step_name: step for step in context.steps}
    node_id_by_step: dict[str, str] = {v: k for k, v in dag_model.step_name_by_node.items()}

    # Track state — all mutations on coordinator thread only
    completed_node_ids: set[str] = set()
    failed_node_ids: set[str] = set()
    accumulated_outputs: list[dict[str, Any]] = []
    outputs_by_step: dict[str, dict[str, Any]] = {}
    execution_round = 0

    # Pre-populate from already-completed/failed steps (resume support)
    for step in context.steps:
        node_id = node_id_by_step.get(step.step_name)
        if not node_id:
            continue
        if step.status == RunStepStatus.COMPLETED.value and isinstance(step.output_payload_json, dict):
            completed_node_ids.add(node_id)
            accumulated_outputs.append(step.output_payload_json)
            outputs_by_step[step.step_name] = step.output_payload_json
        elif step.status == RunStepStatus.FAILED.value:
            failed_node_ids.add(node_id)

    max_workers = min(4, max(dag_model.max_fan_out, 1))

    while True:
        # --- Check cancellation ---
        context.db.refresh(context.run)
        if context.run.status == RunStatus.CANCELLED.value:
            for step in context.steps:
                if step.status not in (RunStepStatus.COMPLETED.value, RunStepStatus.FAILED.value):
                    service._mark_step_cancelled(  # noqa: SLF001
                        step,
                        scheduler_kind=context.scheduler_kind,
                        scheduler_authority_source=context.scheduler_authority_source,
                        pending_step_authority_source=context.pending_step_authority_source,
                    )
            context.db.commit()
            return SchedulerRunResult(
                terminal_state="cancelled",
                accumulated_outputs=accumulated_outputs,
                outputs_by_step=outputs_by_step,
                approved_step_names=set(context.approved_step_names),
            )

        # --- Compute ready set and blocked set ---
        ready_node_ids = dag_model.ready_set(completed_node_ids, failed_node_ids)
        blocked_node_ids = dag_model.blocked_set(failed_node_ids)

        if not ready_node_ids:
            all_node_ids = set(dag_model.node_ids)
            if completed_node_ids >= all_node_ids:
                return SchedulerRunResult(
                    terminal_state="completed",
                    accumulated_outputs=accumulated_outputs,
                    outputs_by_step=outputs_by_step,
                    approved_step_names=set(context.approved_step_names),
                )
            # DAG is stuck — some nodes failed, rest blocked.
            # Set tracker to first failed step so caller's failure handler works.
            for nid in dag_model.topological_order:
                if nid in failed_node_ids:
                    sname = dag_model.step_name_by_node[nid]
                    if sname in steps_by_name:
                        tracker.current_step = steps_by_name[sname]
                        break
            failed_names = sorted(dag_model.step_name_by_node[n] for n in failed_node_ids)
            blocked_names = sorted(dag_model.step_name_by_node[n] for n in blocked_node_ids if n in dag_model.step_name_by_node)
            raise RuntimeError(
                f"DAG execution incomplete: {len(failed_node_ids)} failed, {len(blocked_node_ids)} blocked. "
                f"Failed: [{', '.join(failed_names)}]. Blocked: [{', '.join(blocked_names)}]"
            )

        execution_round += 1

        # --- Partition ready nodes: can_execute vs needs_gate ---
        can_execute: list[str] = []
        needs_gate: list[str] = []

        for node_id in sorted(ready_node_ids):
            step_name = dag_model.step_name_by_node[node_id]
            step = steps_by_name[step_name]
            step_definition = service._step_definition_for_name(context.resolved_plan, step.step_name)  # noqa: SLF001
            requires_gate, _gate_authority = service._step_requires_human_gate(  # noqa: SLF001
                resolved_plan=context.resolved_plan,
                step_name=step.step_name,
                step_definition=step_definition,
            )
            if requires_gate and step.step_name not in context.approved_step_names:
                needs_gate.append(node_id)
            else:
                can_execute.append(node_id)

        if not can_execute:
            # All ready nodes need human gate — pause for the first one
            first_gated_id = needs_gate[0]
            gated_step_name = dag_model.step_name_by_node[first_gated_id]
            gated_step = steps_by_name[gated_step_name]
            gated_step_def = service._step_definition_for_name(context.resolved_plan, gated_step.step_name)  # noqa: SLF001
            runtime_operation = service._pause_for_confirmation(  # noqa: SLF001
                db=context.db,
                run=context.run,
                step=gated_step,
                step_definition=gated_step_def,
                artifact_repository=context.artifact_repository,
                execution_mode=context.execution_mode,
                last_successful_step=accumulated_outputs[-1].get("step_name") if accumulated_outputs else None,
                authority_source="workflow_graph.dag_human_gate",
            )
            approved_step_names = {
                str(item) for item in runtime_operation.get("approved_step_names", []) if str(item)
            }
            return SchedulerRunResult(
                terminal_state="paused_for_confirmation",
                accumulated_outputs=accumulated_outputs,
                outputs_by_step=outputs_by_step,
                approved_step_names=approved_step_names,
            )

        # --- Mark all executable nodes as RUNNING (coordinator thread) ---
        for node_id in can_execute:
            step_name = dag_model.step_name_by_node[node_id]
            step = steps_by_name[step_name]
            step.status = RunStepStatus.RUNNING.value
            step.started_at = datetime.now(timezone.utc)
            step.input_payload_json = service._build_step_input(  # noqa: SLF001
                context.run,
                context.runtime_profile,
                step,
                accumulated_outputs,
                context.resolved_plan,
            )
            step.log_excerpt = f"[started] {step.step_name}"
        context.db.commit()

        # --- Update run-level state to show round dispatch ---
        running_step_names = [dag_model.step_name_by_node[nid] for nid in can_execute]
        service._apply_runtime_operation(  # noqa: SLF001
            run=context.run,
            patch={
                "operation_state": "running",
                "scheduler_kind": context.scheduler_kind,
                "scheduler_authority_source": context.scheduler_authority_source,
                "running_steps": running_step_names,
                "execution_round": execution_round,
                "graph_execution_kind": "dag_parallel" if len(can_execute) > 1 else "dag_sequential",
                "updated_at": service._timestamp(),  # noqa: SLF001
            },
        )
        context.db.commit()
        log_event(
            logger,
            logging.INFO,
            "run.dag.round.started",
            run_id=context.run.id,
            execution_round=execution_round,
            dispatching_nodes=len(can_execute),
            ready_nodes=len(ready_node_ids),
            completed_nodes=len(completed_node_ids),
            failed_nodes=len(failed_node_ids),
        )

        # --- Dispatch nodes (ThreadPoolExecutor for parallel, direct for single) ---
        results: list[DAGNodeExecutionResult] = []
        outputs_snapshot = dict(outputs_by_step)  # snapshot for thread safety

        if len(can_execute) == 1:
            node_id = can_execute[0]
            step = steps_by_name[dag_model.step_name_by_node[node_id]]
            tracker.current_step = step
            results.append(
                _execute_node_worker(
                    service=service,
                    run=context.run,
                    blueprint_type=context.blueprint_type,
                    step=step,
                    runtime_profile=context.runtime_profile,
                    execution_mode=context.execution_mode,
                    previous_outputs_by_step=outputs_snapshot,
                    node_id=node_id,
                )
            )
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {}
                for node_id in can_execute:
                    step = steps_by_name[dag_model.step_name_by_node[node_id]]
                    tracker.current_step = step
                    future = executor.submit(
                        _execute_node_worker,
                        service=service,
                        run=context.run,
                        blueprint_type=context.blueprint_type,
                        step=step,
                        runtime_profile=context.runtime_profile,
                        execution_mode=context.execution_mode,
                        previous_outputs_by_step=outputs_snapshot,
                        node_id=node_id,
                    )
                    futures[future] = node_id
                for future in as_completed(futures):
                    results.append(future.result())

        # --- Process results (coordinator thread — all state mutations) ---
        for result in results:
            step = steps_by_name[result.step_name]
            tracker.current_step = step

            if result.success:
                completed_node_ids.add(result.node_id)
                step.output_payload_json = result.output_payload
                token_usage = service._token_usage_dict(  # noqa: SLF001
                    result.provider_response,
                    step.input_payload_json or {},
                    result.output_payload,
                )
                step.token_usage_json = token_usage

                # Build DAG-aware checkpoint
                completed_step_names = [
                    dag_model.step_name_by_node[nid]
                    for nid in dag_model.topological_order
                    if nid in completed_node_ids
                ]
                ready_after = dag_model.ready_set(completed_node_ids, failed_node_ids)
                blocked_after = dag_model.blocked_set(failed_node_ids)
                pending_step_names = [
                    dag_model.step_name_by_node[nid]
                    for nid in dag_model.topological_order
                    if nid in ready_after
                ]

                checkpoint = service._build_checkpoint(  # noqa: SLF001
                    run=context.run,
                    completed_step_names=completed_step_names,
                    last_successful_step=step.step_name,
                    pending_step=pending_step_names[0] if pending_step_names else None,
                    pending_step_authority_source="workflow_artifact.workflow_graph.dag_ready_set",
                    scheduler_kind=context.scheduler_kind,
                    scheduler_authority_source=context.scheduler_authority_source,
                )
                # Augment checkpoint with DAG fields
                checkpoint["pending_steps"] = pending_step_names
                checkpoint["ready_steps"] = [dag_model.step_name_by_node[nid] for nid in sorted(ready_after)]
                checkpoint["running_steps"] = []
                checkpoint["blocked_step_names"] = [
                    dag_model.step_name_by_node[nid] for nid in sorted(blocked_after) if nid in dag_model.step_name_by_node
                ]
                checkpoint["failed_step_names"] = [
                    dag_model.step_name_by_node[nid] for nid in sorted(failed_node_ids)
                ]
                checkpoint["execution_round"] = execution_round
                checkpoint["graph_execution_kind"] = "dag_parallel" if len(can_execute) > 1 else "dag_sequential"

                step.output_payload_json["operation_state"] = {
                    "step_name": step.step_name,
                    "operation_state": "completed",
                    "scheduler_kind": context.scheduler_kind,
                    "scheduler_authority_source": context.scheduler_authority_source,
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "resume_supported": True,
                    "rerun_supported": True,
                    "pending_step_authority_source": "workflow_artifact.workflow_graph.dag_ready_set",
                    "execution_round": execution_round,
                }
                step.log_excerpt = (
                    f"[started] {step.step_name}\n"
                    f"[completed] {result.output_payload.get('summary', 'step completed')}"
                )
                step.finished_at = datetime.now(timezone.utc)
                step.status = RunStepStatus.COMPLETED.value

                # Apply run-level operation
                ready_step_names_after = [dag_model.step_name_by_node[nid] for nid in sorted(ready_after)]
                blocked_step_names_after = [
                    dag_model.step_name_by_node[nid] for nid in sorted(blocked_after) if nid in dag_model.step_name_by_node
                ]
                failed_step_names_snap = [dag_model.step_name_by_node[nid] for nid in sorted(failed_node_ids)]
                service._apply_runtime_operation(  # noqa: SLF001
                    run=context.run,
                    patch={
                        "operation_state": "partially_completed",
                        "scheduler_kind": context.scheduler_kind,
                        "scheduler_authority_source": context.scheduler_authority_source,
                        "last_successful_step": step.step_name,
                        "pending_step": pending_step_names[0] if pending_step_names else None,
                        "pending_steps": pending_step_names,
                        "pending_step_authority_source": "workflow_artifact.workflow_graph.dag_ready_set",
                        "resume_supported": True,
                        "rerun_supported": True,
                        "latest_checkpoint": checkpoint,
                        "ready_steps": ready_step_names_after,
                        "blocked_step_names": blocked_step_names_after,
                        "failed_step_names": failed_step_names_snap,
                        "execution_round": execution_round,
                        "graph_execution_kind": "dag_parallel" if len(can_execute) > 1 else "dag_sequential",
                        "recovery_summary": {
                            "run_id": context.run.id,
                            "operation_state": "partially_completed",
                            "last_successful_step": step.step_name,
                            "pending_step": pending_step_names[0] if pending_step_names else None,
                            "pending_step_authority_source": "workflow_artifact.workflow_graph.dag_ready_set",
                            "recovery_continuation_step": pending_step_names[0] if pending_step_names else None,
                            "recovery_continuation_authority_source": "workflow_artifact.workflow_graph.dag_ready_set",
                            "blocked_reason": None,
                            "blocked_step_names": blocked_step_names_after,
                            "retryable_step_names": failed_step_names_snap,
                            "recovery_options": service._recovery_options(  # noqa: SLF001
                                restart_only=False,
                                include_resume=True,
                                include_rerun=True,
                            ),
                            "updated_at": service._timestamp(),  # noqa: SLF001
                        },
                        "updated_at": service._timestamp(),  # noqa: SLF001
                    },
                )
                context.db.commit()
                accumulated_outputs.append(result.output_payload)
                outputs_by_step[step.step_name] = result.output_payload

                # Persist step output artifact
                context.artifact_repository.create(
                    Artifact(
                        build_task_id=None,
                        build_step_id=None,
                        run_id=context.run.id,
                        run_step_id=step.id,
                        executable_id=context.run.executable_id,
                        artifact_type=f"{step.step_name}_output",
                        storage_kind="inline",
                        path_or_content=result.output_payload.get("content") or result.output_payload.get("summary") or "",
                        metadata_json={
                            "step_name": step.step_name,
                            "execution_mode": context.execution_mode,
                            "status": result.output_payload.get("status"),
                            "execution_round": execution_round,
                        },
                    )
                )
                # Persist iteration trace if present (agentic loop)
                iteration_trace = result.output_payload.get("iteration_trace")
                if isinstance(iteration_trace, list) and iteration_trace:
                    context.artifact_repository.create(
                        Artifact(
                            build_task_id=None,
                            build_step_id=None,
                            build_id=None,
                            build_step_v2_id=None,
                            run_id=context.run.id,
                            run_step_id=step.id,
                            executable_id=context.run.executable_id,
                            artifact_type="runtime_node_loop_trace",
                            storage_kind="inline",
                            path_or_content=json.dumps(iteration_trace, ensure_ascii=False, indent=2),
                            metadata_json={
                                "step_name": step.step_name,
                                "execution_mode": context.execution_mode,
                                "execution_round": execution_round,
                                "iteration_count": result.output_payload.get("iteration_count", len(iteration_trace)),
                                "budget_exhausted": result.output_payload.get("budget_exhausted", False),
                            },
                        )
                    )
                for art_def in result.extra_artifacts:
                    context.artifact_repository.create(
                        Artifact(
                            build_task_id=None,
                            build_step_id=None,
                            build_id=None,
                            build_step_v2_id=None,
                            run_id=context.run.id,
                            run_step_id=step.id,
                            executable_id=context.run.executable_id,
                            artifact_type=art_def["artifact_type"],
                            storage_kind="inline",
                            path_or_content=art_def["stored_content"],
                            metadata_json=art_def["metadata_json"],
                        )
                    )
                context.token_usage_log_repository.create(
                    TokenUsageLog(
                        build_task_id=None,
                        build_step_id=None,
                        build_id=None,
                        build_step_v2_id=None,
                        run_id=context.run.id,
                        run_step_id=step.id,
                        provider=token_usage["provider"],
                        model_name=token_usage["model_name"],
                        prompt_tokens=token_usage["prompt_tokens"],
                        completion_tokens=token_usage["completion_tokens"],
                        total_tokens=token_usage["total_tokens"],
                        cost_estimate=token_usage["cost_estimate"],
                    )
                )
                context.db.commit()
                service._persist_run_artifact(  # noqa: SLF001
                    artifact_repository=context.artifact_repository,
                    run=context.run,
                    executable_id=context.run.executable_id,
                    artifact_type="runtime_checkpoint",
                    content=json.dumps(checkpoint, ensure_ascii=False, indent=2),
                    metadata={
                        "execution_mode": context.execution_mode,
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "last_successful_step": checkpoint["last_successful_step"],
                        "execution_round": execution_round,
                    },
                    run_step_id=step.id,
                )
                context.db.commit()
                log_event(
                    logger,
                    logging.INFO,
                    "run.dag.step.completed",
                    run_id=context.run.id,
                    step_id=step.id,
                    step_name=step.step_name,
                    execution_round=execution_round,
                    total_tokens=token_usage["total_tokens"],
                )
            else:
                # --- Node failed ---
                failed_node_ids.add(result.node_id)
                step.output_payload_json = result.output_payload
                step.output_payload_json["operation_state"] = {
                    "step_name": step.step_name,
                    "operation_state": "failed_terminal",
                    "scheduler_kind": context.scheduler_kind,
                    "scheduler_authority_source": context.scheduler_authority_source,
                    "execution_round": execution_round,
                    "blocked_reason_class": None,
                }
                step.status = RunStepStatus.FAILED.value
                step.finished_at = datetime.now(timezone.utc)
                step.log_excerpt = (
                    f"[started] {step.step_name}\n"
                    f"[failed] {result.error_message or 'unknown error'}"
                )
                context.db.commit()
                log_event(
                    logger,
                    logging.ERROR,
                    "run.dag.step.failed",
                    run_id=context.run.id,
                    step_id=step.id,
                    step_name=step.step_name,
                    execution_round=execution_round,
                    error=result.error_message,
                )

                # Mark newly blocked descendants with reason_class
                newly_blocked = dag_model.blocked_set(failed_node_ids) - blocked_node_ids
                for blocked_nid in newly_blocked:
                    blocked_step_name = dag_model.step_name_by_node.get(blocked_nid)
                    if blocked_step_name and blocked_step_name in steps_by_name:
                        blocked_step = steps_by_name[blocked_step_name]
                        if blocked_step.status == RunStepStatus.PENDING.value:
                            blocked_step.output_payload_json = {
                                "step_name": blocked_step_name,
                                "status": "blocked",
                                "blocked_reason_class": "blocked_by_failed_dependency",
                                "blocked_by_step": result.step_name,
                                "operation_state": {
                                    "step_name": blocked_step_name,
                                    "operation_state": "blocked",
                                    "blocked_reason_class": "blocked_by_failed_dependency",
                                    "blocked_by_step": result.step_name,
                                    "scheduler_kind": context.scheduler_kind,
                                    "execution_round": execution_round,
                                },
                            }
                context.db.commit()

        time.sleep(service.delay_seconds)


class CompatibilityStepScheduler:
    scheduler_kind = "compatibility_step_scheduler"

    def run(
        self,
        *,
        service: RunExecutionService,
        context: SchedulerExecutionContext,
        tracker: SchedulerCurrentStepTracker,
        logger,
    ) -> SchedulerRunResult:
        return _run_steps_in_order(
            service=service,
            context=context,
            tracker=tracker,
            logger=logger,
            ordered_steps=context.steps,
        )


@dataclass
class DAGReadModel:
    """Read model for a validated DAG workflow graph.

    This is the general case. Linear graphs are a valid subset where
    max_fan_out == 1 and max_fan_in == 1.
    """

    read_model_kind: str = "dag_workflow_graph_v1"
    node_ids: list[str] = field(default_factory=list)
    step_name_by_node: dict[str, str] = field(default_factory=dict)
    spec_ref_by_node: dict[str, str] = field(default_factory=dict)
    predecessors: dict[str, set[str]] = field(default_factory=dict)
    successors: dict[str, set[str]] = field(default_factory=dict)
    entry_node_ids: set[str] = field(default_factory=set)
    exit_node_ids: set[str] = field(default_factory=set)
    materialization_statuses: dict[str, str] = field(default_factory=dict)
    topological_order: list[str] = field(default_factory=list)
    max_fan_out: int = 0
    max_fan_in: int = 0

    @property
    def is_linear(self) -> bool:
        return self.max_fan_out <= 1 and self.max_fan_in <= 1

    @property
    def has_parallelism(self) -> bool:
        return self.max_fan_out > 1 or self.max_fan_in > 1

    def ordered_step_names(self) -> list[str]:
        return [self.step_name_by_node[nid] for nid in self.topological_order]

    def ready_set(self, completed: set[str], failed: set[str]) -> set[str]:
        """Compute the set of node_ids ready to execute."""
        ready: set[str] = set()
        blocked = self.blocked_set(failed)
        for node_id in self.node_ids:
            if node_id in completed or node_id in failed or node_id in blocked:
                continue
            preds = self.predecessors.get(node_id, set())
            if all(p in completed for p in preds):
                ready.add(node_id)
        return ready

    def blocked_set(self, failed: set[str]) -> set[str]:
        """Compute all nodes transitively blocked by failures."""
        blocked: set[str] = set()
        queue = list(failed)
        while queue:
            node_id = queue.pop()
            for succ in self.successors.get(node_id, set()):
                if succ not in blocked and succ not in failed:
                    blocked.add(succ)
                    queue.append(succ)
        return blocked

    def dependency_cone(self, node_id: str) -> set[str]:
        """Compute all nodes reachable downstream from node_id (exclusive)."""
        cone: set[str] = set()
        queue = list(self.successors.get(node_id, set()))
        while queue:
            nid = queue.pop()
            if nid not in cone:
                cone.add(nid)
                queue.extend(self.successors.get(nid, set()))
        return cone


class WorkflowGraphScheduler:
    scheduler_kind = "workflow_graph_scheduler_v1"

    def run(
        self,
        *,
        service: RunExecutionService,
        context: SchedulerExecutionContext,
        tracker: SchedulerCurrentStepTracker,
        logger,
    ) -> SchedulerRunResult:
        support = self.describe_support(resolved_plan=context.resolved_plan)
        dag_model: DAGReadModel | None = support.get("_dag_read_model")

        # Route to DAG parallel executor if graph has actual parallelism
        if dag_model is not None and dag_model.has_parallelism:
            steps_by_name = {step.step_name: step for step in context.steps}
            if len(steps_by_name) != len(context.steps):
                raise RuntimeError("workflow graph scheduler requires unique step names")
            # Verify all graph step names map to runtime steps
            for node_id in dag_model.node_ids:
                step_name = dag_model.step_name_by_node.get(node_id, "")
                if step_name not in steps_by_name:
                    raise RuntimeError(
                        f"workflow graph scheduler could not map graph node '{node_id}' "
                        f"(step '{step_name}') to a runtime step"
                    )
            return _run_steps_dag(
                service=service,
                context=context,
                tracker=tracker,
                logger=logger,
                dag_model=dag_model,
            )

        # Linear graph — use existing ordered execution
        read_model = support.get("read_model")
        if not isinstance(read_model, dict):
            raise RuntimeError("workflow graph scheduler selected without a valid graph read model")
        ordered_step_names = read_model.get("ordered_step_names", [])
        if not isinstance(ordered_step_names, list):
            raise RuntimeError("workflow graph scheduler missing ordered_step_names")
        steps_by_name = {step.step_name: step for step in context.steps}
        if len(steps_by_name) != len(context.steps):
            raise RuntimeError("workflow graph scheduler requires unique step names")
        ordered_steps: list[RunStep] = []
        for step_name in ordered_step_names:
            if not isinstance(step_name, str) or step_name not in steps_by_name:
                raise RuntimeError("workflow graph scheduler could not map graph step names to runtime steps")
            ordered_steps.append(steps_by_name[step_name])
        return _run_steps_in_order(
            service=service,
            context=context,
            tracker=tracker,
            logger=logger,
            ordered_steps=ordered_steps,
        )

    def describe_support(self, *, resolved_plan: ResolvedExecutionPlan | None) -> dict[str, Any]:
        if resolved_plan is None or not isinstance(resolved_plan.workflow_artifact, dict):
            return {
                "scheduler_kind": self.scheduler_kind,
                "supported": False,
                "reason": "workflow_artifact_missing",
            }
        workflow_graph = resolved_plan.workflow_artifact.get("workflow_graph", {})
        if not isinstance(workflow_graph, dict):
            return {
                "scheduler_kind": self.scheduler_kind,
                "supported": False,
                "reason": "workflow_graph_missing",
            }
        dag_model = self._build_dag_read_model(workflow_graph=workflow_graph)
        linear_read_model = self._build_linear_read_model(workflow_graph=workflow_graph)
        node_count = len(workflow_graph.get("node_instances", [])) if isinstance(workflow_graph.get("node_instances", []), list) else 0
        edge_count = len(workflow_graph.get("edge_set", [])) if isinstance(workflow_graph.get("edge_set", []), list) else 0

        if dag_model is not None:
            reason = "dag_supported" if dag_model.has_parallelism else "linear_subset_supported"
            # dag_read_model_summary is JSON-safe; full DAGReadModel stored as _dag_read_model (not serialized)
            dag_summary = {
                "read_model_kind": dag_model.read_model_kind,
                "node_count": len(dag_model.node_ids),
                "is_linear": dag_model.is_linear,
                "has_parallelism": dag_model.has_parallelism,
                "max_fan_out": dag_model.max_fan_out,
                "max_fan_in": dag_model.max_fan_in,
                "entry_node_ids": sorted(dag_model.entry_node_ids),
                "exit_node_ids": sorted(dag_model.exit_node_ids),
                "topological_order": dag_model.topological_order,
                "ordered_step_names": dag_model.ordered_step_names(),
            }
            return {
                "scheduler_kind": self.scheduler_kind,
                "supported": True,
                "reason": reason,
                "graph_kind": "dag" if dag_model.has_parallelism else "linear",
                "node_count": node_count,
                "edge_count": edge_count,
                "max_fan_out": dag_model.max_fan_out,
                "max_fan_in": dag_model.max_fan_in,
                "read_model": linear_read_model,
                "dag_read_model_summary": dag_summary,
                "_dag_read_model": dag_model,
            }
        if linear_read_model is not None:
            return {
                "scheduler_kind": self.scheduler_kind,
                "supported": True,
                "reason": "linear_subset_supported",
                "graph_kind": "linear",
                "node_count": node_count,
                "edge_count": edge_count,
                "read_model": linear_read_model,
                "dag_read_model_summary": None,
                "_dag_read_model": None,
            }
        return {
            "scheduler_kind": self.scheduler_kind,
            "supported": False,
            "reason": "non_linear_or_incomplete_workflow_graph",
            "node_count": node_count,
            "edge_count": edge_count,
            "read_model": None,
            "dag_read_model_summary": None,
            "_dag_read_model": None,
        }

    def _build_linear_read_model(self, *, workflow_graph: dict[str, Any]) -> dict[str, Any] | None:
        nodes = workflow_graph.get("node_instances", [])
        edges = workflow_graph.get("edge_set", [])
        entry_nodes = workflow_graph.get("entry_nodes", [])
        exit_nodes = workflow_graph.get("exit_nodes", [])
        if not (
            isinstance(nodes, list)
            and isinstance(edges, list)
            and isinstance(entry_nodes, list)
            and isinstance(exit_nodes, list)
        ):
            return None
        if not nodes:
            return {
                "read_model_kind": "linear_workflow_graph_v1",
                "entry_node_id": None,
                "exit_node_id": None,
                "next_ready_node_id": None,
                "next_ready_step_name": None,
                "ordered_node_ids": [],
                "ordered_step_names": [],
                "ordered_spec_refs": [],
                "materialization_statuses": {},
                "graph_checkpoint_metadata": {
                    "checkpoint_mode": "ordered_linear_nodes_v1",
                    "entry_node_count": 0,
                    "exit_node_count": 0,
                    "checkpoint_node_ids": [],
                    "ordered_checkpoint_candidates": [],
                },
            }
        if len(entry_nodes) != 1 or len(exit_nodes) != 1 or len(edges) != max(len(nodes) - 1, 0):
            return None

        nodes_by_id: dict[str, dict[str, Any]] = {}
        indegree: dict[str, int] = {}
        adjacency: dict[str, list[str]] = {}
        for node in nodes:
            if not isinstance(node, dict):
                return None
            node_id = str(node.get("node_id") or "")
            if not node_id:
                return None
            nodes_by_id[node_id] = node
            indegree[node_id] = 0
            adjacency[node_id] = []

        for edge in edges:
            if not isinstance(edge, dict):
                return None
            from_node_id = str(edge.get("from_node_id") or "")
            to_node_id = str(edge.get("to_node_id") or "")
            if from_node_id not in nodes_by_id or to_node_id not in nodes_by_id:
                return None
            adjacency[from_node_id].append(to_node_id)
            indegree[to_node_id] = indegree.get(to_node_id, 0) + 1

        if any(len(targets) > 1 for targets in adjacency.values()):
            return None
        if any(count > 1 for count in indegree.values()):
            return None

        entry_node_id = str(entry_nodes[0] or "")
        exit_node_id = str(exit_nodes[0] or "")
        if entry_node_id not in nodes_by_id or exit_node_id not in nodes_by_id:
            return None
        if indegree.get(entry_node_id, 0) != 0:
            return None

        ordered_node_ids: list[str] = []
        ordered_step_names: list[str] = []
        ordered_spec_refs: list[str] = []
        materialization_statuses: dict[str, str] = {}
        checkpoint_node_ids: list[str] = []
        ordered_checkpoint_candidates: list[dict[str, Any]] = []

        current_node_id = entry_node_id
        visited: set[str] = set()
        while current_node_id:
            if current_node_id in visited:
                return None
            visited.add(current_node_id)
            ordered_node_ids.append(current_node_id)
            node = nodes_by_id[current_node_id]
            execution_hints = node.get("instance_execution_hints", {})
            if not isinstance(execution_hints, dict):
                execution_hints = {}
            step_name = str(execution_hints.get("step_name") or node.get("spec_ref") or current_node_id)
            ordered_step_names.append(step_name)
            ordered_spec_refs.append(str(node.get("spec_ref") or ""))
            materialization_status = str(node.get("materialization_status") or "unknown")
            materialization_statuses[current_node_id] = materialization_status
            ordered_checkpoint_candidates.append(
                {
                    "node_id": current_node_id,
                    "step_name": step_name,
                    "materialization_status": materialization_status,
                }
            )
            if materialization_status != "resolved":
                checkpoint_node_ids.append(current_node_id)

            next_targets = adjacency.get(current_node_id, [])
            if not next_targets:
                break
            current_node_id = next_targets[0]

        if ordered_node_ids[-1] != exit_node_id:
            return None
        if len(visited) != len(nodes_by_id):
            return None

        next_ready_index = next(
            (index for index, node_id in enumerate(ordered_node_ids) if materialization_statuses.get(node_id) == "resolved"),
            None,
        )
        next_ready_node_id = ordered_node_ids[next_ready_index] if next_ready_index is not None else None
        next_ready_step_name = ordered_step_names[next_ready_index] if next_ready_index is not None else None

        return {
            "read_model_kind": "linear_workflow_graph_v1",
            "entry_node_id": entry_node_id,
            "exit_node_id": exit_node_id,
            "next_ready_node_id": next_ready_node_id,
            "next_ready_step_name": next_ready_step_name,
            "ordered_node_ids": ordered_node_ids,
            "ordered_step_names": ordered_step_names,
            "ordered_spec_refs": ordered_spec_refs,
            "materialization_statuses": materialization_statuses,
            "graph_checkpoint_metadata": {
                "checkpoint_mode": "ordered_linear_nodes_v1",
                "entry_node_count": len(entry_nodes),
                "exit_node_count": len(exit_nodes),
                "checkpoint_node_ids": checkpoint_node_ids,
                "ordered_checkpoint_candidates": ordered_checkpoint_candidates,
            },
        }

    def _build_dag_read_model(self, *, workflow_graph: dict[str, Any]) -> DAGReadModel | None:
        """Build a general DAG read model from a workflow graph.

        Returns None if the graph is not a valid DAG (contains cycles or is malformed).
        Linear graphs are a valid subset (max_fan_out=1, max_fan_in=1).
        """
        nodes = workflow_graph.get("node_instances", [])
        edges = workflow_graph.get("edge_set", [])
        entry_nodes = workflow_graph.get("entry_nodes", [])
        exit_nodes = workflow_graph.get("exit_nodes", [])
        if not (
            isinstance(nodes, list)
            and isinstance(edges, list)
            and isinstance(entry_nodes, list)
            and isinstance(exit_nodes, list)
        ):
            return None
        if not nodes:
            return DAGReadModel()

        # Parse nodes
        nodes_by_id: dict[str, dict[str, Any]] = {}
        for node in nodes:
            if not isinstance(node, dict):
                return None
            node_id = str(node.get("node_id") or "")
            if not node_id:
                return None
            nodes_by_id[node_id] = node

        # Parse edges, build adjacency
        predecessors: dict[str, set[str]] = {nid: set() for nid in nodes_by_id}
        successors: dict[str, set[str]] = {nid: set() for nid in nodes_by_id}
        for edge in edges:
            if not isinstance(edge, dict):
                return None
            from_id = str(edge.get("from_node_id") or "")
            to_id = str(edge.get("to_node_id") or "")
            if from_id not in nodes_by_id or to_id not in nodes_by_id:
                return None
            successors[from_id].add(to_id)
            predecessors[to_id].add(from_id)

        # Validate entry/exit nodes
        entry_set = set()
        for eid in entry_nodes:
            eid_str = str(eid or "")
            if eid_str not in nodes_by_id:
                return None
            if predecessors[eid_str]:
                return None  # entry node must have no predecessors
            entry_set.add(eid_str)
        exit_set = set()
        for xid in exit_nodes:
            xid_str = str(xid or "")
            if xid_str not in nodes_by_id:
                return None
            if successors[xid_str]:
                return None  # exit node must have no successors
            exit_set.add(xid_str)
        if not entry_set or not exit_set:
            return None

        # Topological sort (Kahn's algorithm) — validates DAG property
        in_degree = {nid: len(preds) for nid, preds in predecessors.items()}
        queue = [nid for nid in nodes_by_id if in_degree[nid] == 0]
        topo_order: list[str] = []
        while queue:
            queue.sort()  # deterministic ordering within same level
            nid = queue.pop(0)
            topo_order.append(nid)
            for succ in sorted(successors[nid]):
                in_degree[succ] -= 1
                if in_degree[succ] == 0:
                    queue.append(succ)
        if len(topo_order) != len(nodes_by_id):
            return None  # cycle detected

        # Extract step names and metadata
        step_name_by_node: dict[str, str] = {}
        spec_ref_by_node: dict[str, str] = {}
        materialization_statuses: dict[str, str] = {}
        for node_id, node in nodes_by_id.items():
            hints = node.get("instance_execution_hints", {})
            if not isinstance(hints, dict):
                hints = {}
            step_name_by_node[node_id] = str(hints.get("step_name") or node.get("spec_ref") or node_id)
            spec_ref_by_node[node_id] = str(node.get("spec_ref") or "")
            materialization_statuses[node_id] = str(node.get("materialization_status") or "unknown")

        # Check for duplicate step names
        step_names = list(step_name_by_node.values())
        if len(set(step_names)) != len(step_names):
            return None

        max_fan_out = max((len(succs) for succs in successors.values()), default=0)
        max_fan_in = max((len(preds) for preds in predecessors.values()), default=0)

        return DAGReadModel(
            read_model_kind="dag_workflow_graph_v1",
            node_ids=list(nodes_by_id.keys()),
            step_name_by_node=step_name_by_node,
            spec_ref_by_node=spec_ref_by_node,
            predecessors=predecessors,
            successors=successors,
            entry_node_ids=entry_set,
            exit_node_ids=exit_set,
            materialization_statuses=materialization_statuses,
            topological_order=topo_order,
            max_fan_out=max_fan_out,
            max_fan_in=max_fan_in,
        )
