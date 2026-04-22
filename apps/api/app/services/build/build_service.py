from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.enums import BuildStatus, BuildStepStatus
from app.core.exceptions import AppException
from app.db.models import Build, BuildStepV2, FrontChainSession
from app.db.repositories.blueprint_repository import BlueprintRepository
from app.db.repositories.build_repository import BuildRepository
from app.db.repositories.build_step_repository import BuildStepRepository
from app.db.repositories.project_repository import ProjectRepository
from app.repositories.artifact_repository import ArtifactRepository
from app.schemas.artifact import ArtifactResponse
from app.schemas.build import BuildCreateRequest, BuildResponse, BuildStepResponse
from app.services.job.job_dispatch_service import JobDispatchService
from app.services.job.job_service import JobService


def _iso(value) -> str:
    return value.isoformat() if value else ""


class BuildService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.project_repository = ProjectRepository(db)
        self.blueprint_repository = BlueprintRepository(db)
        self.build_repository = BuildRepository(db)
        self.build_step_repository = BuildStepRepository(db)
        self.artifact_repository = ArtifactRepository(db)
        self.job_service = JobService(db)
        self.dispatch_service = JobDispatchService()

    def create_build(self, payload: BuildCreateRequest) -> BuildResponse:
        if self.project_repository.get_by_id(payload.project_id) is None:
            raise AppException("Project not found", status_code=404)

        # Parallel entry: exactly one of (blueprint_id, session_id) must
        # be provided. The two anchors lead to different execute_build
        # branches — we don't try to shoehorn session into blueprint or
        # vice versa.
        has_blueprint = bool(payload.blueprint_id)
        has_session = bool(payload.session_id)
        if has_blueprint == has_session:
            raise AppException(
                "Build requires exactly one of blueprint_id or session_id",
                status_code=400,
            )

        session_id: str | None = None
        if has_blueprint:
            blueprint = self.blueprint_repository.get_by_id(payload.blueprint_id)
            if blueprint is None or blueprint.project_id != payload.project_id:
                raise AppException("Blueprint not found for this project", status_code=404)
            build_version = self._next_build_version(payload.blueprint_id)
        else:
            session = (
                self.db.query(FrontChainSession)
                .filter(FrontChainSession.id == payload.session_id)
                .one_or_none()
            )
            if session is None or session.project_id != payload.project_id:
                raise AppException("Front-chain session not found for this project", status_code=404)
            session_id = session.id
            build_version = self._next_build_version_for_session(session_id)

        build = Build(
            project_id=payload.project_id,
            blueprint_id=payload.blueprint_id if has_blueprint else None,
            session_id=session_id,
            build_version=build_version,
            status=BuildStatus.PENDING.value,
            build_options_json=payload.build_options_json or {},
        )
        self.build_repository.create(build)
        self.build_step_repository.create_many(
            [
                BuildStepV2(build_id=build.id, step_name="analyze_source", step_order=1, status=BuildStepStatus.PENDING.value),
                BuildStepV2(build_id=build.id, step_name="generate_manifest", step_order=2, status=BuildStepStatus.PENDING.value),
                BuildStepV2(build_id=build.id, step_name="materialize_executable", step_order=3, status=BuildStepStatus.PENDING.value),
                BuildStepV2(build_id=build.id, step_name="finalize_executable", step_order=4, status=BuildStepStatus.PENDING.value),
            ]
        )
        self.db.commit()
        return self.get_build(build.id)

    def start_build(self, build_id: str) -> BuildResponse:
        build = self.build_repository.get_by_id(build_id, with_relations=True)
        if build is None:
            raise AppException("Build not found", status_code=404)
        if build.status not in {BuildStatus.PENDING.value, BuildStatus.FAILED.value}:
            raise AppException("Build cannot be started from the current state", status_code=400)
        if build.status == BuildStatus.FAILED.value:
            for step in build.build_steps:
                step.status = BuildStepStatus.PENDING.value
                step.input_payload_json = None
                step.output_payload_json = None
                step.log_excerpt = None
                step.token_usage_json = None
                step.started_at = None
                step.finished_at = None
        build.status = BuildStatus.PENDING.value
        build.active_job_id = None
        build.error_message = None
        build.summary = None
        build.started_at = None
        build.ended_at = None
        job = self.job_service.create_job(
            project_id=build.project_id,
            job_type="build",
            owner_id=build.id,
            payload_json={"build_id": build.id},
        )
        build.active_job_id = job.id
        self.db.commit()
        dispatch_mode, celery_task_id = self.dispatch_service.dispatch(job.id)
        job.celery_task_id = celery_task_id
        job.result_json = {"dispatch_mode": dispatch_mode}
        self.db.commit()
        return self.get_build(build.id)

    def get_build(self, build_id: str) -> BuildResponse:
        build = self.build_repository.get_by_id(build_id, with_relations=True)
        if build is None:
            raise AppException("Build not found", status_code=404)
        return self._serialize(build)

    def list_project_builds(self, project_id: str) -> list[BuildResponse]:
        if self.project_repository.get_by_id(project_id) is None:
            raise AppException("Project not found", status_code=404)
        return [self.get_build(item.id) for item in self.build_repository.list_by_project(project_id)]

    def get_build_steps(self, build_id: str) -> list[BuildStepResponse]:
        build = self.build_repository.get_by_id(build_id)
        if build is None:
            raise AppException("Build not found", status_code=404)
        return [self._serialize_step(item) for item in self.build_step_repository.list_by_build(build_id)]

    def get_build_artifacts(self, build_id: str) -> list[ArtifactResponse]:
        build = self.build_repository.get_by_id(build_id)
        if build is None:
            raise AppException("Build not found", status_code=404)
        return [
            ArtifactResponse(
                id=item.id,
                build_id=item.build_id,
                build_step_id=item.build_step_v2_id,
                run_id=item.run_id,
                run_step_id=item.run_step_id,
                executable_id=item.executable_id,
                artifact_type=item.artifact_type,
                storage_kind=item.storage_kind,
                path_or_content=item.path_or_content,
                metadata_json=item.metadata_json or {},
                created_at=_iso(item.created_at),
            )
            for item in self.artifact_repository.list_by_build(build_id)
        ]

    def _serialize(self, build: Build) -> BuildResponse:
        return BuildResponse(
            id=build.id,
            project_id=build.project_id,
            blueprint_id=build.blueprint_id,
            session_id=build.session_id,
            executable_id=build.executable_id,
            active_job_id=build.active_job_id,
            active_job_status=build.active_job.status if build.active_job is not None else None,
            build_version=build.build_version,
            status=build.status,
            build_options_json=build.build_options_json or {},
            manifest_snapshot_json=build.manifest_snapshot_json or {},
            summary=build.summary,
            error_message=build.error_message,
            started_at=_iso(build.started_at),
            ended_at=_iso(build.ended_at),
            created_at=_iso(build.created_at),
            updated_at=_iso(build.updated_at),
            build_steps=[self._serialize_step(step) for step in sorted(build.build_steps, key=lambda item: item.step_order)],
        )

    def _serialize_step(self, step: BuildStepV2) -> BuildStepResponse:
        return BuildStepResponse(
            id=step.id,
            build_id=step.build_id,
            step_name=step.step_name,
            step_order=step.step_order,
            status=step.status,
            input_payload_json=step.input_payload_json or {},
            output_payload_json=step.output_payload_json or {},
            log_excerpt=step.log_excerpt,
            token_usage_json=step.token_usage_json or {},
            started_at=_iso(step.started_at),
            finished_at=_iso(step.finished_at),
            created_at=_iso(step.created_at),
            updated_at=_iso(step.updated_at),
        )

    def _next_build_version(self, blueprint_id: str) -> str:
        count = self.build_repository.count_by_blueprint(blueprint_id) + 1
        return f"build-{count:04d}"

    def _next_build_version_for_session(self, session_id: str) -> str:
        count = (
            self.db.query(Build)
            .filter(Build.session_id == session_id)
            .count()
            + 1
        )
        return f"build-{count:04d}"
