from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import RunStatus, RunStepStatus
from app.core.exceptions import AppException
from app.db.models import Run, RunStep
from app.db.repositories.blueprint_repository import BlueprintRepository
from app.db.repositories.executable_repository import ExecutableRepository
from app.db.repositories.project_repository import ProjectRepository
from app.db.repositories.run_repository import RunRepository
from app.db.repositories.run_step_repository import RunStepRepository
from app.db.repositories.runtime_profile_repository import RuntimeProfileRepository
from app.repositories.artifact_repository import ArtifactRepository
from app.schemas.artifact import ArtifactResponse
from app.schemas.run import (
    RunConfirmationActionRequest,
    RunCreateRequest,
    RunRecoveryRequest,
    RunReplayRequest,
    RunResponse,
    RunResumeRequest,
    RunStepResponse,
    RuntimeOperationStateResponse,
    RuntimeRecoveryOptionResponse,
    RuntimeStepOperationStateResponse,
)
from app.services.job.job_dispatch_service import JobDispatchService
from app.services.job.job_service import JobService


def _iso(value) -> str:
    return value.isoformat() if value else ""


class RunService:
    INPUT_REUSE_SECRET_KEYWORDS = ("password", "secret", "token", "api_key")
    INPUT_REUSE_EPHEMERAL_KEYWORDS = ("session", "request_id", "timestamp", "nonce")

    def __init__(self, db: Session) -> None:
        self.db = db
        self.project_repository = ProjectRepository(db)
        self.blueprint_repository = BlueprintRepository(db)
        self.executable_repository = ExecutableRepository(db)
        self.runtime_profile_repository = RuntimeProfileRepository(db)
        self.run_repository = RunRepository(db)
        self.run_step_repository = RunStepRepository(db)
        self.artifact_repository = ArtifactRepository(db)
        self.job_service = JobService(db)
        self.dispatch_service = JobDispatchService()

    def create_run(self, payload: RunCreateRequest) -> RunResponse:
        if self.project_repository.get_by_id(payload.project_id) is None:
            raise AppException("Project not found", status_code=404)
        if payload.executable_id is None:
            raise AppException(
                "Executable is required before starting a run. Build first, then run from the executable.",
                status_code=400,
            )
        executable = self.executable_repository.get_by_id(payload.executable_id)
        if executable is None or executable.project_id != payload.project_id:
            raise AppException("Executable not found for this project", status_code=404)

        # Session-anchored executables have no blueprint_id; runs for
        # them also omit blueprint_id. For legacy executables, verify
        # the blueprint lineage as before.
        blueprint = None
        session_anchored = executable.blueprint_id is None
        if session_anchored:
            if payload.blueprint_id is not None:
                raise AppException(
                    "Session-anchored executables do not accept blueprint_id",
                    status_code=400,
                )
        else:
            if payload.blueprint_id is None:
                raise AppException(
                    "blueprint_id is required for blueprint-anchored executables",
                    status_code=400,
                )
            blueprint = self.blueprint_repository.get_by_id(payload.blueprint_id)
            if blueprint is None or blueprint.project_id != payload.project_id:
                raise AppException("Blueprint not found for this project", status_code=404)
            if executable.blueprint_id != payload.blueprint_id:
                raise AppException(
                    "Executable does not belong to the selected blueprint",
                    status_code=400,
                )
        executable_id = payload.executable_id
        runtime_profile_id = payload.runtime_profile_id
        if payload.runtime_profile_id is not None:
            runtime_profile = self.runtime_profile_repository.get_by_id(payload.runtime_profile_id)
            if runtime_profile is None or runtime_profile.project_id != payload.project_id:
                raise AppException("Runtime profile not found for this project", status_code=404)
            if (
                not session_anchored
                and runtime_profile.blueprint_id
                and runtime_profile.blueprint_id != payload.blueprint_id
            ):
                raise AppException("Runtime profile does not belong to the selected blueprint", status_code=400)

        run_payload = self._normalized_payload(payload.model_dump(mode="python", exclude_none=True))
        reuse_latest_successful_input = bool(run_payload.pop("reuse_latest_successful_input", False))
        input_reuse = self._build_input_reuse_metadata(
            enabled=reuse_latest_successful_input,
            project_id=payload.project_id,
            blueprint_id=payload.blueprint_id,
            runtime_profile_id=runtime_profile_id,
            executable_id=executable_id,
        )
        resolved_snapshot = dict(run_payload.get("resolved_config_snapshot_json") or {})
        resolved_snapshot["input_reuse"] = input_reuse
        resolved_snapshot["execution_mode"] = "executable-manifest"
        resolved_snapshot["runtime_operation"] = self._runtime_operation_snapshot(run_id="", operation_state="idle")
        run_payload["resolved_config_snapshot_json"] = resolved_snapshot
        run = Run(**run_payload)
        if run.status == RunStatus.RUNNING.value and run.started_at is None:
            run.started_at = datetime.now(timezone.utc)
        self.run_repository.create(run)
        if session_anchored:
            # Pull step list from the executable's manifest (the
            # session-anchored build wrote steps.json as part of the
            # generated file set).
            steps_json = self._load_session_executable_steps(executable)
            self.run_step_repository.create_many(self._build_run_steps(run.id, steps_json))
        else:
            self.run_step_repository.create_many(
                self._build_run_steps(run.id, blueprint.execution_steps_json)
            )
        self.db.commit()
        runtime_snapshot = self._runtime_operation_snapshot(run_id=run.id, operation_state="idle")
        resolved_snapshot = dict(run.resolved_config_snapshot_json or {})
        resolved_snapshot["runtime_operation"] = runtime_snapshot
        run.resolved_config_snapshot_json = resolved_snapshot
        self.db.commit()
        return self.get_run(run.id)

    def start_run(self, run_id: str) -> RunResponse:
        run = self.run_repository.get_by_id(run_id, with_relations=True)
        if run is None:
            raise AppException("Run not found", status_code=404)
        if run.status not in {RunStatus.PENDING.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}:
            raise AppException("Run cannot be started from the current state", status_code=400)

        if run.status in {RunStatus.FAILED.value, RunStatus.CANCELLED.value}:
            for step in run.run_steps:
                self._reset_step(step)

        run.started_at = None
        return self._queue_run_execution(run=run, requested_action="start", operator_notes=None, clear_terminal_state=True)

    def cancel_run(self, run_id: str) -> RunResponse:
        run = self.run_repository.get_by_id(run_id)
        if run is None:
            raise AppException("Run not found", status_code=404)
        if run.status in {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}:
            raise AppException("Run is already terminal", status_code=400)
        run.status = RunStatus.CANCELLED.value
        run.error_message = "cancelled by user"
        run.ended_at = datetime.now(timezone.utc)
        self._set_runtime_operation(
            run,
            {
                "operation_state": "cancelled",
                "blocked_reason": "cancelled_by_user",
                "confirmation_required": False,
                "intervention_required": False,
                "pending_step": None,
                "updated_at": _iso(datetime.now(timezone.utc)),
            },
        )
        self.db.commit()
        return self.get_run(run.id)

    def confirm_run(self, run_id: str, payload: RunConfirmationActionRequest) -> RunResponse:
        run = self.run_repository.get_by_id(run_id, with_relations=True)
        if run is None:
            raise AppException("Run not found", status_code=404)
        operation = self._get_runtime_operation(run)
        if operation.operation_state != "waiting_for_confirmation" or operation.confirmation_request is None:
            raise AppException("Run is not waiting for confirmation", status_code=400)

        step_name = payload.step_name or operation.confirmation_request.step_name
        if step_name is None:
            raise AppException("Confirmation step could not be determined", status_code=400)

        if payload.decision == "decline":
            run.status = RunStatus.CANCELLED.value
            run.ended_at = datetime.now(timezone.utc)
            run.error_message = f"confirmation declined for step '{step_name}'"
            run.summary = f"Run stopped because confirmation was declined for '{step_name}'."
            self._set_runtime_operation(
                run,
                {
                    "operation_state": "blocked",
                    "confirmation_required": False,
                    "confirmation_request": None,
                    "blocked_reason": "confirmation_declined",
                    "pending_step": step_name,
                    "operator_notes": payload.operator_notes,
                    "updated_at": _iso(datetime.now(timezone.utc)),
                    "recovery_summary": {
                        "run_id": run.id,
                        "operation_state": "blocked",
                        "last_successful_step": operation.last_successful_step,
                        "pending_step": step_name,
                        "blocked_reason": "confirmation_declined",
                        "recovery_options": [
                            RuntimeRecoveryOptionResponse(action="restart_run", label="Restart run").model_dump(mode="python"),
                            RuntimeRecoveryOptionResponse(action="abort_run", label="Abort run").model_dump(mode="python"),
                        ],
                        "updated_at": _iso(datetime.now(timezone.utc)),
                    },
                },
            )
            self.db.commit()
            return self.get_run(run.id)

        approved = list(operation.approved_step_names)
        if step_name not in approved:
            approved.append(step_name)
        self._set_runtime_operation(
            run,
            {
                "operation_state": "resumable" if not payload.auto_resume else "resumed",
                "confirmation_required": False,
                "confirmation_request": None,
                "approved_step_names": approved,
                "operator_notes": payload.operator_notes,
                "updated_at": _iso(datetime.now(timezone.utc)),
            },
        )
        self.db.commit()
        if not payload.auto_resume:
            return self.get_run(run.id)
        return self._queue_run_execution(
            run=run,
            requested_action="resume_run",
            operator_notes=payload.operator_notes,
            clear_terminal_state=False,
        )

    def resume_run(self, run_id: str, payload: RunResumeRequest) -> RunResponse:
        run = self.run_repository.get_by_id(run_id, with_relations=True)
        if run is None:
            raise AppException("Run not found", status_code=404)
        operation = self._get_runtime_operation(run)
        if operation.operation_state not in {"resumable", "waiting_for_intervention", "resumed"}:
            raise AppException("Run is not resumable", status_code=400)
        intervention_policy, _ = self._resolve_intervention_legality(run)
        if not self._intervention_resume_supported(intervention_policy):
            raise AppException("Run does not permit resume_run under the current intervention policy", status_code=400)
        return self._queue_run_execution(
            run=run,
            requested_action="resume_run",
            operator_notes=payload.operator_notes,
            clear_terminal_state=False,
        )

    def recover_run(self, run_id: str, payload: RunRecoveryRequest) -> RunResponse:
        run = self.run_repository.get_by_id(run_id, with_relations=True)
        if run is None:
            raise AppException("Run not found", status_code=404)
        if payload.override_input_json:
            current_input = dict(run.input_snapshot_json or {})
            current_input.update(payload.override_input_json)
            run.input_snapshot_json = current_input

        if payload.action == "abort_run":
            run.status = RunStatus.CANCELLED.value
            run.error_message = "recovery aborted by operator"
            run.ended_at = datetime.now(timezone.utc)
            self._set_runtime_operation(
                run,
                {
                    "operation_state": "cancelled",
                    "blocked_reason": "aborted_by_operator",
                    "operator_notes": payload.operator_notes,
                    "updated_at": _iso(datetime.now(timezone.utc)),
                },
            )
            self.db.commit()
            return self.get_run(run.id)

        if payload.action == "restart_run":
            for step in run.run_steps:
                self._reset_step(step)
            run.started_at = None
            return self._queue_run_execution(
                run=run,
                requested_action="restart_run",
                operator_notes=payload.operator_notes,
                clear_terminal_state=True,
            )

        requested_step_name = payload.step_name
        operation = self._get_runtime_operation(run)
        intervention_policy, _ = self._resolve_intervention_legality(run)
        if payload.action == "resume_run" and not self._intervention_resume_supported(intervention_policy):
            raise AppException("Run does not permit resume_run under the current intervention policy", status_code=400)
        if payload.action == "retry_step" and not self._intervention_mode_allowed(intervention_policy, "rerun"):
            raise AppException("Run does not permit retry_step under the current intervention policy", status_code=400)
        if payload.action == "retry_step" and not operation.rerun_supported:
            raise AppException("Run does not support retry_step", status_code=400)
        return self._queue_run_execution(
            run=run,
            requested_action=payload.action,
            requested_step_name=requested_step_name,
            operator_notes=payload.operator_notes,
            clear_terminal_state=False,
        )

    def get_run(self, run_id: str) -> RunResponse:
        run = self.run_repository.get_by_id(run_id, with_relations=True)
        if run is None:
            raise AppException("Run not found", status_code=404)
        return self._serialize(run)

    def list_project_runs(self, project_id: str) -> list[RunResponse]:
        if self.project_repository.get_by_id(project_id) is None:
            raise AppException("Project not found", status_code=404)
        return [self.get_run(item.id) for item in self.run_repository.list_by_project(project_id)]

    def get_run_steps(self, run_id: str) -> list[RunStepResponse]:
        run = self.run_repository.get_by_id(run_id)
        if run is None:
            raise AppException("Run not found", status_code=404)
        return [self._serialize_step(step) for step in self.run_step_repository.list_by_run(run_id)]

    def get_run_artifacts(self, run_id: str) -> list[ArtifactResponse]:
        run = self.run_repository.get_by_id(run_id)
        if run is None:
            raise AppException("Run not found", status_code=404)
        artifacts = self.artifact_repository.list_by_run(run_id)
        return [
            ArtifactResponse(
                id=item.id,
                run_id=item.run_id,
                run_step_id=item.run_step_id,
                artifact_type=item.artifact_type,
                storage_kind=item.storage_kind,
                path_or_content=item.path_or_content,
                metadata_json=item.metadata_json or {},
                created_at=_iso(item.created_at),
            )
            for item in artifacts
        ]

    def replay_run(self, run_id: str, payload: RunReplayRequest) -> RunResponse:
        original_run = self.run_repository.get_by_id(run_id)
        if original_run is None:
            raise AppException("Run not found", status_code=404)
        if original_run.executable_id is None:
            raise AppException(
                "This run was created before executable-only runtime enforcement. Rebuild the blueprint and start a new run from an executable.",
                status_code=400,
            )

        input_snapshot = dict(original_run.input_snapshot_json or {})
        input_snapshot.update(payload.input_snapshot_json or {})
        replay = RunCreateRequest(
            project_id=original_run.project_id,
            blueprint_id=original_run.blueprint_id,
            executable_id=original_run.executable_id,
            runtime_profile_id=original_run.runtime_profile_id,
            triggered_by=payload.triggered_by or f"replay:{original_run.id}",
            status=RunStatus.PENDING.value,
            input_snapshot_json=input_snapshot,
            resolved_config_snapshot_json={},
            summary=None,
            error_message=None,
        )
        new_run = self.create_run(replay)
        return self.start_run(new_run.id)

    def _serialize(self, run: Run) -> RunResponse:
        runtime_operation = self._get_runtime_operation(run)
        runtime_scheduler = dict((run.resolved_config_snapshot_json or {}).get("runtime_scheduler") or {})
        return RunResponse(
            id=run.id,
            project_id=run.project_id,
            blueprint_id=run.blueprint_id,
            executable_id=run.executable_id,
            runtime_profile_id=run.runtime_profile_id,
            active_job_id=run.active_job_id,
            active_job_status=run.active_job.status if run.active_job is not None else None,
            triggered_by=run.triggered_by,
            status=run.status,
            started_at=_iso(run.started_at),
            ended_at=_iso(run.ended_at),
            input_snapshot_json=run.input_snapshot_json or {},
            resolved_config_snapshot_json=run.resolved_config_snapshot_json or {},
            runtime_operation=runtime_operation,
            summary=run.summary,
            error_message=run.error_message,
            created_at=_iso(run.created_at),
            updated_at=_iso(run.updated_at),
            run_steps=[self._serialize_step(step, runtime_scheduler=runtime_scheduler) for step in sorted(run.run_steps, key=lambda value: value.step_order)],
        )

    def _serialize_step(self, step: RunStep, *, runtime_scheduler: dict[str, Any] | None = None) -> RunStepResponse:
        operation_state = self._step_operation_state(step, runtime_scheduler=runtime_scheduler)
        return RunStepResponse(
            id=step.id,
            run_id=step.run_id,
            step_name=step.step_name,
            step_order=step.step_order,
            node_name=step.node_name,
            status=step.status,
            input_payload_json=step.input_payload_json or {},
            output_payload_json=step.output_payload_json or {},
            log_excerpt=step.log_excerpt,
            token_usage_json=step.token_usage_json or {},
            started_at=_iso(step.started_at),
            finished_at=_iso(step.finished_at),
            operation_state=operation_state,
            created_at=_iso(step.created_at),
            updated_at=_iso(step.updated_at),
        )

    def _load_session_executable_steps(self, executable) -> dict[str, Any]:
        """Load ``steps.json`` from a session-anchored executable bundle."""
        import json
        from pathlib import Path

        if executable.source_path:
            steps_path = Path(executable.source_path) / "steps.json"
            if steps_path.exists():
                try:
                    return json.loads(steps_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
        # Fallback: pull from the executable's inline manifest_json if
        # the file isn't on disk (e.g. the manifest_snapshot path).
        manifest = executable.manifest_json or {}
        if isinstance(manifest, dict):
            meta = manifest.get("metadata", {}) or {}
            summary = meta.get("session_anchored_binding_summary") or {}
            # If we only have names, fabricate minimal step dicts so
            # RunStep rows get created with the right step_name.
            runnable = list(summary.get("runnable_steps") or [])
            blocked = list(summary.get("blocked_steps") or [])
            if runnable or blocked:
                return {
                    "steps": [
                        {"step_name": name, "node_name": name}
                        for name in runnable + blocked
                    ]
                }
        return {}

    def _build_run_steps(self, run_id: str, execution_steps_json: dict[str, Any] | None) -> list[RunStep]:
        raw_steps = []
        if isinstance(execution_steps_json, dict):
            raw_steps = execution_steps_json.get("steps", [])
        elif isinstance(execution_steps_json, list):
            raw_steps = execution_steps_json

        if not raw_steps:
            raw_steps = [
                {"step_name": "prepare_runtime", "node_name": "runtime", "status": RunStepStatus.PENDING.value},
                {"step_name": "execute_blueprint", "node_name": "executor", "status": RunStepStatus.PENDING.value},
            ]

        steps = []
        for index, item in enumerate(raw_steps):
            if isinstance(item, str):
                step_name = item
                node_name = None
                status = RunStepStatus.PENDING.value
            else:
                step_name = item.get("step_name") or item.get("name") or f"step_{index + 1}"
                node_name = item.get("node_name")
                status = item.get("status") or RunStepStatus.PENDING.value
            steps.append(
                RunStep(
                    run_id=run_id,
                    step_name=step_name,
                    step_order=index + 1,
                    node_name=node_name,
                    status=status,
                )
            )
        return steps

    def _normalized_payload(self, payload: dict) -> dict:
        for field_name, value in list(payload.items()):
            if hasattr(value, "value"):
                payload[field_name] = value.value
        return payload

    def _queue_run_execution(
        self,
        *,
        run: Run,
        requested_action: str,
        requested_step_name: str | None = None,
        operator_notes: str | None,
        clear_terminal_state: bool,
    ) -> RunResponse:
        now = datetime.now(timezone.utc)
        run.status = RunStatus.PENDING.value
        run.active_job_id = None
        run.error_message = None
        run.summary = None if clear_terminal_state else run.summary
        run.ended_at = None
        self._set_runtime_operation(
            run,
            {
                "operation_state": "running" if requested_action in {"start", "restart_run"} else "resumed",
                "requested_action": requested_action,
                "requested_step_name": requested_step_name,
                "operator_notes": operator_notes,
                "confirmation_required": False,
                "confirmation_request": None,
                "intervention_required": False,
                "intervention_state": None,
                "approved_step_names": [] if clear_terminal_state else self._get_runtime_operation(run).approved_step_names,
                "last_successful_step": None if clear_terminal_state else self._get_runtime_operation(run).last_successful_step,
                "pending_step": None if clear_terminal_state else self._get_runtime_operation(run).pending_step,
                "latest_checkpoint": None if clear_terminal_state else self._get_runtime_operation(run).latest_checkpoint.model_dump(mode="python") if self._get_runtime_operation(run).latest_checkpoint is not None else None,
                "recovery_summary": None if clear_terminal_state else self._get_runtime_operation(run).recovery_summary.model_dump(mode="python") if self._get_runtime_operation(run).recovery_summary is not None else None,
                "updated_at": _iso(now),
            },
        )
        job = self.job_service.create_job(
            project_id=run.project_id,
            job_type="run",
            owner_id=run.id,
            payload_json={"run_id": run.id, "requested_action": requested_action, "requested_step_name": requested_step_name},
        )
        run.active_job_id = job.id
        self.db.commit()
        dispatch_mode, celery_task_id = self.dispatch_service.dispatch(job.id)
        job.celery_task_id = celery_task_id
        job.result_json = {"dispatch_mode": dispatch_mode, "requested_action": requested_action}
        self.db.commit()
        return self.get_run(run.id)

    def _runtime_operation_snapshot(self, *, run_id: str, operation_state: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "operation_state": operation_state,
            "scheduler_kind": None,
            "scheduler_authority_source": None,
            "confirmation_required": False,
            "intervention_required": False,
            "intervention_legality_authority_source": None,
            "resume_supported": False,
            "rerun_supported": False,
            "step_states": [],
            "approved_step_names": [],
            "pending_step_authority_source": None,
        }

    def _get_runtime_operation(self, run: Run) -> RuntimeOperationStateResponse:
        raw_snapshot = dict(run.resolved_config_snapshot_json or {})
        payload = dict(raw_snapshot.get("runtime_operation") or {})
        runtime_scheduler = dict(raw_snapshot.get("runtime_scheduler") or {})
        payload.setdefault("run_id", run.id)
        payload.setdefault("scheduler_kind", runtime_scheduler.get("active_scheduler"))
        payload.setdefault("scheduler_authority_source", runtime_scheduler.get("active_scheduler_authority_source"))
        payload.setdefault("pending_step_authority_source", runtime_scheduler.get("pending_step_authority_source"))
        if not payload.get("updated_at"):
            payload["updated_at"] = _iso(run.updated_at)
        return RuntimeOperationStateResponse.model_validate(payload)

    def _set_runtime_operation(self, run: Run, patch: dict[str, Any]) -> None:
        snapshot = dict(run.resolved_config_snapshot_json or {})
        current = dict(snapshot.get("runtime_operation") or self._runtime_operation_snapshot(run_id=run.id, operation_state="idle"))
        current.update({key: value for key, value in patch.items() if value is not None or key in current})
        current["run_id"] = run.id
        snapshot["runtime_operation"] = current
        run.resolved_config_snapshot_json = snapshot

    def _step_operation_state(self, step: RunStep, *, runtime_scheduler: dict[str, Any] | None = None) -> RuntimeStepOperationStateResponse | None:
        payload = step.output_payload_json or {}
        operation_payload = payload.get("operation_state")
        if isinstance(operation_payload, dict):
            return RuntimeStepOperationStateResponse.model_validate(operation_payload)
        status = str(payload.get("status") or "").strip().lower()
        if status in {"waiting_for_confirmation", "waiting_for_intervention", "failed", "completed", "cancelled"}:
            scheduler = runtime_scheduler or {}
            return RuntimeStepOperationStateResponse(
                step_name=step.step_name,
                operation_state="failed_terminal" if status == "failed" else status,
                scheduler_kind=scheduler.get("active_scheduler"),
                scheduler_authority_source=scheduler.get("active_scheduler_authority_source"),
                blocked_reason=payload.get("failure_detail") if status == "failed" else payload.get("blocked_reason"),
                confirmation_required=bool(payload.get("confirmation_required")),
                confirmation_reason=payload.get("confirmation_reason"),
                authority_source=payload.get("authority_source"),
                intervention_required=bool(payload.get("intervention_required")),
                intervention_reason=payload.get("intervention_reason"),
                intervention_legality_authority_source=payload.get("intervention_legality_authority_source"),
                resume_supported=bool(payload.get("resume_supported")),
                rerun_supported=bool(payload.get("rerun_supported")),
                rerun_authority_source=payload.get("rerun_authority_source"),
                rerun_requires_human_approval=bool(payload.get("rerun_requires_human_approval")),
                pending_step_authority_source=scheduler.get("pending_step_authority_source"),
                recovery_options=[
                    RuntimeRecoveryOptionResponse.model_validate(item)
                    for item in payload.get("recovery_options", [])
                    if isinstance(item, dict)
                ],
                checkpoint_id=payload.get("checkpoint_id"),
            )
        step_status = step.status
        if step_status in {RunStepStatus.PENDING.value, RunStepStatus.RUNNING.value}:
            scheduler = runtime_scheduler or {}
            mapped_state = "running" if step_status == RunStepStatus.RUNNING.value else "idle"
            return RuntimeStepOperationStateResponse(
                step_name=step.step_name,
                operation_state=mapped_state,
                scheduler_kind=scheduler.get("active_scheduler"),
                scheduler_authority_source=scheduler.get("active_scheduler_authority_source"),
                pending_step_authority_source=scheduler.get("pending_step_authority_source"),
            )
        return None

    def _reset_step(self, step: RunStep) -> None:
        step.status = RunStepStatus.PENDING.value
        step.input_payload_json = None
        step.output_payload_json = None
        step.log_excerpt = None
        step.token_usage_json = None
        step.started_at = None
        step.finished_at = None

    def _resolve_intervention_legality(self, run: Run) -> tuple[dict[str, Any], str]:
        snapshot = dict(run.resolved_config_snapshot_json or {})
        workflow_artifact = snapshot.get("workflow_artifact")
        if isinstance(workflow_artifact, dict):
            execution_contract = workflow_artifact.get("execution_contract", {})
            if isinstance(execution_contract, dict):
                policy = execution_contract.get("intervention_legality", {})
                if isinstance(policy, dict) and isinstance(policy.get("allowed_modes"), list):
                    return policy, "workflow_artifact.execution_contract.intervention_legality"

        explainability = snapshot.get("workflow_native_explainability", {})
        if isinstance(explainability, dict):
            shadow = explainability.get("workflow_native_shadow_authority", {})
            if isinstance(shadow, dict):
                intervention = shadow.get("intervention_legality", {})
                if isinstance(intervention, dict):
                    compatibility = intervention.get("compatibility_projection", {})
                    if isinstance(compatibility, dict) and isinstance(compatibility.get("allowed_modes"), list):
                        return compatibility, "resolved_steps_and_bindings.compatibility_projection"

        return {
            "allowed_modes": ["inspect", "approve", "reject", "rerun"],
            "required_before_execute": False,
            "required_before_commit": False,
            "editable_fields": [],
            "skip_policy": "forbidden",
            "resume_policy": "resume_from_current",
        }, "runtime.compatibility_default_intervention"

    def _intervention_mode_allowed(self, policy: dict[str, Any], mode: str) -> bool:
        allowed_modes = policy.get("allowed_modes")
        if not isinstance(allowed_modes, list):
            return False
        return mode in {str(item) for item in allowed_modes}

    def _intervention_resume_supported(self, policy: dict[str, Any]) -> bool:
        return str(policy.get("resume_policy") or "resume_from_current").strip() != "forbidden"

    def _build_input_reuse_metadata(
        self,
        *,
        enabled: bool,
        project_id: str,
        blueprint_id: str,
        runtime_profile_id: str | None,
        executable_id: str | None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "enabled": enabled,
            "used": False,
            "source_kind": "latest_successful_run_input" if enabled else None,
            "source_run_id": None,
            "reused_key_count": 0,
            "dropped_keys": [],
            "reused_input": {},
        }
        if not enabled:
            return metadata

        candidate = self.run_repository.get_latest_successful_reusable_candidate(
            project_id=project_id,
            blueprint_id=blueprint_id,
            runtime_profile_id=runtime_profile_id,
            executable_id=executable_id,
        )
        if candidate is None:
            return metadata

        reusable_input, dropped_keys = self._filter_reusable_input(candidate.input_snapshot_json or {})
        metadata["source_run_id"] = candidate.id
        metadata["dropped_keys"] = dropped_keys
        metadata["reused_input"] = reusable_input
        metadata["reused_key_count"] = len(reusable_input)
        metadata["used"] = len(reusable_input) > 0
        return metadata

    def _filter_reusable_input(self, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        reusable: dict[str, Any] = {}
        dropped_keys: list[str] = []
        for key, value in payload.items():
            lowered = str(key).strip().lower()
            if any(keyword in lowered for keyword in self.INPUT_REUSE_SECRET_KEYWORDS + self.INPUT_REUSE_EPHEMERAL_KEYWORDS):
                dropped_keys.append(str(key))
                continue
            reusable[str(key)] = value
        return reusable, sorted(dropped_keys)
