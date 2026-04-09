from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.core.enums import BuildStepStatus
from app.core.exceptions import AppException
from app.db.models.entities import BuildStep
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.build_step_repository import BuildStepRepository
from app.repositories.build_task_repository import BuildTaskRepository
from app.schemas.build_task import (
    ArtifactResponse,
    BuildStepResponse,
    BuildTaskEventPayload,
    BuildTaskResponse,
    TokenUsageLogResponse,
)


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


class BuildTaskService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.build_task_repository = BuildTaskRepository(db)
        self.build_step_repository = BuildStepRepository(db)
        self.artifact_repository = ArtifactRepository(db)

    def get_build_task(self, build_task_id: str) -> BuildTaskResponse:
        build_task = self.build_task_repository.get_by_id(build_task_id, with_relations=True)
        if build_task is None:
            raise AppException("Build task not found", status_code=404)
        return self._serialize_build_task(build_task)

    def get_build_steps(self, build_task_id: str) -> list[BuildStepResponse]:
        build_task = self.build_task_repository.get_by_id(build_task_id)
        if build_task is None:
            raise AppException("Build task not found", status_code=404)
        steps = self.build_step_repository.list_by_build_task(build_task_id)
        return [self._serialize_step(step) for step in steps]

    def get_artifacts(self, build_task_id: str) -> list[ArtifactResponse]:
        build_task = self.build_task_repository.get_by_id(build_task_id)
        if build_task is None:
            raise AppException("Build task not found", status_code=404)
        artifacts = self.artifact_repository.list_by_build_task(build_task_id)
        return [self._serialize_artifact(item) for item in artifacts]

    def build_event_payload(self, build_task_id: str) -> BuildTaskEventPayload:
        build_task = self.build_task_repository.get_by_id(build_task_id, with_relations=True)
        if build_task is None:
            raise AppException("Build task not found", status_code=404)
        steps = [self._serialize_step(step) for step in sorted(build_task.build_steps, key=lambda item: item.step_order)]
        current_step = next((step.step_name for step in build_task.build_steps if step.status == BuildStepStatus.RUNNING.value), None)
        if current_step is None:
            current_step = next((step.step_name for step in build_task.build_steps if step.status == BuildStepStatus.PENDING.value), None)
        return BuildTaskEventPayload(
            task_id=build_task.id,
            status=build_task.status,
            current_step=current_step,
            total_tokens=build_task.total_tokens,
            error_message=build_task.error_message,
            steps=steps,
        )

    def _serialize_build_task(self, build_task: BuildTask) -> BuildTaskResponse:
        return BuildTaskResponse(
            id=build_task.id,
            project_id=build_task.project_id,
            requirement_session_id=build_task.requirement_document_id,
            status=build_task.status,
            started_at=_iso(build_task.started_at),
            finished_at=_iso(build_task.finished_at),
            total_tokens=build_task.total_tokens,
            error_message=build_task.error_message,
            created_at=_iso(build_task.created_at) or "",
            updated_at=_iso(build_task.updated_at) or "",
            steps=[self._serialize_step(step) for step in sorted(build_task.build_steps, key=lambda item: item.step_order)],
            artifacts=[self._serialize_artifact(item) for item in build_task.artifacts],
            token_usage_logs=[
                TokenUsageLogResponse(
                    id=item.id,
                    build_task_id=item.build_task_id,
                    build_step_id=item.build_step_id,
                    provider=item.provider,
                    model_name=item.model_name,
                    prompt_tokens=item.prompt_tokens,
                    completion_tokens=item.completion_tokens,
                    total_tokens=item.total_tokens,
                    cost_estimate=float(item.cost_estimate) if item.cost_estimate is not None else None,
                    created_at=_iso(item.created_at) or "",
                )
                for item in build_task.token_usage_logs
            ],
        )

    def _serialize_step(self, step: BuildStep) -> BuildStepResponse:
        return BuildStepResponse(
            id=step.id,
            step_name=step.step_name,
            step_order=step.step_order,
            status=step.status,
            input_payload=step.input_payload,
            output_payload=step.output_payload,
            prompt_text=step.prompt_text,
            log_text=step.log_text,
            token_usage=step.token_usage,
            started_at=_iso(step.started_at),
            finished_at=_iso(step.finished_at),
            created_at=_iso(step.created_at) or "",
            updated_at=_iso(step.updated_at) or "",
        )

    def _serialize_artifact(self, artifact) -> ArtifactResponse:
        return ArtifactResponse(
            id=artifact.id,
            build_task_id=artifact.build_task_id,
            build_step_id=artifact.build_step_id,
            artifact_type=artifact.artifact_type,
            path_or_content=artifact.path_or_content,
            metadata_json=artifact.metadata_json,
            created_at=_iso(artifact.created_at) or "",
        )
