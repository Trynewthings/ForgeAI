from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Thread
from typing import Any

from app.core.config import ROOT_DIR, settings
from app.core.database import SessionLocal
from app.core.enums import BuildStatus, BuildStepStatus, BlueprintType, ExecutableStatus, ExecutableType
from app.core.logging import log_event
from app.db.models import Build, BuildStepV2, Executable
from app.db.models.entities import Artifact, TokenUsageLog
from app.db.repositories.blueprint_repository import BlueprintRepository
from app.db.repositories.build_repository import BuildRepository
from app.db.repositories.build_step_repository import BuildStepRepository
from app.db.repositories.executable_repository import ExecutableRepository
from app.repositories.artifact_repository import ArtifactRepository
from app.repositories.token_usage_log_repository import TokenUsageLogRepository
from app.schemas.executable_manifest import (
    ExecutableBuildPlanSummary,
    ExecutionPackageAssetIndex,
    ExecutionPackageAssetReference,
    ExecutionPackageBinding,
    ExecutionPackageBindingsDocument,
    ExecutionPackageMetadata,
    ExecutionPackageRuntimeEntry,
)
from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.workflow_native import (
    AcceptanceRule,
    ApprovalRequirement,
    BuildMaterializationRecord,
    ExecutionAdmissibilityContract,
    FailureEscalationPolicy,
    ArtifactEmissionPolicy,
    EvidenceRetentionPolicy,
    FallbackPolicy,
    GraphInvariant,
    HumanGatePolicy,
    InterventionPolicy,
    NodeCapabilityBinding,
    NodeInstance,
    NodeVerificationTarget,
    NodePortSpec,
    NodeSpec,
    NodeBindingDecisionRecord,
    MaterializationCheckpointInsertion,
    ProjectionRepairRecord,
    RetryPolicy,
    RerunPolicy,
    TimeoutPolicy,
    WorkflowGraphDraft,
    WorkflowPlanningDraft,
    NodeDraft,
    VerificationMethod,
    VerificationPlan,
    WorkflowArtifact,
    WorkflowEdge,
    WorkflowExecutionContract,
    WorkflowGraph,
)
from app.services.capabilities import (
    CapabilityRegistryService,
    CapabilityLifecycleService,
    CapabilityMaterializationService,
    CapabilityRequestService,
    CapabilityResolutionService,
)
from app.services.build.build_planning_service import ExecutableBuildPlanningService

logger = logging.getLogger(__name__)


class BuildExecutionService:
    def __init__(self) -> None:
        self.delay_seconds = settings.build_step_delay_seconds
        self.db = None

    def dispatch(self, build_id: str) -> str:
        thread = Thread(target=self.execute_build, args=(build_id,), daemon=True)
        thread.start()
        log_event(logger, logging.INFO, "build.dispatch.sent_to_thread", build_id=build_id)
        return "thread"

    def execute_build(self, build_id: str) -> None:
        db = SessionLocal()
        self.db = db
        build_repository = BuildRepository(db)
        build_step_repository = BuildStepRepository(db)
        blueprint_repository = BlueprintRepository(db)
        executable_repository = ExecutableRepository(db)
        artifact_repository = ArtifactRepository(db)
        token_usage_log_repository = TokenUsageLogRepository(db)

        current_step: BuildStepV2 | None = None
        try:
            build = build_repository.get_by_id(build_id, with_relations=True)
            if build is None:
                raise RuntimeError("Build not found")

            # Session-anchored C-slice takes a completely separate path.
            # The legacy blueprint branch below is intentionally left
            # untouched — it is known-broken at the planner signature
            # point, tracked as tech debt. We do NOT try to unify.
            if build.session_id and not build.blueprint_id:
                self._execute_session_anchored_build(
                    db=db,
                    build=build,
                    build_repository=build_repository,
                    build_step_repository=build_step_repository,
                    executable_repository=executable_repository,
                    artifact_repository=artifact_repository,
                    token_usage_log_repository=token_usage_log_repository,
                )
                return

            blueprint = blueprint_repository.get_by_id(build.blueprint_id)
            if blueprint is None:
                raise RuntimeError("Blueprint not found")

            build.status = BuildStatus.RUNNING.value
            build.started_at = datetime.now(timezone.utc)
            build.error_message = None
            db.commit()
            log_event(logger, logging.INFO, "build.started", build_id=build.id, blueprint_id=blueprint.id)

            build_dir = ROOT_DIR / ".generated" / "executables" / build.project_id / build.build_version
            build_dir.mkdir(parents=True, exist_ok=True)

            executable_type = self._executable_type_for_blueprint(blueprint.blueprint_type)
            executable = Executable(
                project_id=build.project_id,
                blueprint_id=build.blueprint_id,
                executable_type=executable_type,
                source_path=str(build_dir),
                build_version=build.build_version,
                status=ExecutableStatus.DRAFT.value,
            )
            executable_repository.create(executable)
            build.executable_id = executable.id
            db.commit()

            build_plan_summary, build_plan_validation_records, build_plan_fallback_events, build_plan_stage_trace = (
                ExecutableBuildPlanningService(db, project_id=build.project_id).plan(
                    blueprint=blueprint,
                    build_options=build.build_options_json or {},
                )
            )
            capability_requests = CapabilityRequestService(db).ensure_requests_for_gaps(
                project_id=build.project_id,
                gaps=list(build_plan_summary.capability_gaps),
                source_blueprint_id=blueprint.id,
                source_build_id=build.id,
            )
            capability_resolution = CapabilityResolutionService(db).build_role_views(
                project_id=build.project_id,
                capability_gaps=list(build_plan_summary.capability_gaps),
                capability_requests=capability_requests,
            )
            build_plan_metadata = build_plan_summary.model_copy(
                update={
                    "capability_requests": capability_requests,
                    "capability_request_summary": CapabilityRequestService(db).summarize_requests(capability_requests),
                    "capability_resolution": capability_resolution,
                    "warnings": [
                        *build_plan_summary.warnings,
                        *[record.message for record in build_plan_validation_records if record.status != "accepted"],
                        *[event.detail for event in build_plan_fallback_events],
                    ],
                }
            )
            # --- Run WorkflowPlanner to produce draft graph ---
            from app.services.workflow_planner_service import WorkflowPlannerService
            planner_output = WorkflowPlannerService().plan(blueprint)

            # --- LLM Planner: shadow + partial authority migration ---
            from app.services.workflow_planner_llm_service import (
                WorkflowPlannerLLMService,
                WorkflowPlannerShadowComparator,
                resolve_planner_authority,
            )
            llm_shadow_result = WorkflowPlannerLLMService().plan(blueprint)
            llm_shadow_diff = WorkflowPlannerShadowComparator().compare(planner_output, llm_shadow_result)
            # Resolve authority: may promote LLM output for eligible domains
            planner_output, authority_metadata = resolve_planner_authority(
                deterministic_output=planner_output,
                llm_result=llm_shadow_result,
                shadow_diff=llm_shadow_diff,
                blueprint=blueprint,
            )
            log_event(
                logger, logging.INFO, "build.planner_authority_resolved",
                authority_source=authority_metadata.get("authority_source"),
                llm_chosen=authority_metadata.get("llm_chosen"),
                reason=authority_metadata.get("reason"),
            )

            generated_files = self._build_files_for_blueprint(
                build_id=build.id,
                project_id=build.project_id,
                blueprint_type=blueprint.blueprint_type,
                blueprint_name=blueprint.name,
                execution_steps_json=blueprint.execution_steps_json or {},
                input_schema_json=blueprint.input_schema_json or {},
                output_schema_json=blueprint.output_schema_json or {},
                execution_env_json=blueprint.execution_env_json or {},
                configurable_vars_json=blueprint.configurable_vars_json or {},
                artifacts_spec_json=blueprint.artifacts_spec_json or {},
                runtime_constraints_json=blueprint.runtime_constraints_json or {},
                build_plan_summary=build_plan_metadata,
                planner_output=planner_output,
                llm_shadow_result=llm_shadow_result,
                llm_shadow_diff=llm_shadow_diff,
                planner_authority_metadata=authority_metadata,
            )
            manifest = self._manifest_for_build(blueprint, build, executable, generated_files, build_plan_metadata)
            manifest.setdefault("metadata", {})["build_planning_trace"] = {
                "stage": build_plan_stage_trace.stage if build_plan_stage_trace is not None else "build_planning",
                "prompt_contract_id": build_plan_stage_trace.prompt_contract_id if build_plan_stage_trace is not None else None,
                "outcome": build_plan_stage_trace.outcome if build_plan_stage_trace is not None else "not_requested",
                "fallback_reason": build_plan_stage_trace.fallback_reason if build_plan_stage_trace is not None else None,
                "machine_status": build_plan_stage_trace.machine_status if build_plan_stage_trace is not None else None,
                "validation_outcomes": [record.model_dump(mode="python") for record in build_plan_validation_records],
                "fallback_events": [event.model_dump(mode="python") for event in build_plan_fallback_events],
            }

            for step in build_step_repository.list_by_build(build.id):
                current_step = step
                step.status = BuildStepStatus.RUNNING.value
                step.started_at = datetime.now(timezone.utc)
                step.input_payload_json = {
                    "blueprint_id": blueprint.id,
                    "blueprint_type": blueprint.blueprint_type,
                    "build_version": build.build_version,
                }
                db.commit()

                output_payload = self._output_for_step(
                    step.step_name,
                    manifest,
                    generated_files,
                    build_dir,
                    build_plan_summary=build_plan_metadata,
                )
                token_usage = self._token_usage(step.step_name, output_payload)

                if step.step_name == "generate_manifest":
                    (build_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
                    artifact_repository.create(
                        Artifact(
                            build_id=build.id,
                            build_step_v2_id=step.id,
                            executable_id=executable.id,
                            artifact_type="manifest_file",
                            storage_kind="inline",
                            path_or_content=json.dumps(manifest, indent=2),
                            metadata_json={"filename": "manifest.json", "file_path": str(build_dir / "manifest.json")},
                        )
                    )
                if step.step_name == "materialize_executable":
                    for filename, content in generated_files.items():
                        self._write_generated_file(build_dir / filename, content)
                        artifact_repository.create(
                            Artifact(
                                build_id=build.id,
                                build_step_v2_id=step.id,
                                executable_id=executable.id,
                                artifact_type="generated_file",
                                storage_kind="inline",
                                path_or_content=self._stringify_content(content),
                                metadata_json={"filename": filename, "file_path": str(build_dir / filename)},
                            )
                        )

                step.output_payload_json = output_payload
                step.log_excerpt = f"[completed] {step.step_name}"
                step.token_usage_json = token_usage
                step.status = BuildStepStatus.COMPLETED.value
                step.finished_at = datetime.now(timezone.utc)
                token_usage_log_repository.create(
                    TokenUsageLog(
                        build_id=build.id,
                        build_step_v2_id=step.id,
                        provider="builder",
                        model_name="template",
                        prompt_tokens=token_usage["prompt_tokens"],
                        completion_tokens=token_usage["completion_tokens"],
                        total_tokens=token_usage["total_tokens"],
                        cost_estimate=Decimal("0"),
                    )
                )
                db.commit()
                log_event(logger, logging.INFO, "build.step.completed", build_id=build.id, step_name=step.step_name)
                time.sleep(self.delay_seconds)

            executable.manifest_json = manifest
            executable.generated_files_index_json = {
                "files": [
                    {
                        "filename": filename,
                        "file_path": str(build_dir / filename),
                        "artifact_type": "generated_file",
                    }
                    for filename in generated_files.keys()
                ]
            }
            executable.status = ExecutableStatus.READY.value
            build.manifest_snapshot_json = manifest
            build.summary = f"Built {executable.executable_type} executable with {len(generated_files)} generated files."
            build.status = BuildStatus.COMPLETED.value
            build.ended_at = datetime.now(timezone.utc)
            artifact_repository.create(
                Artifact(
                    build_id=build.id,
                    executable_id=executable.id,
                    artifact_type="build_summary",
                    storage_kind="inline",
                    path_or_content=build.summary,
                    metadata_json={"build_version": build.build_version},
                )
            )
            db.commit()
            log_event(logger, logging.INFO, "build.completed", build_id=build.id, executable_id=executable.id)
        except Exception as exc:
            db.rollback()
            build = build_repository.get_by_id(build_id)
            if build is not None:
                build.status = BuildStatus.FAILED.value
                build.error_message = str(exc)
                build.ended_at = datetime.now(timezone.utc)
            if current_step is not None:
                current_step.status = BuildStepStatus.FAILED.value
                current_step.finished_at = datetime.now(timezone.utc)
                current_step.log_excerpt = (current_step.log_excerpt or "") + f"\n[failed] {exc}"
            db.commit()
            log_event(logger, logging.ERROR, "build.failed", build_id=build_id, error=str(exc))
            raise
        finally:
            self.db = None
            db.close()

    # -----------------------------------------------------------------
    # Session-anchored C-slice (coding_agent narrow minimum).
    # -----------------------------------------------------------------

    def _execute_session_anchored_build(
        self,
        *,
        db,
        build: Build,
        build_repository: BuildRepository,
        build_step_repository: BuildStepRepository,
        executable_repository: ExecutableRepository,
        artifact_repository: ArtifactRepository,
        token_usage_log_repository: TokenUsageLogRepository,
    ) -> None:
        """Narrow path: session_id → planner_output → coding_agent executable.

        Scope intentionally limited to what the coding_agent demo needs:
        no Blueprint, no capability_gaps / capability_requests plumbing,
        no workflow_native_sidecar, no LLM-planner shadow. Uses the
        ``bindings_from_planner_output`` adapter to turn the persisted
        planner_output into ``steps.json`` + ``bindings.json`` and emits
        a minimal manifest that the existing resolver / runtime can
        consume.
        """
        from app.db.models import FrontChainState
        from app.schemas.workflow_planner import WorkflowPlannerOutput
        from app.services.build.planner_output_bindings import (
            bindings_from_planner_output,
        )

        current_step: BuildStepV2 | None = None
        try:
            build.status = BuildStatus.RUNNING.value
            build.started_at = datetime.now(timezone.utc)
            build.error_message = None
            db.commit()
            log_event(
                logger,
                logging.INFO,
                "build.session_anchored.started",
                build_id=build.id,
                session_id=build.session_id,
            )

            # 1. Load planner_output from the latest state row.
            state_row = (
                db.query(FrontChainState)
                .filter(
                    FrontChainState.session_id == build.session_id,
                    FrontChainState.planner_output_json.isnot(None),
                )
                .order_by(FrontChainState.created_at.desc())
                .first()
            )
            if state_row is None:
                raise RuntimeError(
                    "session has no planner_output — call /front-chain/sessions/{id}/plan first"
                )
            planner_output = WorkflowPlannerOutput.model_validate(
                state_row.planner_output_json
            )

            # Load the compile_context too — the session-anchored
            # adapter uses its input/output locators to thread concrete
            # paths into filesystem_read / filesystem_write bindings
            # and to compute a shared workspace_dir across coding_agent
            # nodes. Absence is tolerated (natural chain just can't
            # execute fs_read/fs_write).
            compile_context_for_adapter: Any | None = None
            if state_row.compile_context_json:
                try:
                    compile_context_for_adapter = CompileContextResponse.model_validate(
                        state_row.compile_context_json
                    )
                except Exception:  # noqa: BLE001 — defensive
                    compile_context_for_adapter = None

            # 2. Allocate executable + build dir (no blueprint_id here).
            build_dir = (
                ROOT_DIR / ".generated" / "executables" / build.project_id / build.build_version
            )
            build_dir.mkdir(parents=True, exist_ok=True)
            executable = Executable(
                project_id=build.project_id,
                blueprint_id=None,
                executable_type=ExecutableType.WORKFLOW_BUNDLE.value,
                source_path=str(build_dir),
                build_version=build.build_version,
                status=ExecutableStatus.DRAFT.value,
            )
            executable_repository.create(executable)
            build.executable_id = executable.id
            db.commit()

            # 3. Planner_output → (steps, bindings) via the step-4 adapter.
            steps, bindings = bindings_from_planner_output(
                planner_output, compile_context=compile_context_for_adapter
            )
            runnable_binding_names = [
                b.step_name for b in bindings if b.runnable
            ]
            blocked_binding_names = [
                b.step_name for b in bindings if not b.runnable
            ]
            log_event(
                logger,
                logging.INFO,
                "build.session_anchored.bindings",
                build_id=build.id,
                runnable=runnable_binding_names,
                blocked=blocked_binding_names,
            )

            # 4. Assemble the minimal file set. Keep the names aligned
            # with ExecutableManifestParser expectations so resolver can
            # load them without customisation.
            generated_files: dict[str, Any] = {}
            generated_files["steps.json"] = {"steps": steps}
            generated_files["bindings.json"] = ExecutionPackageBindingsDocument(
                bindings=bindings
            ).model_dump(mode="python")
            generated_files["runtime_entry.json"] = ExecutionPackageRuntimeEntry(
                entry_version="0.1",
                loader_kind="native_execution_package",
                execution_mode="execution-package",
                manifest_path="manifest.json",
                steps_source="steps.json",
                bindings_source="bindings.json",
                package_metadata_source="package_metadata.json",
                asset_index_source=None,
                supported_execution_kinds=["sandboxed_script", "filesystem", "verify", "blocked"],
                runnable_step_names=runnable_binding_names,
                blocked_step_names=blocked_binding_names,
                review_required_step_names=[
                    b.step_name for b in bindings if b.manual_review_required
                ],
                active_binding_step_names=runnable_binding_names,
                is_runnable=bool(runnable_binding_names),
                notes=[
                    "session_anchored_c_slice",
                    "only_coding_agent_runnable_in_this_slice",
                ],
            ).model_dump(mode="python")
            generated_files["package_metadata.json"] = ExecutionPackageMetadata(
                package_version="0.1",
                package_kind="native_execution_package",
                build_id=build.id,
                executable_id=executable.id,
                blueprint_id=None,
                executable_type=ExecutableType.WORKFLOW_BUNDLE.value,
                total_steps=len(bindings),
                bound_step_count=sum(1 for b in bindings if b.selected_capability_id),
                runnable_step_count=len(runnable_binding_names),
                blocked_step_count=len(blocked_binding_names),
                review_required_step_count=sum(
                    1 for b in bindings if b.manual_review_required
                ),
                bound_capability_ids=sorted(
                    {b.selected_capability_id for b in bindings if b.selected_capability_id}
                ),
                runnable_capability_ids=sorted(
                    {b.selected_capability_id for b in bindings if b.runnable and b.selected_capability_id}
                ),
                runtime_entry_ready=True,
                runtime_input_hints=[],
                notes=[
                    f"session_id={build.session_id}",
                    f"planner_state_id={state_row.id}",
                ],
            ).model_dump(mode="python")
            # Minimal schemas — the resolver only reads these as JSON.
            generated_files["input.schema.json"] = {"type": "object", "properties": {}}
            generated_files["output.schema.json"] = {"type": "object", "properties": {}}
            generated_files["runtime.defaults.json"] = {}

            # 5. Minimal manifest.
            manifest: dict[str, Any] = {
                "manifest_version": "0.1",
                "executable_id": executable.id,
                "build_id": build.id,
                "build_version": build.build_version,
                "project_id": build.project_id,
                "blueprint_id": None,
                # Manifest validator recognises "workflow" / "single_service"
                # / "multi_agent" only. Session-anchored bundles act as
                # workflows semantically (graph of steps with bindings).
                "executable_type": "workflow",
                "compatibility": {"mode": "session_anchored_c_slice"},
                "package": {
                    "bindings": "bindings.json",
                    "runtime_entry": "runtime_entry.json",
                    "package_metadata": "package_metadata.json",
                    "asset_index": None,
                },
                "entrypoint": {
                    "kind": "steps_file",
                    "path": "steps.json",
                    "handler": None,
                },
                "schemas": {
                    "input": "input.schema.json",
                    "output": "output.schema.json",
                    "config": "runtime.defaults.json",
                },
                "execution": {
                    "strategy": "sequential",
                    "steps_source": "steps.json",
                    "nodes_source": None,
                    "retry_policy_ref": None,
                    "timeout_seconds": None,
                },
                "runtime": {"default_env": {}, "env_required": []},
                "artifacts": {
                    "spec_file": None,
                    "default_outputs": ["run_summary", "step_output"],
                },
                "observability": {
                    "trace_enabled": True,
                    "step_logs": True,
                    "token_usage": True,
                    "metrics": [],
                },
                "files": [
                    {"path": "manifest.json", "kind": "manifest"},
                    *[
                        {"path": name, "kind": "generated"}
                        for name in generated_files.keys()
                    ],
                ],
                "metadata": {
                    "session_anchored_source": {
                        "session_id": build.session_id,
                        "planner_state_id": state_row.id,
                        "planner_version": state_row.planner_version,
                    },
                    "session_anchored_binding_summary": {
                        "runnable_steps": runnable_binding_names,
                        "blocked_steps": blocked_binding_names,
                    },
                },
            }

            # 6. Walk build steps (purely bookkeeping in this slice).
            for step in build_step_repository.list_by_build(build.id):
                current_step = step
                step.status = BuildStepStatus.RUNNING.value
                step.started_at = datetime.now(timezone.utc)
                step.input_payload_json = {
                    "session_id": build.session_id,
                    "build_version": build.build_version,
                }
                db.commit()

                if step.step_name == "generate_manifest":
                    (build_dir / "manifest.json").write_text(
                        json.dumps(manifest, indent=2), encoding="utf-8"
                    )
                    artifact_repository.create(
                        Artifact(
                            build_id=build.id,
                            build_step_v2_id=step.id,
                            executable_id=executable.id,
                            artifact_type="manifest_file",
                            storage_kind="inline",
                            path_or_content=json.dumps(manifest, indent=2),
                            metadata_json={
                                "filename": "manifest.json",
                                "file_path": str(build_dir / "manifest.json"),
                            },
                        )
                    )
                if step.step_name == "materialize_executable":
                    for filename, content in generated_files.items():
                        self._write_generated_file(build_dir / filename, content)
                        artifact_repository.create(
                            Artifact(
                                build_id=build.id,
                                build_step_v2_id=step.id,
                                executable_id=executable.id,
                                artifact_type="generated_file",
                                storage_kind="inline",
                                path_or_content=self._stringify_content(content),
                                metadata_json={
                                    "filename": filename,
                                    "file_path": str(build_dir / filename),
                                },
                            )
                        )

                step.output_payload_json = {
                    "step_name": step.step_name,
                    "runnable_bindings": runnable_binding_names,
                    "blocked_bindings": blocked_binding_names,
                }
                step.log_excerpt = f"[completed] {step.step_name} (session_anchored)"
                step.token_usage_json = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                }
                step.status = BuildStepStatus.COMPLETED.value
                step.finished_at = datetime.now(timezone.utc)
                token_usage_log_repository.create(
                    TokenUsageLog(
                        build_id=build.id,
                        build_step_v2_id=step.id,
                        provider="builder",
                        model_name="session_anchored",
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        cost_estimate=Decimal("0"),
                    )
                )
                db.commit()
                time.sleep(self.delay_seconds)

            executable.manifest_json = manifest
            executable.generated_files_index_json = {
                "files": [
                    {
                        "filename": filename,
                        "file_path": str(build_dir / filename),
                        "artifact_type": "generated_file",
                    }
                    for filename in generated_files.keys()
                ]
            }
            executable.status = ExecutableStatus.READY.value
            build.manifest_snapshot_json = manifest
            build.summary = (
                f"Session-anchored build: {len(runnable_binding_names)} runnable / "
                f"{len(blocked_binding_names)} blocked coding_agent-slice steps."
            )
            build.status = BuildStatus.COMPLETED.value
            build.ended_at = datetime.now(timezone.utc)
            artifact_repository.create(
                Artifact(
                    build_id=build.id,
                    executable_id=executable.id,
                    artifact_type="build_summary",
                    storage_kind="inline",
                    path_or_content=build.summary,
                    metadata_json={"build_version": build.build_version},
                )
            )
            db.commit()
            log_event(
                logger,
                logging.INFO,
                "build.session_anchored.completed",
                build_id=build.id,
                executable_id=executable.id,
                runnable=runnable_binding_names,
            )
        except Exception as exc:
            db.rollback()
            # Refetch build — rollback may have invalidated the ORM copy.
            build_refetched = build_repository.get_by_id(build.id)
            if build_refetched is not None:
                build_refetched.status = BuildStatus.FAILED.value
                build_refetched.error_message = str(exc)
                build_refetched.ended_at = datetime.now(timezone.utc)
            if current_step is not None:
                current_step.status = BuildStepStatus.FAILED.value
                current_step.finished_at = datetime.now(timezone.utc)
                current_step.log_excerpt = (
                    (current_step.log_excerpt or "") + f"\n[failed] {exc}"
                )
            db.commit()
            log_event(
                logger,
                logging.ERROR,
                "build.session_anchored.failed",
                build_id=build.id,
                error=str(exc),
            )
            raise

    def _executable_type_for_blueprint(self, blueprint_type: str) -> str:
        if blueprint_type == BlueprintType.WORKFLOW.value:
            return ExecutableType.WORKFLOW_BUNDLE.value
        if blueprint_type == BlueprintType.MULTI_AGENT.value:
            return ExecutableType.AGENT_GRAPH.value
        return ExecutableType.REPO.value

    def _build_files_for_blueprint(
        self,
        *,
        build_id: str,
        project_id: str,
        blueprint_type: str,
        blueprint_name: str,
        execution_steps_json: dict[str, Any],
        input_schema_json: dict[str, Any],
        output_schema_json: dict[str, Any],
        execution_env_json: dict[str, Any],
        configurable_vars_json: dict[str, Any],
        artifacts_spec_json: dict[str, Any],
        runtime_constraints_json: dict[str, Any],
        build_plan_summary: ExecutableBuildPlanSummary,
        planner_output: Any = None,
        llm_shadow_result: Any = None,
        llm_shadow_diff: dict[str, Any] | None = None,
        planner_authority_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        planned_steps = self._planned_execution_steps(
            execution_steps_json=execution_steps_json,
            build_plan_summary=build_plan_summary,
        )
        package_bindings, asset_index, asset_payloads = self._build_execution_package_bindings(
            project_id=project_id,
            execution_steps_json=execution_steps_json,
            build_plan_summary=build_plan_summary,
        )
        # --- Phase 4b: Auto-propose available_tools / loop_config (opt-in) ---
        # Only activates when runtime_constraints declares auto_propose_tools: true
        # or individual steps declare auto_propose_tools: true.
        auto_propose_enabled = bool(
            runtime_constraints_json.get("auto_propose_tools")
            or any(
                isinstance(s, dict) and s.get("auto_propose_tools")
                for s in (execution_steps_json.get("steps") or [])
            )
        )
        agentic_proposals = self._propose_agentic_config(
            planned_steps=planned_steps,
            package_bindings=package_bindings,
            planner_output=planner_output,
            enabled=auto_propose_enabled,
            runtime_constraints_json=runtime_constraints_json,
        )

        runtime_entry = self._build_runtime_entry(package_bindings)
        package_metadata = self._build_package_metadata(
            blueprint_type=blueprint_type,
            package_bindings=package_bindings,
            runtime_entry=runtime_entry,
            runtime_constraints_json=runtime_constraints_json,
        )
        workflow_native_legacy_planning_sidecar = self._build_workflow_native_legacy_planning_sidecar(
            planned_steps=planned_steps,
            build_plan_summary=build_plan_summary,
            runtime_constraints_json=runtime_constraints_json,
        )
        workflow_native_projection = self._build_workflow_native_projection(
            blueprint_type=blueprint_type,
            planned_steps=planned_steps,
            package_bindings=package_bindings,
            input_schema_json=input_schema_json,
            output_schema_json=output_schema_json,
            planner_output=planner_output,
        )
        workflow_native_verification = self._build_workflow_native_verification_sidecar(
            planned_steps=planned_steps,
            package_bindings=package_bindings,
            artifacts_spec_json=artifacts_spec_json,
        )
        workflow_native_execution_contract = self._build_workflow_native_execution_contract_sidecar(
            package_bindings=package_bindings,
            runtime_entry=runtime_entry,
            runtime_constraints_json=runtime_constraints_json,
            artifacts_spec_json=artifacts_spec_json,
        )
        workflow_native_materialization_record = self._build_workflow_native_materialization_record_sidecar(
            build_id=build_id,
            package_bindings=package_bindings,
            build_plan_summary=build_plan_summary,
        )
        workflow_native_artifact = self._build_workflow_native_artifact_sidecar(
            build_id=build_id,
            package_metadata=package_metadata,
            workflow_native_projection=workflow_native_projection,
            workflow_native_verification=workflow_native_verification,
            workflow_native_execution_contract=workflow_native_execution_contract,
            workflow_native_materialization_record=workflow_native_materialization_record,
        )

        # Store planner output as a workflow-native file
        planner_output_file: dict[str, Any] = {}
        planner_decision_surface: dict[str, Any] = {}
        if planner_output is not None:
            planner_output_file["workflow_native/planner_output.json"] = planner_output.model_dump(mode="python")
            # --- R2: Consume unresolved_items into build decision surface ---
            decision_surface, projection_downgrades = self._process_planner_unresolved_items(
                planner_output=planner_output,
                workflow_native_projection=workflow_native_projection,
                package_bindings=package_bindings,
            )
            # --- Phase 4b: Append agentic auto-proposal decisions ---
            if agentic_proposals:
                decision_surface.setdefault("agentic_auto_proposals", agentic_proposals)
                decision_surface["summary"]["agentic_proposal_count"] = len(agentic_proposals)
            # --- Planner authority resolution ---
            decision_surface["planner_authority"] = planner_authority_metadata or {}
            # --- LLM Shadow: summary in decision surface, full output in separate sidecar ---
            decision_surface["llm_shadow"] = {
                "llm_shadow_used": llm_shadow_result.success or llm_shadow_result.fallback_reason is not None,
                "llm_shadow_repaired": llm_shadow_result.repaired,
                "llm_shadow_diff_summary": llm_shadow_diff,
                "llm_shadow_fallback_reason": llm_shadow_result.fallback_reason,
                "llm_shadow_validation_issues": llm_shadow_result.validation_issues,
                "llm_shadow_output_ref": (
                    "workflow_native/planner_output.llm_shadow.json"
                    if llm_shadow_result.output is not None
                    else None
                ),
            }
            planner_decision_surface["workflow_native/planner_decision_surface.json"] = decision_surface
            # Store full LLM shadow output in its own sidecar file (not in decision surface)
            if llm_shadow_result.output is not None:
                planner_output_file["workflow_native/planner_output.llm_shadow.json"] = {
                    **llm_shadow_result.output.model_dump(mode="python"),
                    "_shadow_metadata": {
                        "repaired": llm_shadow_result.repaired,
                        "validation_issues": llm_shadow_result.validation_issues,
                        "stage_trace": llm_shadow_result.stage_trace,
                    },
                }
            planner_output_file.update(planner_decision_surface)

        if blueprint_type == BlueprintType.SINGLE_SERVICE.value:
            generated = {
                "service.descriptor.json": {
                    "service_name": blueprint_name,
                    "handler": "run_service",
                },
                "steps.json": {
                    "steps": planned_steps or [{"step_name": "run_service", "node_name": "service"}],
                },
                "input.schema.json": input_schema_json or {"type": "object"},
                "output.schema.json": output_schema_json or {"type": "object"},
                "config.schema.json": input_schema_json or {"type": "object"},
                "run_config.example.json": {"input": {}, "env": execution_env_json},
                "artifact_spec.json": artifacts_spec_json or {"artifacts": ["build_summary", "run_summary"]},
                "README.generated.md": f"# {blueprint_name}\n\nGenerated single service executable.\n",
                **workflow_native_legacy_planning_sidecar,
                **workflow_native_projection,
                **workflow_native_verification,
                **workflow_native_execution_contract,
                **workflow_native_materialization_record,
                **workflow_native_artifact,
                **planner_output_file,
            }
            return self._augment_with_execution_package(
                generated_files=generated,
                package_bindings=package_bindings,
                runtime_entry=runtime_entry,
                package_metadata=package_metadata,
                asset_index=asset_index,
                asset_payloads=asset_payloads,
            )
        if blueprint_type == BlueprintType.MULTI_AGENT.value:
            generated = {
                "steps.json": {
                    "steps": planned_steps or [{"step_name": "dispatch_agents", "node_name": "coordinator"}],
                },
                "input.schema.json": input_schema_json or {"type": "object"},
                "output.schema.json": output_schema_json or {"type": "object"},
                "runtime.defaults.json": {"env": execution_env_json, "vars": configurable_vars_json},
                "agent_graph.placeholder.json": {
                    "nodes": planned_steps,
                    "note": "Placeholder graph for multi_agent blueprint.",
                },
                "README.generated.md": f"# {blueprint_name}\n\nGenerated multi-agent placeholder executable.\n",
                **workflow_native_legacy_planning_sidecar,
                **workflow_native_projection,
                **workflow_native_verification,
                **workflow_native_execution_contract,
                **workflow_native_materialization_record,
                **workflow_native_artifact,
                **planner_output_file,
            }
            if artifacts_spec_json:
                generated["artifact_spec.json"] = artifacts_spec_json
            return self._augment_with_execution_package(
                generated_files=generated,
                package_bindings=package_bindings,
                runtime_entry=runtime_entry,
                package_metadata=package_metadata,
                asset_index=asset_index,
                asset_payloads=asset_payloads,
            )
        # Workflow blueprint: workflow.bundle.json / nodes.json / steps.json are legacy
        # compatibility artifacts preserved for CompatibilityStepScheduler fallback only.
        # Authoritative graph shape lives in workflow_native/workflow_graph.json.
        generated = {
            "workflow.bundle.json": {"name": blueprint_name, "steps": planned_steps, "_compat": "legacy_sequential"},
            "nodes.json": {"nodes": [step.get("node_name") for step in planned_steps if isinstance(step, dict)], "_compat": "legacy_sequential"},
            "steps.json": {"steps": planned_steps},
            "input.schema.json": input_schema_json or {"type": "object"},
            "output.schema.json": output_schema_json or {"type": "object"},
            "runtime.defaults.json": {"env": execution_env_json, "vars": configurable_vars_json},
            "README.generated.md": f"# {blueprint_name}\n\nGenerated workflow bundle executable.\n",
            **workflow_native_legacy_planning_sidecar,
            **workflow_native_projection,
            **workflow_native_verification,
            **workflow_native_execution_contract,
            **workflow_native_materialization_record,
            **workflow_native_artifact,
            **planner_output_file,
        }
        if artifacts_spec_json:
            generated["artifact_spec.json"] = artifacts_spec_json
        return self._augment_with_execution_package(
            generated_files=generated,
            package_bindings=package_bindings,
            runtime_entry=runtime_entry,
            package_metadata=package_metadata,
            asset_index=asset_index,
            asset_payloads=asset_payloads,
        )

    def _manifest_for_build(
        self,
        blueprint,
        build: Build,
        executable: Executable,
        generated_files: dict[str, Any],
        build_plan_summary: ExecutableBuildPlanSummary,
    ) -> dict[str, Any]:
        package_metadata = generated_files.get("package_metadata.json")
        if isinstance(package_metadata, dict):
            package_metadata.setdefault("build_id", build.id)
            package_metadata.setdefault("executable_id", executable.id)
            package_metadata.setdefault("blueprint_id", blueprint.id)
        if blueprint.blueprint_type == BlueprintType.SINGLE_SERVICE.value:
            entrypoint = {"kind": "service_descriptor", "path": "service.descriptor.json", "handler": "run_service"}
            schemas = {"input": "input.schema.json", "output": "output.schema.json", "config": "config.schema.json"}
            execution = {"strategy": "sequential", "steps_source": "steps.json", "nodes_source": None, "retry_policy_ref": None, "timeout_seconds": None}
            artifacts = {"spec_file": "artifact_spec.json", "default_outputs": list((blueprint.artifacts_spec_json or {}).get("default_outputs", ["run_summary", "step_output"]))}
        elif blueprint.blueprint_type == BlueprintType.MULTI_AGENT.value:
            entrypoint = {"kind": "agent_graph", "path": "agent_graph.placeholder.json", "handler": None}
            schemas = {"input": "input.schema.json", "output": "output.schema.json", "config": "runtime.defaults.json"}
            execution = {"strategy": "sequential", "steps_source": "steps.json", "nodes_source": "agent_graph.placeholder.json", "retry_policy_ref": None, "timeout_seconds": None}
            artifacts = {
                "spec_file": "artifact_spec.json" if "artifact_spec.json" in generated_files else None,
                "default_outputs": list((blueprint.artifacts_spec_json or {}).get("default_outputs", ["run_summary", "step_output", "agent_trace"])),
            }
        else:
            entrypoint = {"kind": "steps_file", "path": "workflow.bundle.json", "handler": None}
            schemas = {"input": "input.schema.json", "output": "output.schema.json", "config": "runtime.defaults.json"}
            execution = {"strategy": "sequential", "steps_source": "steps.json", "nodes_source": "nodes.json", "retry_policy_ref": None, "timeout_seconds": None}
            artifacts = {
                "spec_file": "artifact_spec.json" if "artifact_spec.json" in generated_files else None,
                "default_outputs": list((blueprint.artifacts_spec_json or {}).get("default_outputs", ["run_summary", "step_output"])),
            }

        base_files = [{"path": "manifest.json", "kind": "manifest"}]
        base_files.extend({"path": name, "kind": "generated"} for name in generated_files.keys())
        workflow_native_sidecar = {
            # Planner-first artifacts (authoritative)
            "planner_output": (
                "workflow_native/planner_output.json"
                if "workflow_native/planner_output.json" in generated_files
                else None
            ),
            "planner_decision_surface": (
                "workflow_native/planner_decision_surface.json"
                if "workflow_native/planner_decision_surface.json" in generated_files
                else None
            ),
            "planner_draft_entry": (
                "workflow_native/planner_output.json"
                if "workflow_native/planner_output.json" in generated_files
                else (
                    "workflow_native/workflow_planning_draft.json"
                    if "workflow_native/workflow_planning_draft.json" in generated_files
                    else None
                )
            ),
            # Build materialization artifacts
            "node_specs": "workflow_native/node_specs.json" if "workflow_native/node_specs.json" in generated_files else None,
            "node_instances": "workflow_native/node_instances.json" if "workflow_native/node_instances.json" in generated_files else None,
            "workflow_graph": "workflow_native/workflow_graph.json" if "workflow_native/workflow_graph.json" in generated_files else None,
            "verification_plan_draft": (
                "workflow_native/verification_plan.draft.json"
                if "workflow_native/verification_plan.draft.json" in generated_files
                else None
            ),
            "verification_plan_bound": (
                "workflow_native/verification_plan.bound.json"
                if "workflow_native/verification_plan.bound.json" in generated_files
                else None
            ),
            "execution_contract": (
                "workflow_native/execution_contract.json"
                if "workflow_native/execution_contract.json" in generated_files
                else None
            ),
            "build_materialization_record": (
                "workflow_native/build_materialization_record.json"
                if "workflow_native/build_materialization_record.json" in generated_files
                else None
            ),
            "workflow_artifact": (
                "workflow_native/workflow_artifact.json"
                if "workflow_native/workflow_artifact.json" in generated_files
                else None
            ),
            "preferred_explainability_entry": (
                "workflow_native/workflow_artifact.json"
                if "workflow_native/workflow_artifact.json" in generated_files
                else (
                    "workflow_native/workflow_graph.json"
                    if "workflow_native/workflow_graph.json" in generated_files
                    else None
                )
            ),
            "projection_applied": any(
                name in generated_files
                for name in (
                    "workflow_native/planner_output.json",
                    "workflow_native/node_specs.json",
                    "workflow_native/node_instances.json",
                    "workflow_native/workflow_graph.json",
                    "workflow_native/verification_plan.draft.json",
                    "workflow_native/verification_plan.bound.json",
                    "workflow_native/execution_contract.json",
                    "workflow_native/build_materialization_record.json",
                    "workflow_native/workflow_artifact.json",
                )
            ),
            "verification_binding_status": (
                generated_files.get("workflow_native/verification_plan.bound.json", {}) or {}
            ).get("verification_binding_status"),
            # Legacy compatibility sidecar — not authoritative for graph shape.
            # Prefer workflow_native/planner_output.json for the authoritative planner record.
            "legacy_planning_draft_sidecar": (
                "workflow_native/workflow_planning_draft.json"
                if "workflow_native/workflow_planning_draft.json" in generated_files
                else None
            ),
        }
        execution_package_compatibility_view = {
            "semantic_role": "legacy_execution_compatibility_view",
            "preferred_for": ["current_runtime_package_observability", "legacy_runtime_debugging"],
            "not_authoritative_for": ["workflow_native_explainability", "workflow_native_execution_policy"],
            "source_path": "package_metadata.json",
        }
        # Legacy sequential files — preserved for CompatibilityStepScheduler fallback only.
        # Authoritative execution structure lives in workflow_native/workflow_graph.json.
        runtime_compatibility_sources = {
            "workflow_bundle": {
                "path": "workflow.bundle.json" if "workflow.bundle.json" in generated_files else None,
                "semantic_role": "legacy_sequential_compatibility_source",
                "deprecated_by": "workflow_native/workflow_graph.json",
            },
            "steps": {
                "path": "steps.json" if "steps.json" in generated_files else None,
                "semantic_role": "legacy_sequential_compatibility_source",
                "deprecated_by": "workflow_native/workflow_graph.json",
            },
            "nodes": {
                "path": "nodes.json" if "nodes.json" in generated_files else None,
                "semantic_role": "legacy_sequential_compatibility_source",
                "deprecated_by": "workflow_native/workflow_graph.json",
            },
        }
        return {
            "manifest_version": "0.1",
            "project_id": build.project_id,
            "blueprint_id": blueprint.id,
            "executable_type": blueprint.blueprint_type,
            "executable_id": executable.id,
            "build_id": build.id,
            "build_version": build.build_version,
            "compatibility": {
                "mode": "native",
            },
            "package": {
                "bindings": "bindings.json",
                "runtime_entry": "runtime_entry.json",
                "package_metadata": "package_metadata.json",
                "asset_index": "assets/asset_index.json",
            },
            "entrypoint": entrypoint,
            "schemas": schemas,
            "execution": execution,
            "runtime": {
                "env_file": None,
                "default_env": dict(blueprint.execution_env_json or {}),
                "env_required": [],
                "resource_limits": {},
                "debug_defaults": {},
            },
            "artifacts": artifacts,
            "observability": {
                "trace_enabled": True,
                "step_logs": True,
                "token_usage": True,
                "metrics": ["latency", "tokens"],
            },
            "files": base_files,
            "metadata": {
                "generator": "agentforge-build-v0.1",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "build_planning": build_plan_summary.model_dump(mode="python"),
                "execution_package": generated_files.get("package_metadata.json", {}),
                "execution_package_compatibility_view": execution_package_compatibility_view,
                "runtime_compatibility_sources": runtime_compatibility_sources,
                "workflow_native_sidecar": workflow_native_sidecar,
            },
        }

    def _output_for_step(
        self,
        step_name: str,
        manifest: dict[str, Any],
        generated_files: dict[str, Any],
        build_dir: Path,
        *,
        build_plan_summary: ExecutableBuildPlanSummary,
    ) -> dict[str, Any]:
        if step_name == "analyze_blueprint":
            return {
                "summary": f"Blueprint analyzed with {len(generated_files)} target files.",
                "build_planning": build_plan_summary.model_dump(mode="python"),
                "execution_package": generated_files.get("package_metadata.json", {}),
                "execution_package_compatibility_view": {
                    "semantic_role": "legacy_execution_compatibility_view",
                },
            }
        if step_name == "generate_manifest":
            return {
                "summary": "Manifest generated.",
                "manifest": manifest,
                "build_planning": build_plan_summary.model_dump(mode="python"),
                "execution_package": generated_files.get("package_metadata.json", {}),
                "execution_package_compatibility_view": {
                    "semantic_role": "legacy_execution_compatibility_view",
                },
            }
        if step_name == "materialize_executable":
            return {
                "summary": f"Generated files written to {build_dir}.",
                "generated_files": list(generated_files.keys()),
                "review_required_steps": list(build_plan_summary.review_required_step_names),
                "execution_package": generated_files.get("package_metadata.json", {}),
                "execution_package_compatibility_view": {
                    "semantic_role": "legacy_execution_compatibility_view",
                },
            }
        return {"summary": "Executable finalized and indexed."}

    def _planned_execution_steps(
        self,
        *,
        execution_steps_json: dict[str, Any],
        build_plan_summary: ExecutableBuildPlanSummary,
    ) -> list[dict[str, Any]]:
        steps = [
            dict(step)
            for step in execution_steps_json.get("steps", [])
            if isinstance(step, dict)
        ]
        bindings_by_step = {
            item.step_name: item
            for item in build_plan_summary.proposed_bindings
        }
        for step in steps:
            step_name = str(step.get("step_name") or "")
            binding = bindings_by_step.get(step_name)
            if binding is None:
                continue
            if binding.logical_step_kind:
                step["logical_step_kind"] = binding.logical_step_kind
            if binding.selected_capability_id:
                step["selected_capability_id"] = binding.selected_capability_id
            if binding.fallback_capability_ids:
                step["fallback_capability_ids"] = list(binding.fallback_capability_ids)
            if binding.provider_requirements:
                step["provider_requirements"] = list(binding.provider_requirements)
            if binding.runtime_requirements:
                step["runtime_requirements"] = list(binding.runtime_requirements)
            if binding.config_hints:
                step["build_config_hints"] = dict(binding.config_hints)
            if binding.template_execution_kind:
                step["template_execution_kind"] = binding.template_execution_kind
            if binding.manual_review_required:
                step["manual_review_required"] = True
            if binding.ambiguities:
                step["build_planning_ambiguities"] = list(binding.ambiguities)
            step["build_realization"] = {
                "selected_source_kind": binding.selected_source_kind,
                "selected_source_ref": binding.selected_source_ref,
                "selected_materialization_id": binding.selected_materialization_id,
                "selection_reason": binding.selection_reason,
                "requested_hints": list(binding.requested_hints),
                "considered_candidate_ids": list(binding.considered_candidate_ids),
                "rejected_candidate_ids": list(binding.rejected_candidate_ids),
                "selected_candidate": binding.selected_candidate.model_dump(mode="python") if binding.selected_candidate is not None else None,
                "rejected_candidates": [item.model_dump(mode="python") for item in binding.rejected_candidates],
                "flexibility_explanation": (
                    binding.flexibility_explanation.model_dump(mode="python")
                    if binding.flexibility_explanation is not None
                    else None
                ),
            }
        return steps

    def _augment_with_execution_package(
        self,
        *,
        generated_files: dict[str, Any],
        package_bindings: list[ExecutionPackageBinding],
        runtime_entry: ExecutionPackageRuntimeEntry,
        package_metadata: ExecutionPackageMetadata,
        asset_index: ExecutionPackageAssetIndex,
        asset_payloads: dict[str, Any],
    ) -> dict[str, Any]:
        payload = dict(generated_files)
        payload["bindings.json"] = ExecutionPackageBindingsDocument(bindings=package_bindings).model_dump(mode="python")
        payload["runtime_entry.json"] = runtime_entry.model_dump(mode="python")
        payload["package_metadata.json"] = package_metadata.model_dump(mode="python")
        payload["assets/asset_index.json"] = asset_index.model_dump(mode="python")
        for asset in asset_index.assets:
            if asset.path in asset_payloads:
                payload[asset.path] = asset_payloads[asset.path]
            elif asset.kind == "template_stub":
                payload[asset.path] = {
                    "step_name": asset.step_name,
                    "capability_id": asset.capability_id,
                    "description": asset.description,
                    "review_required": asset.review_required,
                }
            elif asset.kind == "step_config":
                payload.setdefault(asset.path, {})
        return payload

    def _build_workflow_native_legacy_planning_sidecar(
        self,
        *,
        planned_steps: list[dict[str, Any]],
        build_plan_summary: ExecutableBuildPlanSummary,
        runtime_constraints_json: dict[str, Any],
    ) -> dict[str, Any]:
        """LEGACY COMPATIBILITY SIDECAR.

        Generates workflow_native/workflow_planning_draft.json as a compatibility artifact.
        This sidecar is NOT the authoritative planner record; that role belongs to
        workflow_native/planner_output.json produced by WorkflowPlannerService.

        Kept for tooling that reads workflow_planning_draft.json directly.
        """
        bindings_by_step = {item.step_name: item for item in build_plan_summary.proposed_bindings}
        missing_by_step = {item.step_name: item for item in build_plan_summary.missing_bindings}
        registry = CapabilityRegistryService()
        node_drafts: list[NodeDraft] = []
        edges: list[WorkflowEdge] = []
        previous_node_id: str | None = None
        unresolved_node_ids: list[str] = []
        has_explicit_deps = any(
            isinstance(step.get("depends_on"), list)
            for step in planned_steps
            if isinstance(step, dict)
        )
        # Build step_name -> draft_node_id map for depends_on resolution
        draft_node_id_by_step: dict[str, str] = {}
        for idx, step in enumerate(planned_steps, start=1):
            if isinstance(step, dict):
                sn = str(step.get("step_name") or f"step_{idx}")
                draft_node_id_by_step[sn] = f"draft_node_{idx}"

        admissibility_summary: dict[str, Any] = {}
        admissibility = self._load_compile_context_execution_admissibility(runtime_constraints_json)
        if admissibility is not None:
            admissibility_summary = {
                "primary_execution_regime": admissibility.primary_execution_regime,
                "max_automation_level": admissibility.max_automation_level,
                "build_admission_decision": admissibility.build_admission_decision,
                "admissibility_status": admissibility.admissibility_status,
            }

        capability_subset_summary: dict[str, list[str]] = {}
        for index, step in enumerate(planned_steps, start=1):
            step_name = str(step.get("step_name") or f"step_{index}")
            node_id = f"draft_node_{index}"
            node_name = str(step.get("node_name") or step_name)
            logical_step_kind = str(step.get("logical_step_kind") or "").strip()
            binding = bindings_by_step.get(step_name)
            missing = missing_by_step.get(step_name)
            candidate_capability_ids = [
                item.capability_id
                for item in registry.list_capabilities(context="build_planning", include_disabled=False)
                if logical_step_kind and logical_step_kind in (item.supported_step_kinds or [])
            ]
            capability_subset_summary[step_name] = list(candidate_capability_ids)
            preferred_capability_ids = list(binding.considered_candidate_ids) if binding is not None and binding.considered_candidate_ids else (
                [binding.selected_capability_id] if binding is not None and binding.selected_capability_id else []
            )
            unresolved_reasons: list[str] = []
            if missing is not None:
                unresolved_reasons.append(str(missing.reason))
                unresolved_reasons.extend(list(missing.notes))
            if binding is None and not candidate_capability_ids:
                unresolved_reasons.append("no_candidate_capabilities_for_step_kind")
            if binding is not None and binding.ambiguities:
                unresolved_reasons.extend(list(binding.ambiguities))
            if unresolved_reasons:
                unresolved_node_ids.append(node_id)
            # Determine upstream dependencies
            if has_explicit_deps:
                explicit_deps = step.get("depends_on")
                if isinstance(explicit_deps, list) and explicit_deps:
                    upstream_dep_ids = [
                        draft_node_id_by_step[str(d)]
                        for d in explicit_deps
                        if str(d) in draft_node_id_by_step and str(d) != step_name
                    ]
                else:
                    upstream_dep_ids = []
            else:
                upstream_dep_ids = [previous_node_id] if previous_node_id is not None else []

            node_drafts.append(
                NodeDraft(
                    draft_node_id=node_id,
                    step_name=step_name,
                    node_name=node_name,
                    node_semantic_type=self._workflow_native_node_semantic_type(logical_step_kind, binding),
                    semantic_goal=str(step.get("purpose") or step.get("output_intent") or logical_step_kind),
                    upstream_dependencies=upstream_dep_ids,
                    preferred_capability_ids=list(preferred_capability_ids),
                    candidate_capability_ids=list(candidate_capability_ids),
                    unresolved_reasons=unresolved_reasons,
                    review_required=bool(
                        (binding is not None and binding.manual_review_required)
                        or step_name in build_plan_summary.review_required_step_names
                    ),
                )
            )
            # Build edges: from depends_on if explicit, otherwise linear
            if has_explicit_deps:
                for dep_id in upstream_dep_ids:
                    edges.append(
                        WorkflowEdge(
                            edge_id=f"draft_edge_{dep_id}_{node_id}",
                            edge_type="data_flow",
                            from_node_id=dep_id,
                            to_node_id=node_id,
                            payload_mapping={"primary_output": "primary_input"},
                        )
                    )
            else:
                if previous_node_id is not None:
                    edges.append(
                        WorkflowEdge(
                            edge_id=f"draft_edge_{index - 1}_{index}",
                            edge_type="data_flow",
                            from_node_id=previous_node_id,
                            to_node_id=node_id,
                            payload_mapping={"primary_output": "primary_input"},
                        )
                    )
            previous_node_id = node_id

        draft_status = "complete"
        if unresolved_node_ids:
            draft_status = "review_required" if any(item.review_required for item in node_drafts) else "partial"
        elif build_plan_summary.review_required_step_names:
            draft_status = "review_required"

        # Compute entry/exit for DAG mode
        if has_explicit_deps and node_drafts:
            draft_successors: dict[str, set[str]] = {nd.draft_node_id: set() for nd in node_drafts}
            draft_predecessors: dict[str, set[str]] = {nd.draft_node_id: set() for nd in node_drafts}
            for edge in edges:
                draft_successors[edge.from_node_id].add(edge.to_node_id)
                draft_predecessors[edge.to_node_id].add(edge.from_node_id)
            draft_entry_nodes = sorted(nid for nid, preds in draft_predecessors.items() if not preds)
            draft_exit_nodes = sorted(nid for nid, succs in draft_successors.items() if not succs)
            if not draft_entry_nodes:
                draft_entry_nodes = [node_drafts[0].draft_node_id]
            if not draft_exit_nodes:
                draft_exit_nodes = [node_drafts[-1].draft_node_id]
        else:
            draft_entry_nodes = [node_drafts[0].draft_node_id] if node_drafts else []
            draft_exit_nodes = [node_drafts[-1].draft_node_id] if node_drafts else []

        planning_notes = [
            "LEGACY COMPATIBILITY SIDECAR: Prefer workflow_native/planner_output.json for the authoritative planner record.",
            "This artifact is a build-side transition sidecar generated from planned steps and build planning outputs.",
            "It is not a runtime-authoritative workflow graph.",
        ]
        if has_explicit_deps:
            planning_notes.append("Graph structure derived from explicit depends_on declarations.")

        graph_draft = WorkflowGraphDraft(
            draft_id="workflow_graph_draft_v1",
            node_drafts=node_drafts,
            edge_set=edges,
            entry_nodes=draft_entry_nodes,
            exit_nodes=draft_exit_nodes,
            unresolved_node_ids=unresolved_node_ids,
            draft_status=draft_status,
            planning_notes=planning_notes,
        )

        planning_draft = WorkflowPlanningDraft(
            draft_id="workflow_planning_draft_v1",
            workflow_graph_draft=graph_draft,
            verification_plan_draft=VerificationPlan(
                verification_targets=[],
                verification_methods=[],
                required_evidence=[],
                postconditions=["Planner draft remains materializable into workflow-native build objects."],
                acceptance_rules=[],
                approval_requirements=[],
                failure_on_unverified=False,
                recheck_policy="recheck_on_replan",
                verification_binding_status="draft",
            ),
            capability_subset_summary=capability_subset_summary,
            admissibility_summary=admissibility_summary,
            planning_notes=[
                "workflow_planning_draft is the preferred planner-side draft object for future workflow-native planning migration.",
                "Current source remains build_sidecar_transition_v1 until a dedicated WorkflowPlanner replaces this producer.",
            ],
        )
        return {
            "workflow_native/workflow_planning_draft.json": planning_draft.model_dump(mode="python"),
        }

    def _build_execution_package_bindings(
        self,
        *,
        project_id: str,
        execution_steps_json: dict[str, Any],
        build_plan_summary: ExecutableBuildPlanSummary,
    ) -> tuple[list[ExecutionPackageBinding], ExecutionPackageAssetIndex, dict[str, Any]]:
        steps = [dict(step) for step in execution_steps_json.get("steps", []) if isinstance(step, dict)]
        bindings_by_step = {item.step_name: item for item in build_plan_summary.proposed_bindings}
        missing_by_step = {item.step_name: item for item in build_plan_summary.missing_bindings}
        asset_index = ExecutionPackageAssetIndex()
        asset_payloads: dict[str, Any] = {}
        package_bindings: list[ExecutionPackageBinding] = []
        lifecycle_service = CapabilityLifecycleService(self.db)
        materialization_service = CapabilityMaterializationService(self.db)
        active_lifecycle_entries = {
            item.capability_id: item
            for item in lifecycle_service.repository.list_active_by_project(project_id)
        }
        for step in steps:
            step_name = str(step.get("step_name") or "")
            binding = bindings_by_step.get(step_name)
            if binding is None:
                legacy_binding = self._legacy_runtime_binding_for_step(step)
                if legacy_binding is not None:
                    package_bindings.append(legacy_binding)
                    continue
                missing = missing_by_step.get(step_name)
                package_bindings.append(
                    ExecutionPackageBinding(
                        step_name=step_name or "unknown_step",
                        logical_step_kind=step.get("logical_step_kind"),
                        binding_status="blocked",
                        runnable=False,
                        execution_kind="blocked",
                        blocked_reason=missing.reason if missing is not None else "no_capability_binding",
                        review_status="required" if step_name in build_plan_summary.review_required_step_names else "none",
                        review_notes=list(missing.notes if missing is not None else []),
                    )
                )
                continue
            package_binding, assets = self._runtime_binding_for_step(
                step=step,
                binding=binding,
                active_lifecycle_entries=active_lifecycle_entries,
                materialization_service=materialization_service,
            )
            asset_index.assets.extend(assets)
            if package_binding.materialization_id:
                materialization = materialization_service.repository.get_by_id(package_binding.materialization_id)
                if materialization is not None:
                    for item in list(materialization.materialized_asset_refs_json or []):
                        if isinstance(item, dict) and isinstance(item.get("path"), str):
                            asset_payloads[item["path"]] = dict(item.get("payload") or {})
            package_bindings.append(package_binding)
        return package_bindings, asset_index, asset_payloads

    def _build_workflow_native_projection(
        self,
        *,
        blueprint_type: str,
        planned_steps: list[dict[str, Any]],
        package_bindings: list[ExecutionPackageBinding],
        input_schema_json: dict[str, Any],
        output_schema_json: dict[str, Any],
        planner_output: Any = None,
    ) -> dict[str, Any]:
        bindings_by_step = {item.step_name: item for item in package_bindings}
        registry = CapabilityRegistryService()
        node_specs: list[NodeSpec] = []
        node_instances: list[NodeInstance] = []
        edges: list[WorkflowEdge] = []
        previous_node_id: str | None = None

        for index, step in enumerate(planned_steps, start=1):
            step_name = str(step.get("step_name") or f"step_{index}")
            node_name = str(step.get("node_name") or step_name)
            logical_step_kind = str(step.get("logical_step_kind") or "").strip()
            binding = bindings_by_step.get(step_name)
            projection = (
                registry.build_planner_capability_projection(binding.selected_capability_id)
                if binding is not None and binding.selected_capability_id and registry.get_capability(binding.selected_capability_id) is not None
                else None
            )
            spec = NodeSpec(
                spec_id=f"{step_name}_spec",
                node_semantic_type=self._workflow_native_node_semantic_type(logical_step_kind, binding),
                input_port_specs=self._workflow_native_input_port_specs(step),
                output_port_specs=self._workflow_native_output_port_specs(step, binding),
                capability_requirements=[] if projection is None else [
                    {
                        "semantic_goal": str(step.get("purpose") or step.get("output_intent") or logical_step_kind),
                        "allowed_capability_ids": [projection.capability_id],
                        "preferred_capability_ids": [projection.capability_id],
                        "runtime_constraints": list(projection.runtime_constraints),
                    }
                ],
                side_effect_class=projection.side_effect_class if projection is not None else "none",
                intervention_policy={
                    "allowed_modes": ["inspect"] + (["approve"] if binding is not None and (binding.requires_confirmation or binding.manual_review_required) else []),
                    "required_before_execute": bool(binding.requires_confirmation) if binding is not None else False,
                    "required_before_commit": bool(binding.manual_review_required) if binding is not None else False,
                    "editable_fields": [],
                    "skip_policy": "forbidden",
                    "resume_policy": "resume_from_current",
                },
                rerun_policy={
                    "rerun_allowed": bool(binding is not None and binding.execution_kind not in {"artifact_writer", "blocked"}),
                    "rerun_requires_human_approval": bool(binding is not None and (binding.requires_confirmation or binding.manual_review_required)),
                    "rerun_invalidates_downstream": True,
                    "rerun_creates_branch": False,
                },
                observability_contract={
                    "hooks": list(projection.observability_support if projection is not None else ["execution_trace", "output_snapshot"]),
                    "metrics": ["latency"],
                    "evidence_outputs": list(step.get("produces_evidence_types") or []),
                },
                testability_contract={
                    "dry_run_supported": True,
                    "fixture_inputs": [],
                    "mockable_capability_binding": True,
                    "deterministic_check_strategy": "schema_and_contract_check",
                    "expected_output_schema": "step_output",
                    "replayable_trace": True,
                    "postcondition_tests": [],
                },
                verification_contract={
                    "postconditions": [f"{step_name} completes without blocked runtime binding."],
                    "verification_evidence": list(step.get("produces_evidence_types") or []),
                    "approval_required": bool(binding is not None and (binding.requires_confirmation or binding.manual_review_required)),
                },
                admissible_execution_regimes=list(projection.admissible_execution_regimes if projection is not None else []),
            )
            node_specs.append(spec)

            node_id = f"node_{index}"
            binding_resolution_notes: list[str] = []
            if binding is not None:
                if binding.source_ref:
                    binding_resolution_notes.append(f"source_ref:{binding.source_ref}")
                if binding.review_notes:
                    binding_resolution_notes.extend(binding.review_notes)
                if binding.blocked_reason:
                    binding_resolution_notes.append(f"blocked:{binding.blocked_reason}")
            node_instances.append(
                NodeInstance(
                    node_id=node_id,
                    spec_ref=spec.spec_id,
                    bound_capability=NodeCapabilityBinding(
                        capability_id=binding.selected_capability_id if binding is not None else None,
                        invocation_mode=binding.execution_kind if binding is not None else None,
                        required_inputs=[item.port_name for item in spec.input_port_specs if item.required],
                        produced_outputs=[item.port_name for item in spec.output_port_specs],
                        side_effect_class=(projection.side_effect_class if projection is not None else None),
                        fallback_capabilities=list(step.get("fallback_capability_ids") or []),
                        execution_constraints=list(binding.runtime_requirements if binding is not None else []),
                    ),
                    binding_resolution_notes=binding_resolution_notes,
                    concrete_config=dict(binding.config_hints if binding is not None else {}),
                    input_wiring=(
                        [{"target_input_port": "primary_input", "source_kind": "graph_input", "source_ref": "request_input"}]
                        if previous_node_id is None
                        else [{"target_input_port": "primary_input", "source_kind": "node_output", "source_ref": previous_node_id}]
                    ),
                    local_policy_overrides={
                        "manual_review_required": bool(binding.manual_review_required) if binding is not None else False,
                        "review_status": binding.review_status if binding is not None else None,
                    },
                    local_verification_bindings={"postcondition": f"{step_name} emitted expected output contract."},
                    local_intervention_bindings={"requires_confirmation": bool(binding.requires_confirmation) if binding is not None else False},
                    instance_execution_hints={
                        "blueprint_type": blueprint_type,
                        "node_name": node_name,
                        "step_name": step_name,
                        "step_order": index,
                    },
                    materialization_status=(
                        "resolved"
                        if binding is not None and binding.runnable
                        else ("partially_resolved" if binding is not None and binding.binding_status == "review_needed" else "unresolved")
                    ),
                )
            )
            if previous_node_id is not None:
                edges.append(
                    WorkflowEdge(
                        edge_id=f"edge_{index - 1}_{index}",
                        edge_type="data_flow",
                        from_node_id=previous_node_id,
                        to_node_id=node_id,
                        payload_mapping={"primary_output": "primary_input"},
                    )
                )
            previous_node_id = node_id

        # --- Determine graph shape: planner-first, depends_on fallback, or linear fallback ---
        if (
            planner_output is not None
            and hasattr(planner_output, "edge_drafts")
            and planner_output.edge_drafts
            and hasattr(planner_output, "node_drafts")
            and planner_output.node_drafts
            and node_instances
        ):
            # Primary path: planner owns graph shape (edges, entry/exit)
            edges, entry_node_ids, exit_node_ids, graph_invariant_id, graph_invariant_desc = (
                self._build_edges_from_planner_output(planner_output, node_instances)
            )
        else:
            # Fallback: planner produced no edges — use explicit depends_on or linear order
            has_explicit_deps = any(
                isinstance(step.get("depends_on"), list)
                for step in planned_steps
                if isinstance(step, dict)
            )
            if has_explicit_deps and node_instances:
                edges, entry_node_ids, exit_node_ids, graph_invariant_id, graph_invariant_desc = (
                    self._build_dag_edges_from_depends_on(planned_steps, node_instances)
                )
            elif node_instances:
                entry_node_ids = [node_instances[0].node_id]
                exit_node_ids = [node_instances[-1].node_id]
                graph_invariant_id = "planner_fallback_linear"
                graph_invariant_desc = "Planner produced no edge drafts; fell back to sequential linear order."
            else:
                entry_node_ids = []
                exit_node_ids = []
                graph_invariant_id = "empty_graph"
                graph_invariant_desc = "No nodes in graph."

        graph = WorkflowGraph(
            graph_id="workflow_native_projection_v1",
            node_instances=node_instances,
            edge_set=edges,
            entry_nodes=entry_node_ids,
            exit_nodes=exit_node_ids,
            graph_inputs=[
                NodePortSpec(
                    port_name="request_input",
                    schema_summary=str(input_schema_json or {"type": "object"}),
                    semantic_role="workflow_input",
                    required=True,
                )
            ],
            graph_outputs=[
                NodePortSpec(
                    port_name="workflow_output",
                    schema_summary=str(output_schema_json or {"type": "object"}),
                    semantic_role="workflow_output",
                    required=True,
                )
            ],
            graph_invariants=[
                GraphInvariant(
                    invariant_id=graph_invariant_id,
                    description=graph_invariant_desc,
                    severity="info",
                )
            ],
        )

        return {
            "workflow_native/node_specs.json": {"node_specs": [item.model_dump(mode="python") for item in node_specs]},
            "workflow_native/node_instances.json": {"node_instances": [item.model_dump(mode="python") for item in node_instances]},
            "workflow_native/workflow_graph.json": graph.model_dump(mode="python"),
        }

    def _build_edges_from_planner_output(
        self,
        planner_output: Any,
        node_instances: list[NodeInstance],
    ) -> tuple[list[WorkflowEdge], list[str], list[str], str, str]:
        """Build workflow graph edges from WorkflowPlannerOutput.

        The planner owns the graph shape (edges, entry/exit).
        Build owns the materialization (node_instances carry bindings).

        Maps planner draft_node_ids to build node_ids via step_name matching.
        Falls back to sequential edges if mapping fails.
        """
        # Build mapping: planner draft_node_id → step_name → build node_id
        draft_to_step: dict[str, str] = {}
        for draft in planner_output.node_drafts:
            if draft.step_name:
                draft_to_step[draft.draft_node_id] = draft.step_name

        node_id_by_step: dict[str, str] = {}
        for node in node_instances:
            hints = node.instance_execution_hints or {}
            step_name = str(hints.get("step_name") or node.spec_ref or node.node_id)
            node_id_by_step[step_name] = node.node_id

        draft_to_build: dict[str, str] = {}
        for draft_id, step_name in draft_to_step.items():
            if step_name in node_id_by_step:
                draft_to_build[draft_id] = node_id_by_step[step_name]

        # Verify all drafts mapped successfully
        if len(draft_to_build) != len(planner_output.node_drafts):
            log_event(
                logger, logging.WARNING, "build.planner_output.incomplete_mapping",
                mapped=len(draft_to_build), total=len(planner_output.node_drafts),
            )
            # Fall back to sequential
            return (
                [],
                [node_instances[0].node_id] if node_instances else [],
                [node_instances[-1].node_id] if node_instances else [],
                "planner_mapping_fallback",
                "Planner output could not be fully mapped to build nodes; fell back to sequential.",
            )

        # Build edges from planner edge_drafts
        edges: list[WorkflowEdge] = []
        for edge_draft in planner_output.edge_drafts:
            from_build_id = draft_to_build.get(edge_draft.from_node_id)
            to_build_id = draft_to_build.get(edge_draft.to_node_id)
            if from_build_id and to_build_id:
                edges.append(
                    WorkflowEdge(
                        edge_id=edge_draft.draft_edge_id,
                        edge_type=edge_draft.edge_type or "data_flow",
                        from_node_id=from_build_id,
                        to_node_id=to_build_id,
                        payload_mapping=edge_draft.payload_mapping or {"primary_output": "primary_input"},
                    )
                )

        # Compute entry/exit from graph structure
        nodes_with_predecessors = {e.to_node_id for e in edges}
        nodes_with_successors = {e.from_node_id for e in edges}
        all_build_ids = [n.node_id for n in node_instances]

        entry_ids = [nid for nid in all_build_ids if nid not in nodes_with_predecessors]
        exit_ids = [nid for nid in all_build_ids if nid not in nodes_with_successors]
        if not entry_ids and node_instances:
            entry_ids = [node_instances[0].node_id]
        if not exit_ids and node_instances:
            exit_ids = [node_instances[-1].node_id]

        is_dag = len(entry_ids) == 1 and (
            any(len([e for e in edges if e.from_node_id == nid]) > 1 for nid in all_build_ids)
            or any(len([e for e in edges if e.to_node_id == nid]) > 1 for nid in all_build_ids)
        )

        graph_kind = "planner_dag_projection" if is_dag else "planner_linear_projection"
        graph_desc = (
            f"Graph structure materialized from WorkflowPlannerOutput ({len(edges)} edges, "
            f"{len(entry_ids)} entry, {len(exit_ids)} exit). Planner version: {planner_output.planner_version}."
        )
        return edges, entry_ids, exit_ids, graph_kind, graph_desc

    # --- Phase 4b: Agentic auto-proposal ---

    # Mapping from node semantic_type to proposed read-only tools.
    # Only low-risk, read-only tools are eligible for auto-proposal.
    _SEMANTIC_TYPE_TOOL_PROPOSALS: dict[str, list[str]] = {
        "acquire": ["web_search", "fetch_url", "read_file"],
        "transform": ["read_file"],
    }
    # Logical step kinds that imply research / data gathering intent,
    # eligible for the broader "acquire"-level tool set even on transform nodes.
    _RESEARCH_STEP_KINDS: set[str] = {
        "research", "gather", "collect", "search", "fetch",
        # "analyze" excluded: analysis consumes previous outputs, doesn't gather new data
    }
    _DEFAULT_LOOP_CONFIG: dict[str, Any] = {
        "max_iterations": 5,
        "max_duration_seconds": 60,
        "max_total_tokens": 30_000,
    }

    def _propose_agentic_config(
        self,
        *,
        planned_steps: list[dict[str, Any]],
        package_bindings: list[ExecutionPackageBinding],
        planner_output: Any = None,
        enabled: bool = False,
        runtime_constraints_json: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Auto-propose available_tools / loop_config for eligible nodes.

        Rules:
        - Only active when enabled=True (opt-in via runtime_constraints or step-level flag).
        - Only propose if binding.config_hints does not already declare available_tools.
        - Only propose read-only tools: web_search, fetch_url, read_file, browse.
        - Proposal based on node_semantic_type (from planner or logical_step_kind).
        - acquire nodes → [web_search, fetch_url, read_file]
        - transform nodes with research-like step_kind → same as acquire
        - other transform nodes → [read_file] only
        - act / verify / human_gate → no proposal (potential side effects)
        - Mutates binding.config_hints in place so downstream projection picks it up.
        - Returns a list of proposal decision records for the decision surface.
        """
        if not enabled:
            return []

        proposals: list[dict[str, Any]] = []
        bindings_by_step = {b.step_name: b for b in package_bindings}

        # Build planner semantic type lookup
        planner_semantic_types: dict[str, str] = {}
        if planner_output is not None and hasattr(planner_output, "node_drafts") and planner_output.node_drafts:
            for draft in planner_output.node_drafts:
                if draft.step_name and draft.node_semantic_type:
                    planner_semantic_types[draft.step_name] = draft.node_semantic_type

        for step in planned_steps:
            if not isinstance(step, dict):
                continue
            step_name = str(step.get("step_name") or "")
            if not step_name:
                continue
            binding = bindings_by_step.get(step_name)
            if binding is None:
                continue

            # Skip if already has available_tools declared
            existing_hints = dict(binding.config_hints or {})
            if isinstance(existing_hints.get("available_tools"), list) and existing_hints["available_tools"]:
                proposals.append({
                    "step_name": step_name,
                    "action": "skipped_already_declared",
                    "reason": "config_hints already declares available_tools",
                    "existing_tools": existing_hints["available_tools"],
                })
                continue

            # Skip blocked / non-runnable bindings
            if not binding.runnable or binding.binding_status == "blocked":
                continue

            # Determine semantic type: prefer planner, then planner-level mapping, then build-level mapping
            logical_kind = str(step.get("logical_step_kind") or binding.logical_step_kind or "").strip()
            from app.services.workflow_planner_service import _STEP_KIND_TO_SEMANTIC_TYPE
            semantic_type = (
                planner_semantic_types.get(step_name)
                or _STEP_KIND_TO_SEMANTIC_TYPE.get(logical_kind)
                or self._workflow_native_node_semantic_type(logical_kind, binding)
            )

            # Only propose for acquire / transform
            if semantic_type not in self._SEMANTIC_TYPE_TOOL_PROPOSALS:
                proposals.append({
                    "step_name": step_name,
                    "action": "skipped_semantic_type",
                    "reason": f"semantic_type '{semantic_type}' not eligible for auto-proposal",
                    "semantic_type": semantic_type,
                })
                continue

            # Determine tool set
            if semantic_type == "acquire" or logical_kind in self._RESEARCH_STEP_KINDS:
                proposed_tools = ["web_search", "fetch_url", "read_file"]
            else:
                proposed_tools = list(self._SEMANTIC_TYPE_TOOL_PROPOSALS.get(semantic_type, []))

            if not proposed_tools:
                continue

            # Apply proposal: mutate binding.config_hints
            if binding.config_hints is None:
                binding.config_hints = {}
            binding.config_hints["available_tools"] = proposed_tools
            # Use overrides from runtime_constraints if available
            loop_overrides = (runtime_constraints_json or {}).get("agentic_loop_overrides", {})
            loop_config = dict(self._DEFAULT_LOOP_CONFIG)
            if isinstance(loop_overrides, dict):
                for key in ("max_iterations", "max_duration_seconds", "max_total_tokens"):
                    if key in loop_overrides:
                        loop_config[key] = loop_overrides[key]
            binding.config_hints["loop_config"] = loop_config
            binding.config_hints["_agentic_auto_proposed"] = True

            proposals.append({
                "step_name": step_name,
                "action": "proposed",
                "semantic_type": semantic_type,
                "logical_step_kind": logical_kind,
                "proposed_tools": proposed_tools,
                "loop_config": dict(self._DEFAULT_LOOP_CONFIG),
                "source": "build_auto_proposal_v1",
            })

        return proposals

    def _process_planner_unresolved_items(
        self,
        planner_output: Any,
        workflow_native_projection: dict[str, Any],
        package_bindings: list[ExecutionPackageBinding],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Process planner unresolved_items into build decisions.

        Returns:
        - decision_surface: dict for planner_decision_surface.json
        - projection_downgrades: list of downgrade records applied to nodes

        Consumes planner unresolved_items and:
        - Maps capability_gap → build warning + materialization downgrade
        - Maps semantic_ambiguity → build warning
        - Maps constraint_violation → build warning (blocking severity)
        - Maps clarification_needed → review-needed marker
        - Maps review_needed → review-needed marker
        """
        if planner_output is None or not hasattr(planner_output, "unresolved_items"):
            return {"decisions": [], "summary": {"total": 0}}, []

        unresolved = planner_output.unresolved_items or []
        bindings_by_step = {b.step_name: b for b in package_bindings}

        decisions: list[dict[str, Any]] = []
        downgrades: list[dict[str, Any]] = []
        warning_count = 0
        review_needed_count = 0
        downgrade_count = 0

        for item in unresolved:
            item_dict = item.model_dump(mode="python") if hasattr(item, "model_dump") else dict(item)
            kind = item_dict.get("item_kind", "")
            severity = item_dict.get("severity", "info")
            related_nodes = item_dict.get("related_node_ids", [])
            description = item_dict.get("description", "")

            # Determine the step names affected via planner node → step mapping
            affected_steps: list[str] = []
            for draft in (planner_output.node_drafts or []):
                if draft.draft_node_id in related_nodes and draft.step_name:
                    affected_steps.append(draft.step_name)

            # Decision: what does build do with this unresolved item?
            decision_action = "warning"
            materialization_impact = "none"

            if kind == "capability_gap":
                decision_action = "warning"
                materialization_impact = "downgrade_to_partially_resolved"
                warning_count += 1
                downgrade_count += 1
                for step_name in affected_steps:
                    downgrades.append({
                        "step_name": step_name,
                        "reason": f"planner_capability_gap: {description}",
                        "original_status": "resolved",
                        "downgraded_to": "partially_resolved",
                        "source_item_id": item_dict.get("item_id"),
                    })
            elif kind == "semantic_ambiguity":
                decision_action = "warning"
                materialization_impact = "none"
                warning_count += 1
            elif kind == "constraint_violation":
                decision_action = "warning" if severity != "blocking" else "blocking_warning"
                materialization_impact = "downgrade_to_unresolved" if severity == "blocking" else "none"
                warning_count += 1
                if severity == "blocking":
                    downgrade_count += 1
                    for step_name in affected_steps:
                        downgrades.append({
                            "step_name": step_name,
                            "reason": f"planner_constraint_violation: {description}",
                            "original_status": "resolved",
                            "downgraded_to": "unresolved",
                            "source_item_id": item_dict.get("item_id"),
                        })
            elif kind in ("clarification_needed", "review_needed"):
                decision_action = "review_needed"
                materialization_impact = "review_marker"
                review_needed_count += 1

            decisions.append({
                "source_item_id": item_dict.get("item_id"),
                "source_item_kind": kind,
                "source_severity": severity,
                "affected_steps": affected_steps,
                "decision_action": decision_action,
                "materialization_impact": materialization_impact,
                "description": description,
                "suggested_resolution": item_dict.get("suggested_resolution"),
            })

        # Apply materialization downgrades to the workflow graph nodes
        wg_dict = workflow_native_projection.get("workflow_native/workflow_graph.json")
        if isinstance(wg_dict, dict):
            downgrade_by_step = {}
            for dg in downgrades:
                step = dg["step_name"]
                if step not in downgrade_by_step or dg["downgraded_to"] == "unresolved":
                    downgrade_by_step[step] = dg["downgraded_to"]

            for node in (wg_dict.get("node_instances") or []):
                if isinstance(node, dict):
                    hints = node.get("instance_execution_hints", {}) or {}
                    step_name = hints.get("step_name")
                    if step_name and step_name in downgrade_by_step:
                        current_status = node.get("materialization_status", "resolved")
                        target_status = downgrade_by_step[step_name]
                        # Only downgrade, never upgrade
                        if current_status == "resolved" or (current_status == "partially_resolved" and target_status == "unresolved"):
                            node["materialization_status"] = target_status
                            node.setdefault("planner_unresolved_markers", []).append({
                                "reason": downgrade_by_step[step_name],
                                "source": "planner_unresolved_items",
                            })

        decision_surface = {
            "planner_version": planner_output.planner_version,
            "planner_draft_status": planner_output.draft_status,
            "total_unresolved": len(unresolved),
            "decisions": decisions,
            "summary": {
                "total": len(decisions),
                "warning_count": warning_count,
                "review_needed_count": review_needed_count,
                "downgrade_count": downgrade_count,
                "affected_step_names": sorted({s for d in decisions for s in d["affected_steps"]}),
            },
        }
        return decision_surface, downgrades

    def _build_dag_edges_from_depends_on(
        self,
        planned_steps: list[dict[str, Any]],
        node_instances: list[NodeInstance],
    ) -> tuple[list[WorkflowEdge], list[str], list[str], str, str]:
        """Build DAG edges from explicit depends_on declarations.

        Returns (edges, entry_node_ids, exit_node_ids, invariant_id, invariant_desc).

        Rules:
        - depends_on must reference existing step names
        - No self-dependencies
        - No duplicate dependencies
        - Cycle detection via topological sort
        - Steps with no depends_on and no predecessors become entry nodes
        - Steps with no successors become exit nodes
        - Falls back to linear chain on validation failure
        """
        node_id_by_step: dict[str, str] = {}
        for node in node_instances:
            hints = node.instance_execution_hints or {}
            step_name = str(hints.get("step_name") or node.spec_ref or node.node_id)
            node_id_by_step[step_name] = node.node_id

        all_step_names = set(node_id_by_step.keys())
        edges: list[WorkflowEdge] = []
        successors: dict[str, set[str]] = {name: set() for name in all_step_names}
        predecessors: dict[str, set[str]] = {name: set() for name in all_step_names}
        edge_counter = 0

        for step in planned_steps:
            if not isinstance(step, dict):
                continue
            step_name = str(step.get("step_name") or "")
            depends_on = step.get("depends_on")
            if not isinstance(depends_on, list) or not depends_on:
                continue

            seen_deps: set[str] = set()
            for dep in depends_on:
                dep_str = str(dep)
                # Validation: skip invalid dependencies
                if dep_str == step_name:
                    log_event(logger, logging.WARNING, "build.dag.self_dependency", step_name=step_name)
                    continue
                if dep_str not in all_step_names:
                    log_event(logger, logging.WARNING, "build.dag.missing_dependency", step_name=step_name, dependency=dep_str)
                    continue
                if dep_str in seen_deps:
                    continue
                seen_deps.add(dep_str)

                from_node_id = node_id_by_step[dep_str]
                to_node_id = node_id_by_step[step_name]
                edge_counter += 1
                edges.append(
                    WorkflowEdge(
                        edge_id=f"edge_dep_{edge_counter}",
                        edge_type="data_flow",
                        from_node_id=from_node_id,
                        to_node_id=to_node_id,
                        payload_mapping={"primary_output": "primary_input"},
                    )
                )
                successors[dep_str].add(step_name)
                predecessors[step_name].add(dep_str)

        # Cycle detection via Kahn's algorithm
        in_degree = {name: len(preds) for name, preds in predecessors.items()}
        queue = sorted(name for name, deg in in_degree.items() if deg == 0)
        topo_order: list[str] = []
        while queue:
            name = queue.pop(0)
            topo_order.append(name)
            for succ in sorted(successors[name]):
                in_degree[succ] -= 1
                if in_degree[succ] == 0:
                    queue.append(succ)

        if len(topo_order) != len(all_step_names):
            # Cycle detected — fall back to linear chain
            log_event(logger, logging.ERROR, "build.dag.cycle_detected",
                      expected=len(all_step_names), sorted_count=len(topo_order))
            linear_edges: list[WorkflowEdge] = []
            prev_node_id: str | None = None
            for idx, node in enumerate(node_instances, start=1):
                if prev_node_id is not None:
                    linear_edges.append(
                        WorkflowEdge(
                            edge_id=f"edge_{idx - 1}_{idx}",
                            edge_type="data_flow",
                            from_node_id=prev_node_id,
                            to_node_id=node.node_id,
                            payload_mapping={"primary_output": "primary_input"},
                        )
                    )
                prev_node_id = node.node_id
            return (
                linear_edges,
                [node_instances[0].node_id],
                [node_instances[-1].node_id],
                "dag_cycle_fallback_to_linear",
                "Cycle detected in depends_on declarations. Fell back to linear execution order.",
            )

        # Determine entry nodes (no predecessors) and exit nodes (no successors)
        entry_node_ids = sorted(
            node_id_by_step[name] for name in all_step_names if not predecessors[name]
        )
        exit_node_ids = sorted(
            node_id_by_step[name] for name in all_step_names if not successors[name]
        )

        # Safety: ensure at least one entry and one exit
        if not entry_node_ids:
            entry_node_ids = [node_instances[0].node_id]
        if not exit_node_ids:
            exit_node_ids = [node_instances[-1].node_id]

        return (
            edges,
            entry_node_ids,
            exit_node_ids,
            "explicit_dag_from_depends_on",
            "Graph edges generated from explicit depends_on declarations in execution_steps_json.",
        )

    def _build_workflow_native_verification_sidecar(
        self,
        *,
        planned_steps: list[dict[str, Any]],
        package_bindings: list[ExecutionPackageBinding],
        artifacts_spec_json: dict[str, Any],
    ) -> dict[str, Any]:
        bindings_by_step = {item.step_name: item for item in package_bindings}
        draft_targets: list[NodeVerificationTarget] = []
        draft_methods: list[VerificationMethod] = []
        bound_methods: list[VerificationMethod] = []
        acceptance_rules: list[AcceptanceRule] = []
        approval_requirements: list[ApprovalRequirement] = []
        required_evidence: list[str] = []
        seen_evidence: set[str] = set()

        for index, step in enumerate(planned_steps, start=1):
            step_name = str(step.get("step_name") or f"step_{index}")
            node_id = f"node_{index}"
            logical_step_kind = str(step.get("logical_step_kind") or "transform")
            binding = bindings_by_step.get(step_name)

            draft_targets.append(
                NodeVerificationTarget(
                    target_id=f"{step_name}_output",
                    node_id=node_id,
                    level="node",
                    description=f"Verify {step_name} produced the expected {logical_step_kind} output.",
                )
            )
            draft_methods.append(
                VerificationMethod(
                    method_id=f"{step_name}_contract_check",
                    method_kind="step_output_contract_check",
                    binding_status="draft",
                    config={"step_name": step_name, "logical_step_kind": logical_step_kind},
                )
            )

            method_binding_status = "draft"
            if binding is not None and binding.selected_capability_id:
                method_binding_status = "bound" if binding.runnable else "partially_bound"
            bound_methods.append(
                VerificationMethod(
                    method_id=f"{step_name}_contract_check",
                    method_kind="step_output_contract_check",
                    binding_status=method_binding_status,
                    config={
                        "step_name": step_name,
                        "logical_step_kind": logical_step_kind,
                        "selected_capability_id": binding.selected_capability_id if binding is not None else None,
                        "binding_status": binding.binding_status if binding is not None else "unbound",
                    },
                )
            )

            for evidence_type in step.get("produces_evidence_types") or []:
                evidence_value = str(evidence_type).strip()
                if evidence_value and evidence_value not in seen_evidence:
                    seen_evidence.add(evidence_value)
                    required_evidence.append(evidence_value)

            if binding is not None and (binding.requires_confirmation or binding.manual_review_required):
                approval_requirements.append(
                    ApprovalRequirement(
                        requirement_id=f"{step_name}_approval",
                        gate_node_id=node_id,
                        description=f"Operator approval required before accepting results from {step_name}.",
                    )
                )

        draft_targets.append(
            NodeVerificationTarget(
                target_id="workflow_completion",
                level="workflow",
                description="Verify the projected workflow completes with no blocked required nodes.",
            )
        )
        acceptance_rules.append(
            AcceptanceRule(
                rule_id="all_required_nodes_verified",
                description="All required workflow-native sidecar nodes must either bind successfully or remain explicitly review-gated.",
                severity="hard",
            )
        )

        default_outputs = list((artifacts_spec_json or {}).get("default_outputs", []))
        for output_name in default_outputs:
            draft_targets.append(
                NodeVerificationTarget(
                    target_id=f"artifact_{output_name}",
                    level="artifact",
                    description=f"Verify expected artifact output {output_name} is emitted when requested by the workflow.",
                )
            )

        draft_plan = VerificationPlan(
            verification_targets=draft_targets,
            verification_methods=draft_methods,
            required_evidence=list(required_evidence),
            postconditions=["Workflow-native projection remains aligned with generated execution package steps."],
            acceptance_rules=list(acceptance_rules),
            approval_requirements=[],
            failure_on_unverified=False,
            recheck_policy="recheck_on_rebuild",
            verification_binding_status="draft",
        )

        bound_status = "bound"
        if any(item.binding_status == "partially_bound" for item in bound_methods):
            bound_status = "partially_bound"
        if any(item.binding_status == "draft" for item in bound_methods):
            bound_status = "partially_bound"

        bound_plan = VerificationPlan(
            verification_targets=draft_targets,
            verification_methods=bound_methods,
            required_evidence=list(required_evidence),
            postconditions=["Bound workflow-native projection remains aligned with generated execution package bindings."],
            acceptance_rules=list(acceptance_rules),
            approval_requirements=approval_requirements,
            failure_on_unverified=False,
            recheck_policy="recheck_on_rerun",
            verification_binding_status=bound_status,
        )

        return {
            "workflow_native/verification_plan.draft.json": draft_plan.model_dump(mode="python"),
            "workflow_native/verification_plan.bound.json": bound_plan.model_dump(mode="python"),
        }

    def _build_workflow_native_execution_contract_sidecar(
        self,
        *,
        package_bindings: list[ExecutionPackageBinding],
        runtime_entry: ExecutionPackageRuntimeEntry,
        runtime_constraints_json: dict[str, Any],
        artifacts_spec_json: dict[str, Any],
    ) -> dict[str, Any]:
        admissibility = self._load_compile_context_execution_admissibility(runtime_constraints_json)

        required_gate_node_ids = [
            f"node_{index}"
            for index, binding in enumerate(package_bindings, start=1)
            if binding.requires_confirmation or binding.manual_review_required
        ]
        execution_contract = WorkflowExecutionContract(
            global_retry_policy=RetryPolicy(
                max_attempts=1,
                backoff_strategy="none",
                retryable_failure_classes=["transient_runtime_error"],
            ),
            global_timeout_policy=TimeoutPolicy(
                default_timeout_seconds=max((binding.timeout_seconds or 0) for binding in package_bindings) or None,
                hard_kill_timeout_seconds=None,
            ),
            intervention_legality=InterventionPolicy(
                allowed_modes=["inspect", "approve", "reject", "rerun"],
                required_before_execute=False,
                required_before_commit=bool(required_gate_node_ids),
                editable_fields=[],
                skip_policy="forbidden",
                resume_policy="resume_from_current",
            ),
            evidence_retention_policy=EvidenceRetentionPolicy(
                retain_inputs=True,
                retain_outputs=True,
                retain_traces=True,
                retain_receipts=True,
            ),
            verification_rules=[
                "All workflow-native sidecar nodes must preserve explicit postcondition visibility.",
                "All non-runnable or review-gated nodes must remain observable in build metadata.",
            ],
            fallback_policy=FallbackPolicy(
                allow_runtime_fallback=False,
                allowed_fallback_modes=[],
            ),
            rerun_legality_policy=RerunPolicy(
                rerun_allowed=all(binding.execution_kind != "artifact_writer" for binding in package_bindings if binding.runnable),
                rerun_requires_human_approval=any(binding.requires_confirmation or binding.manual_review_required for binding in package_bindings),
                rerun_invalidates_downstream=True,
                rerun_creates_branch=False,
            ),
            failure_escalation_policy=FailureEscalationPolicy(
                escalate_on_node_failure=True,
                escalate_to_human_gate=bool(required_gate_node_ids),
                stop_graph_on_unverified_side_effect=True,
            ),
            artifact_emission_policy=ArtifactEmissionPolicy(
                emit_intermediate_artifacts=True,
                emit_final_artifacts=True,
                required_artifact_types=self._workflow_native_artifact_types(
                    artifacts_spec_json=artifacts_spec_json,
                    admissibility=admissibility,
                ),
            ),
            human_gate_policy=HumanGatePolicy(
                required_gate_node_ids=required_gate_node_ids,
                approval_timeout_seconds=None,
            ),
        )
        payload = execution_contract.model_dump(mode="python")
        payload["runtime_entry_ready"] = runtime_entry.is_runnable
        payload["admissibility_primary_regime"] = admissibility.primary_execution_regime if admissibility is not None else None
        return {
            "workflow_native/execution_contract.json": payload,
        }

    def _build_workflow_native_materialization_record_sidecar(
        self,
        *,
        build_id: str,
        package_bindings: list[ExecutionPackageBinding],
        build_plan_summary: ExecutableBuildPlanSummary,
    ) -> dict[str, Any]:
        decisions_by_step = {item.step_name: item for item in build_plan_summary.proposed_bindings}
        node_binding_decisions: list[NodeBindingDecisionRecord] = []
        inserted_human_checkpoints: list[MaterializationCheckpointInsertion] = []
        unresolved_to_review_downgrades: list[str] = []

        for index, binding in enumerate(package_bindings, start=1):
            decision = decisions_by_step.get(binding.step_name)
            node_id = f"node_{index}"
            node_binding_decisions.append(
                NodeBindingDecisionRecord(
                    node_id=node_id,
                    selected_capability_id=binding.selected_capability_id,
                    considered_capability_ids=list(decision.considered_candidate_ids if decision is not None else []),
                    rejected_capability_ids=list(decision.rejected_candidate_ids if decision is not None else []),
                    final_reason=(
                        decision.selection_reason
                        if decision is not None and decision.selection_reason
                        else (binding.blocked_reason or binding.source_ref or binding.binding_status)
                    ),
                )
            )
            if binding.requires_confirmation or binding.manual_review_required:
                inserted_human_checkpoints.append(
                    MaterializationCheckpointInsertion(
                        checkpoint_id=f"{binding.step_name}_checkpoint",
                        reason=f"Human checkpoint required for {binding.step_name}.",
                        inserted_before_node_id=node_id,
                    )
                )
            if binding.binding_status == "review_needed":
                unresolved_to_review_downgrades.append(binding.step_name)

        record = BuildMaterializationRecord(
            build_id=build_id,
            workflow_artifact_id=None,
            node_binding_decisions=node_binding_decisions,
            inserted_human_checkpoints=inserted_human_checkpoints,
            unresolved_to_review_downgrades=unresolved_to_review_downgrades,
            projection_repairs=[
                ProjectionRepairRecord(
                    record_id="workflow_native_sidecar_projection",
                    projection_ref="workflow_native/build_materialization_record.json",
                    repair_action="sequential_projection_only",
                    reason="Workflow-native sidecar remains a non-authoritative projection of the current build package.",
                )
            ],
            policy_driven_choices=list(build_plan_summary.admissibility_warnings),
            build_warnings=list(build_plan_summary.warnings),
            unresolved_assumptions=list(build_plan_summary.unresolved_points),
        )
        return {
            "workflow_native/build_materialization_record.json": record.model_dump(mode="python"),
        }

    def _build_workflow_native_artifact_sidecar(
        self,
        *,
        build_id: str,
        package_metadata: ExecutionPackageMetadata,
        workflow_native_projection: dict[str, Any],
        workflow_native_verification: dict[str, Any],
        workflow_native_execution_contract: dict[str, Any],
        workflow_native_materialization_record: dict[str, Any],
    ) -> dict[str, Any]:
        graph = WorkflowGraph.model_validate(workflow_native_projection["workflow_native/workflow_graph.json"])
        verification_plan = VerificationPlan.model_validate(
            workflow_native_verification["workflow_native/verification_plan.bound.json"]
        )
        execution_contract = WorkflowExecutionContract.model_validate(
            workflow_native_execution_contract["workflow_native/execution_contract.json"]
        )
        materialization_record = BuildMaterializationRecord.model_validate(
            workflow_native_materialization_record["workflow_native/build_materialization_record.json"]
        )
        artifact = WorkflowArtifact(
            artifact_id=f"workflow-artifact-{build_id}",
            workflow_graph=graph,
            execution_contract=execution_contract,
            verification_plan=verification_plan,
            build_materialization_record=materialization_record,
            package_metadata=package_metadata.model_dump(mode="python"),
        )
        return {
            "workflow_native/workflow_artifact.json": artifact.model_dump(mode="python"),
        }

    def _workflow_native_artifact_types(
        self,
        *,
        artifacts_spec_json: dict[str, Any],
        admissibility: ExecutionAdmissibilityContract | None,
    ) -> list[str]:
        resolved: list[str] = []
        seen: set[str] = set()

        def add(value: str | None) -> None:
            if value and value not in seen:
                seen.add(value)
                resolved.append(value)

        if admissibility is not None:
            expected = str(admissibility.expected_deliverable_class or "").strip()
            if expected in {
                "workflow_spec",
                "execution_plan",
                "report",
                "spreadsheet",
                "document",
                "run_log_bundle",
                "evidence_bundle",
                "action_patch",
                "form_submission_record",
                "artifact_bundle",
            }:
                add(expected)

        for item in (artifacts_spec_json or {}).get("default_outputs", []):
            if not isinstance(item, str):
                continue
            lowered = item.strip().lower()
            if lowered == "run_summary" or lowered.endswith(".log"):
                add("run_log_bundle")
            elif lowered.endswith((".md", ".txt")):
                add("document")
            elif lowered.endswith((".csv", ".xlsx")):
                add("spreadsheet")
            elif lowered.endswith(".json"):
                add("evidence_bundle")
            elif "workflow" in lowered:
                add("workflow_spec")
            elif "plan" in lowered:
                add("execution_plan")
            elif "patch" in lowered or lowered.endswith(".diff"):
                add("action_patch")
            elif "report" in lowered or "brief" in lowered or "digest" in lowered:
                add("report")
            elif "bundle" in lowered or lowered.endswith(".zip"):
                add("artifact_bundle")

        return resolved

    def _workflow_native_node_semantic_type(
        self,
        logical_step_kind: str,
        binding: ExecutionPackageBinding | None,
    ) -> str:
        if logical_step_kind == "ingest":
            return "acquire"
        if logical_step_kind in {"transform", "summarize", "rank"}:
            return "transform"
        if logical_step_kind == "validate":
            return "verify"
        if logical_step_kind == "review":
            return "human_gate"
        if logical_step_kind in {"persist", "notify"}:
            return "act"
        if binding is not None and binding.binding_status == "review_needed":
            return "human_gate"
        return "transform"

    def _workflow_native_input_port_specs(self, step: dict[str, Any]) -> list[NodePortSpec]:
        consumes = [str(item) for item in step.get("consumes_evidence_types", []) if str(item).strip()]
        if consumes:
            return [
                NodePortSpec(
                    port_name=evidence_type,
                    schema_summary=f"Consumes evidence type {evidence_type}.",
                    semantic_role="evidence_input",
                    required=True,
                )
                for evidence_type in consumes
            ]
        return [
            NodePortSpec(
                port_name="primary_input",
                schema_summary=str(step.get("input_intent") or "Primary workflow input."),
                semantic_role="workflow_input",
                required=True,
            )
        ]

    def _workflow_native_output_port_specs(
        self,
        step: dict[str, Any],
        binding: ExecutionPackageBinding | None,
    ) -> list[NodePortSpec]:
        produces = [str(item) for item in step.get("produces_evidence_types", []) if str(item).strip()]
        ports = [
            NodePortSpec(
                port_name=evidence_type,
                schema_summary=f"Produces evidence type {evidence_type}.",
                semantic_role="evidence_output",
                required=False,
            )
            for evidence_type in produces
        ]
        if not ports:
            ports.append(
                NodePortSpec(
                    port_name="primary_output",
                    schema_summary=(
                        binding.config_contract_summary
                        if binding is not None and binding.config_contract_summary
                        else str(step.get("output_intent") or "Primary workflow output.")
                    ),
                    semantic_role="workflow_output",
                    required=True,
                )
            )
        return ports

    def _runtime_binding_for_step(
        self,
        *,
        step: dict[str, Any],
        binding,
        active_lifecycle_entries: dict[str, Any],
        materialization_service: CapabilityMaterializationService,
    ) -> tuple[ExecutionPackageBinding, list[ExecutionPackageAssetReference]]:  # noqa: ANN001
        capability_id = binding.selected_capability_id
        assets: list[ExecutionPackageAssetReference] = []
        step_name = str(step.get("step_name") or "unknown_step")
        logical_step_kind = str(step.get("logical_step_kind") or binding.logical_step_kind or "").strip() or None
        raw_args = dict(step.get("args") if isinstance(step.get("args"), dict) else {})
        config_hints = {**raw_args, **dict(binding.config_hints or {})}
        manual_review_required = bool(binding.manual_review_required)
        review_notes = list(binding.ambiguities or [])
        base_kwargs = {
            "step_name": step_name,
            "logical_step_kind": logical_step_kind,
            "selected_capability_id": capability_id,
            "source_kind": "static_registry" if capability_id else "unbound",
            "source_ref": capability_id,
            "runtime_requirements": list(binding.runtime_requirements),
            "provider_requirements": list(binding.provider_requirements),
            "config_hints": config_hints,
            "manual_review_required": manual_review_required,
            "template_execution_kind": binding.template_execution_kind,
            "review_notes": review_notes,
        }
        if capability_id == "parse_json":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="parse_json_payload",
                    config_contract_summary="Reads JSON input from run_input.raw_json or configured source_field.",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "repository_source_resolve":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="repository_source_resolve",
                    config_contract_summary="Classifies and normalizes a repository locator before checkout or direct repository inventory.",
                    policy_scope="workspace_or_http",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "repository_checkout":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="repository_checkout",
                    config_contract_summary="Materializes a supported repository source into a bounded workspace root before repository inventory.",
                    requires_confirmation=True,
                    network_access_required=bool(config_hints.get("allow_network")),
                    policy_scope="workspace_or_http",
                    review_status="recommended",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "repository_inventory":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="repository_inventory",
                    config_contract_summary="Scans a bounded repository root and produces structured inventory and coverage metadata.",
                    policy_scope="workspace",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "deduplicate_items":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="deduplicate_items",
                    config_contract_summary="Uses deterministic key-based deduplication over candidate records.",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "source_structure_extract":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="source_structure_extract",
                    config_contract_summary="Extracts bounded module, import, symbol, and framework structure from repository files.",
                    policy_scope="workspace",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "repository_relationship_map":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="repository_relationship_map",
                    config_contract_summary="Builds bounded dependency and boundary hints across extracted repository modules.",
                    policy_scope="workspace",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "repository_execution_surface_inspect":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="repository_execution_surface_inspect",
                    config_contract_summary="Inspects bounded build/runtime/task/config surfaces across repository files and manifests.",
                    policy_scope="workspace",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "rank_candidates":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="rank_candidates",
                    config_contract_summary="Ranks candidates using deterministic numeric score fields.",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "repository_evidence_bundle":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="repository_evidence_bundle",
                    config_contract_summary="Packages repository analysis outputs into a reusable bounded evidence bundle.",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "architecture_report_brief":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="architecture_report_brief",
                    config_contract_summary="Builds a grounded architecture-report brief from a bounded repository evidence bundle.",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "architecture_report_synthesis_llm":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="llm_task",
                    runtime_adapter="provider_runtime",
                    handler_ref="architecture_report_synthesis_llm",
                    config_contract_summary="Uses a bounded LLM synthesis step to draft an architecture report from a structured report brief.",
                    review_status="recommended",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "architecture_report_synthesis":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="architecture_report_synthesis",
                    config_contract_summary="Synthesizes an architecture-oriented markdown report from a bounded repository evidence bundle.",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "architecture_report_render":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="architecture_report_render",
                    config_contract_summary="Renders a structured architecture-report draft into markdown with coverage notes and citations.",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "architecture_report_quality_gate":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="architecture_report_quality_gate",
                    config_contract_summary="Applies deterministic quality checks to a rendered architecture report before delivery.",
                    review_status="recommended",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "validate_schema":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="validate_schema_contract",
                    config_contract_summary="Validates required keys and simple type expectations.",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "write_artifact":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="artifact_writer",
                    runtime_adapter="artifact_writer",
                    handler_ref="write_artifact",
                    config_contract_summary="Writes the previous structured payload or configured source step to artifact files.",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "research_briefing_render":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="python_function",
                    runtime_adapter="builtin_python",
                    handler_ref="research_briefing_render",
                    config_contract_summary="Renders a structured research briefing draft into markdown before artifact delivery.",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "web_search":
            source_mode = "network_documents" if config_hints.get("allow_network") and config_hints.get("source_urls") else "bounded_corpus"
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="web_search",
                    runtime_adapter="bounded_web_search",
                    handler_ref="web_search",
                    action_kind="search",
                    config_contract_summary="Runs a bounded search over local corpora, supplied documents, or explicitly allowed fetched sources.",
                    network_access_required=bool(config_hints.get("allow_network")),
                    allowed_domains=[str(item) for item in config_hints.get("allowed_domains", []) if str(item).strip()],
                    source_mode=source_mode,
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "filesystem_list":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="filesystem",
                    runtime_adapter="bounded_filesystem",
                    handler_ref="filesystem_list",
                    config_contract_summary="Lists files or directories inside the allowed workspace scope.",
                    policy_scope="workspace",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "filesystem_read":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="filesystem",
                    runtime_adapter="bounded_filesystem",
                    handler_ref="filesystem_read",
                    config_contract_summary="Reads text content from a file inside the allowed workspace scope.",
                    policy_scope="workspace",
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "filesystem_write":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="filesystem",
                    runtime_adapter="bounded_filesystem",
                    handler_ref="filesystem_write",
                    config_contract_summary="Writes content into a workspace-scoped file when explicit write allowance is present.",
                    requires_confirmation=True,
                    policy_scope="workspace",
                    review_status="recommended",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "terminal_command":
            timeout_seconds = max(1, min(int(config_hints.get("timeout_seconds") or 5), 15))
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="terminal_command",
                    runtime_adapter="bounded_terminal",
                    handler_ref="terminal_command",
                    config_contract_summary="Runs an allowlisted command without shell expansion inside the allowed workspace scope.",
                    policy_scope="workspace",
                    timeout_seconds=timeout_seconds,
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "browser_interaction":
            browser_action = str(config_hints.get("action") or config_hints.get("browser_action") or "extract_text").strip().lower()
            if browser_action not in {"open_page", "extract_text", "inspect_links", "click", "fill", "select", "submit", "screenshot", "wait_for_target", "save_text", "download_file"}:
                return (
                    ExecutionPackageBinding(
                        **base_kwargs,
                        execution_kind="blocked",
                        runtime_adapter="bounded_browser",
                        handler_ref=browser_action or "browser_interaction",
                        action_kind=browser_action or None,
                        config_contract_summary="Only the bounded browser action set is materially runnable in the current package runtime.",
                        review_status="required",
                        binding_status="review_needed",
                        runnable=False,
                        blocked_reason="unsupported_browser_action",
                    ),
                    assets,
                )
            timeout_seconds = max(1, min(int(config_hints.get("timeout_seconds") or 5), 15))
            requires_confirmation = browser_action in {"click", "fill", "select", "submit", "save_text", "download_file"}
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="browser_interaction",
                    runtime_adapter="bounded_browser",
                    handler_ref=browser_action,
                    action_kind=browser_action,
                    config_contract_summary="Loads a bounded local or remote page and performs a single auditable browser action without autonomous interaction loops.",
                    requires_confirmation=requires_confirmation,
                    network_access_required=bool(config_hints.get("allow_network")),
                    allowed_domains=[str(item) for item in config_hints.get("allowed_domains", []) if str(item).strip()],
                    source_mode="interactive_browser",
                    policy_scope="workspace_or_http",
                    timeout_seconds=timeout_seconds,
                    review_status="recommended" if requires_confirmation else "none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "fetch_url":
            timeout_seconds = max(1, min(int(config_hints.get("timeout_seconds") or 5), 15))
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="network_fetch",
                    runtime_adapter="bounded_network_fetch",
                    handler_ref="fetch_url",
                    action_kind=str(config_hints.get("action") or "fetch_text").strip().lower() or "fetch_text",
                    config_contract_summary="Fetches bounded local or remote text/page content with explicit network permission and optional domain constraints.",
                    network_access_required=True,
                    allowed_domains=[str(item) for item in config_hints.get("allowed_domains", []) if str(item).strip()],
                    source_mode="remote_fetch",
                    policy_scope="workspace_or_http",
                    timeout_seconds=timeout_seconds,
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "coding_agent":
            # Generalist escape hatch: author + run a bounded script in a
            # sandboxed workspace. Config_hints carry the planner's
            # enricher metadata (args / system_prompt / task_prompt);
            # the resolver folds these into the runtime task_contract.
            timeout_seconds = max(1, min(int(config_hints.get("timeout_seconds") or 120), 600))
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="sandboxed_script",
                    runtime_adapter="bounded_code_runner",
                    handler_ref="coding_agent",
                    config_contract_summary=(
                        "Authors and runs a sandboxed python/bash script against "
                        "the declared workspace with bounded timeout, memory, and "
                        "network — the planner's generalist escape hatch."
                    ),
                    requires_confirmation=manual_review_required,
                    network_access_required=bool(config_hints.get("allow_network")),
                    policy_scope="workspace",
                    timeout_seconds=timeout_seconds,
                    review_status="required" if manual_review_required else "recommended",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "summarize_text_llm":
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="llm_task",
                    runtime_adapter="provider_runtime",
                    handler_ref="summarize_text_llm",
                    config_contract_summary="Uses the configured model binding to summarize provided text or prior step outputs.",
                    review_status="recommended" if binding.manual_review_required else "none",
                    binding_status="runnable",
                    runnable=True,
                ),
                assets,
            )
        if capability_id == "run_python_template":
            asset_path = f"assets/template_stubs/{step_name}.json"
            assets.append(
                ExecutionPackageAssetReference(
                    path=asset_path,
                    kind="template_stub",
                    step_name=step_name,
                    capability_id=capability_id,
                    description=f"Template-backed execution stub for {binding.template_execution_kind or 'reviewed_template'}.",
                    review_required=True,
                )
            )
            return (
                ExecutionPackageBinding(
                    **base_kwargs,
                    execution_kind="blocked",
                    runtime_adapter="template_runtime",
                    handler_ref=binding.template_execution_kind or "run_python_template",
                    config_contract_summary="Template-backed capability remains review-only until a bounded template runtime is activated.",
                    review_status="required",
                    binding_status="review_needed",
                    runnable=False,
                    blocked_reason="template_execution_requires_review",
                    asset_paths=[asset_path],
                ),
                assets,
            )
        lifecycle_entry = active_lifecycle_entries.get(capability_id)
        if lifecycle_entry is not None and lifecycle_entry.request_kind_origin in {"generated_internal", "template_backed_internal"}:
            materialization = materialization_service.get_runtime_materialization(lifecycle_entry.project_id, capability_id)
            if materialization is None:
                blocked_reason = (
                    "active_template_capability_not_materialized"
                    if lifecycle_entry.request_kind_origin == "template_backed_internal"
                    else "active_internal_capability_not_materialized"
                )
                lifecycle_binding_kwargs = {
                    **base_kwargs,
                    "source_kind": "lifecycle_entry",
                    "source_ref": lifecycle_entry.id,
                }
                return (
                    ExecutionPackageBinding(
                        **lifecycle_binding_kwargs,
                        execution_kind="blocked",
                        runtime_adapter="unsupported",
                        handler_ref=capability_id,
                        config_contract_summary="Capability is active in governance, but no reviewed runtime materialization exists yet.",
                        review_status="required",
                        binding_status="blocked",
                        runnable=False,
                        blocked_reason=blocked_reason,
                    ),
                    assets,
                )
            materialized_asset_paths: list[str] = []
            for asset in materialization.materialized_asset_refs:
                assets.append(
                    ExecutionPackageAssetReference(
                        path=asset.path,
                        kind=asset.kind,
                        step_name=step_name,
                        capability_id=capability_id,
                        description=asset.description,
                        review_required=asset.review_required,
                    )
                )
                materialized_asset_paths.append(asset.path)
            materialized_binding_kwargs = {
                **base_kwargs,
                "source_kind": "materialization",
                "source_ref": materialization.source_lifecycle_entry_id,
            }
            return (
                ExecutionPackageBinding(
                    **materialized_binding_kwargs,
                    materialization_id=materialization.materialization_id,
                    materialization_status=materialization.materialization_status,
                    execution_kind=materialization.resulting_execution_kind,
                    runtime_adapter=materialization.resulting_runtime_adapter,
                    handler_ref=materialization.resulting_handler_ref,
                    config_contract_summary=(
                        "Capability is runtime-usable through a reviewed internal materialization."
                        if lifecycle_entry.request_kind_origin == "generated_internal"
                        else "Capability is runtime-usable through a reviewed bounded template materialization."
                    ),
                    policy_scope=str(materialization.runtime_constraints.get("policy_scope") or "") or None,
                    review_status="none",
                    binding_status="runnable",
                    runnable=True,
                    asset_paths=materialized_asset_paths,
                ),
                assets,
            )
        blocked_reason = "no_materialized_runtime_handler"
        if capability_id == "send_email":
            blocked_reason = "notification_runtime_handler_not_materialized"
        return (
            ExecutionPackageBinding(
                **base_kwargs,
                execution_kind="blocked",
                runtime_adapter="unsupported",
                handler_ref=capability_id,
                config_contract_summary="Capability is registered for planning/build, but no bounded runtime handler has been materialized yet.",
                review_status="required" if manual_review_required else "none",
                binding_status="blocked",
                runnable=False,
                blocked_reason=blocked_reason,
            ),
            assets,
        )

    def _legacy_runtime_binding_for_step(self, step: dict[str, Any]) -> ExecutionPackageBinding | None:
        step_name = str(step.get("step_name") or "unknown_step")
        step_type = str(step.get("step_type") or "llm_task").strip().lower()
        if step_type == "python_function":
            return ExecutionPackageBinding(
                step_name=step_name,
                logical_step_kind=step.get("logical_step_kind"),
                selected_capability_id=step.get("selected_capability_id"),
                source_kind="unbound",
                execution_kind="python_function",
                runtime_adapter="builtin_python",
                handler_ref=str(step.get("function_name") or step_name),
                config_contract_summary="Legacy executable step carried its own python_function definition.",
                config_hints=dict(step.get("args") if isinstance(step.get("args"), dict) else {}),
                binding_status="runnable",
                runnable=True,
            )
        if step_type == "web_search":
            args = dict(step.get("args") if isinstance(step.get("args"), dict) else {})
            return ExecutionPackageBinding(
                step_name=step_name,
                logical_step_kind=step.get("logical_step_kind"),
                selected_capability_id=step.get("selected_capability_id") or "web_search",
                source_kind="unbound",
                execution_kind="web_search",
                runtime_adapter="bounded_web_search",
                handler_ref="web_search",
                config_contract_summary="Legacy executable step carried its own bounded web search definition.",
                action_kind="search",
                config_hints=args,
                network_access_required=bool(args.get("allow_network")),
                allowed_domains=[str(item) for item in args.get("allowed_domains", []) if str(item).strip()],
                source_mode="network_documents" if args.get("allow_network") and args.get("source_urls") else "bounded_corpus",
                binding_status="runnable",
                runnable=True,
            )
        if step_type == "filesystem":
            args = dict(step.get("args") if isinstance(step.get("args"), dict) else {})
            action = str(args.get("action") or "list").strip().lower()
            return ExecutionPackageBinding(
                step_name=step_name,
                logical_step_kind=step.get("logical_step_kind"),
                selected_capability_id=step.get("selected_capability_id"),
                source_kind="unbound",
                execution_kind="filesystem",
                runtime_adapter="bounded_filesystem",
                handler_ref=f"filesystem_{action}",
                config_contract_summary="Legacy executable step carried its own bounded filesystem definition.",
                config_hints=args,
                requires_confirmation=action == "write",
                policy_scope="workspace",
                binding_status="runnable",
                runnable=True,
            )
        if step_type == "terminal_command":
            args = dict(step.get("args") if isinstance(step.get("args"), dict) else {})
            timeout_seconds = max(1, min(int(args.get("timeout_seconds") or 5), 15))
            return ExecutionPackageBinding(
                step_name=step_name,
                logical_step_kind=step.get("logical_step_kind"),
                selected_capability_id=step.get("selected_capability_id") or "terminal_command",
                source_kind="unbound",
                execution_kind="terminal_command",
                runtime_adapter="bounded_terminal",
                handler_ref="terminal_command",
                config_contract_summary="Legacy executable step carried its own bounded terminal definition.",
                config_hints=args,
                policy_scope="workspace",
                timeout_seconds=timeout_seconds,
                binding_status="runnable",
                runnable=True,
            )
        if step_type == "browser_interaction":
            args = dict(step.get("args") if isinstance(step.get("args"), dict) else {})
            action = str(args.get("action") or "extract_text").strip().lower()
            if action not in {"open_page", "extract_text", "inspect_links", "click", "fill", "select", "submit", "screenshot", "wait_for_target", "save_text", "download_file"}:
                return ExecutionPackageBinding(
                    step_name=step_name,
                    logical_step_kind=step.get("logical_step_kind"),
                    selected_capability_id=step.get("selected_capability_id") or "browser_interaction",
                    source_kind="unbound",
                    execution_kind="blocked",
                    runtime_adapter="bounded_browser",
                    handler_ref=action,
                    action_kind=action or None,
                    config_contract_summary="Legacy browser step requested an action outside the bounded browser runtime.",
                    config_hints=args,
                    binding_status="review_needed",
                    review_status="required",
                    runnable=False,
                    blocked_reason="unsupported_browser_action",
                )
            timeout_seconds = max(1, min(int(args.get("timeout_seconds") or 5), 15))
            return ExecutionPackageBinding(
                step_name=step_name,
                logical_step_kind=step.get("logical_step_kind"),
                selected_capability_id=step.get("selected_capability_id") or "browser_interaction",
                source_kind="unbound",
                execution_kind="browser_interaction",
                runtime_adapter="bounded_browser",
                handler_ref=action,
                action_kind=action,
                config_contract_summary="Legacy executable step carried its own bounded browser definition.",
                config_hints=args,
                requires_confirmation=action in {"click", "fill", "select", "submit", "save_text", "download_file"},
                network_access_required=bool(args.get("allow_network")),
                allowed_domains=[str(item) for item in args.get("allowed_domains", []) if str(item).strip()],
                source_mode="interactive_browser",
                policy_scope="workspace_or_http",
                timeout_seconds=timeout_seconds,
                binding_status="runnable",
                runnable=True,
            )
        if step_type == "network_fetch":
            args = dict(step.get("args") if isinstance(step.get("args"), dict) else {})
            timeout_seconds = max(1, min(int(args.get("timeout_seconds") or 5), 15))
            return ExecutionPackageBinding(
                step_name=step_name,
                logical_step_kind=step.get("logical_step_kind"),
                selected_capability_id=step.get("selected_capability_id") or "fetch_url",
                source_kind="unbound",
                execution_kind="network_fetch",
                runtime_adapter="bounded_network_fetch",
                handler_ref="fetch_url",
                action_kind=str(args.get("action") or "fetch_text").strip().lower() or "fetch_text",
                config_contract_summary="Legacy executable step carried its own bounded network fetch definition.",
                config_hints=args,
                network_access_required=True,
                allowed_domains=[str(item) for item in args.get("allowed_domains", []) if str(item).strip()],
                source_mode="remote_fetch",
                policy_scope="workspace_or_http",
                timeout_seconds=timeout_seconds,
                binding_status="runnable",
                runnable=True,
            )
        if step_type == "artifact_writer":
            return ExecutionPackageBinding(
                step_name=step_name,
                logical_step_kind=step.get("logical_step_kind"),
                selected_capability_id=step.get("selected_capability_id"),
                source_kind="unbound",
                execution_kind="artifact_writer",
                runtime_adapter="artifact_writer",
                handler_ref=str(step.get("writer_kind") or step_name),
                config_contract_summary="Legacy executable step carried its own artifact writer definition.",
                config_hints=dict(step.get("args") if isinstance(step.get("args"), dict) else {}),
                binding_status="runnable",
                runnable=True,
            )
        if step_type == "blocked":
            return ExecutionPackageBinding(
                step_name=step_name,
                logical_step_kind=step.get("logical_step_kind"),
                source_kind="unbound",
                execution_kind="blocked",
                runtime_adapter="unsupported",
                handler_ref=step_name,
                config_contract_summary="Legacy step was explicitly marked blocked.",
                binding_status="blocked",
                runnable=False,
                blocked_reason=str(step.get("blocked_reason") or "legacy_step_blocked"),
            )
        return ExecutionPackageBinding(
            step_name=step_name,
            logical_step_kind=step.get("logical_step_kind"),
            selected_capability_id=step.get("selected_capability_id"),
            source_kind="unbound",
            execution_kind="llm_task",
            runtime_adapter="provider_runtime",
            handler_ref=str(step.get("task_kind") or step_name),
            config_contract_summary="Legacy executable step uses the existing llm_task runtime handler.",
            binding_status="runnable",
            runnable=True,
            config_hints={
                key: value
                for key, value in {
                    "task_prompt": step.get("task_prompt"),
                    "instructions": step.get("instructions"),
                    "provider": step.get("provider"),
                    "model_name": step.get("model_name"),
                }.items()
                if value is not None
            },
        )

    def _build_runtime_entry(self, package_bindings: list[ExecutionPackageBinding]) -> ExecutionPackageRuntimeEntry:
        runnable_step_names = [item.step_name for item in package_bindings if item.runnable]
        blocked_step_names = [item.step_name for item in package_bindings if item.binding_status == "blocked"]
        review_required_step_names = [
            item.step_name
            for item in package_bindings
            if item.binding_status == "review_needed" or item.manual_review_required
        ]
        supported_execution_kinds = sorted(
            {item.execution_kind for item in package_bindings if item.execution_kind is not None and item.execution_kind != "blocked"}
        )
        notes: list[str] = []
        if blocked_step_names:
            notes.append("Some steps remain blocked and cannot be executed through the bounded package runtime.")
        if review_required_step_names:
            notes.append("Some steps still require explicit review before they can become runnable.")
        return ExecutionPackageRuntimeEntry(
            supported_execution_kinds=supported_execution_kinds,
            runnable_step_names=runnable_step_names,
            blocked_step_names=blocked_step_names,
            review_required_step_names=review_required_step_names,
            active_binding_step_names=[item.step_name for item in package_bindings if item.binding_status != "unbound"],
            is_runnable=len(runnable_step_names) > 0 and len(blocked_step_names) == 0 and len(review_required_step_names) == 0,
            notes=notes,
        )

    def _build_package_metadata(
        self,
        *,
        blueprint_type: str,
        package_bindings: list[ExecutionPackageBinding],
        runtime_entry: ExecutionPackageRuntimeEntry,
        runtime_constraints_json: dict[str, Any],
    ) -> ExecutionPackageMetadata:
        bound_capability_ids = [
            item.selected_capability_id
            for item in package_bindings
            if item.selected_capability_id
        ]
        runnable_capability_ids = [
            item.selected_capability_id
            for item in package_bindings
            if item.selected_capability_id and item.runnable
        ]
        notes = list(runtime_entry.notes)
        if not runnable_capability_ids:
            notes.append("No materially runnable capability bindings were produced for this package.")
        admissibility = self._load_compile_context_execution_admissibility(runtime_constraints_json)
        if runtime_constraints_json.get("compile_context") is not None and admissibility is None:
            notes.append("Compile context was present but execution admissibility could not be derived from it.")
        registry = CapabilityRegistryService()
        selected_projections = [
            registry.build_planner_capability_projection(capability_id)
            for capability_id in dict.fromkeys(bound_capability_ids)
            if registry.get_capability(capability_id) is not None
        ]
        return ExecutionPackageMetadata(
            executable_type=blueprint_type,
            total_steps=len(package_bindings),
            bound_step_count=len([item for item in package_bindings if item.selected_capability_id]),
            runnable_step_count=len(runtime_entry.runnable_step_names),
            blocked_step_count=len(runtime_entry.blocked_step_names),
            review_required_step_count=len(runtime_entry.review_required_step_names),
            bound_capability_ids=bound_capability_ids,
            runnable_capability_ids=runnable_capability_ids,
            runtime_entry_ready=runtime_entry.is_runnable,
            runtime_input_hints=self._build_runtime_input_hints(package_bindings),
            execution_admissibility=admissibility,
            selected_planner_capability_projections=selected_projections,
            notes=notes,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _load_compile_context_execution_admissibility(
        self,
        runtime_constraints_json: dict[str, Any],
    ) -> ExecutionAdmissibilityContract | None:
        payload = runtime_constraints_json.get("compile_context")
        if not isinstance(payload, dict):
            return None
        try:
            compile_context = CompileContextResponse.model_validate(payload)
        except Exception:
            return None
        return compile_context.execution_admissibility

    def _build_runtime_input_hints(self, package_bindings: list[ExecutionPackageBinding]) -> list[dict[str, Any]]:
        hints: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def add_hint(
            *,
            field_name: str,
            description: str,
            required: bool,
            step_name: str,
            example: Any | None = None,
        ) -> None:
            key = (field_name, step_name)
            if key in seen:
                return
            seen.add(key)
            payload = {
                "field_name": field_name,
                "description": description,
                "required": required,
                "step_name": step_name,
            }
            if example is not None:
                payload["example"] = example
            hints.append(payload)

        for binding in package_bindings:
            config_hints = binding.config_hints or {}
            if binding.execution_kind == "web_search":
                query_field = str(config_hints.get("query_field") or "query")
                if not config_hints.get("query"):
                    add_hint(
                        field_name=query_field,
                        description="Search query text for the bounded web search step.",
                        required=True,
                        step_name=binding.step_name,
                        example="AI",
                    )
                if not config_hints.get("source_urls"):
                    add_hint(
                        field_name="source_urls",
                        description="Optional explicit page URLs to fetch and search within before ranking results.",
                        required=False,
                        step_name=binding.step_name,
                        example=["https://example.com/article"],
                    )
                if config_hints.get("allow_network") is None:
                    add_hint(
                        field_name="allow_network",
                        description="Set true to allow remote fetches for explicit source URLs.",
                        required=False,
                        step_name=binding.step_name,
                        example=True,
                    )
                if not config_hints.get("allowed_domains"):
                    add_hint(
                        field_name="allowed_domains",
                        description="Optional hostname or URL allowlist for remote fetches, e.g. 'example.com' or 'https://example.com'.",
                        required=False,
                        step_name=binding.step_name,
                        example=["example.com"],
                    )
            elif binding.execution_kind == "network_fetch":
                if not config_hints.get("url") and not config_hints.get("path"):
                    add_hint(
                        field_name="url",
                        description="Remote or local URL/path to fetch.",
                        required=True,
                        step_name=binding.step_name,
                        example="https://example.com/page.html",
                    )
            elif binding.execution_kind == "browser_interaction":
                if not config_hints.get("url") and not config_hints.get("path") and not config_hints.get("source_step"):
                    add_hint(
                        field_name="url",
                        description="Initial page URL or local file path for the bounded browser action.",
                        required=True,
                        step_name=binding.step_name,
                        example="https://example.com",
                    )
            elif binding.execution_kind == "terminal_command":
                if not config_hints.get("command") and not config_hints.get("command_argv"):
                    add_hint(
                        field_name="command_argv",
                        description="Allowlisted terminal command argv list to execute.",
                        required=True,
                        step_name=binding.step_name,
                        example=["wc", "-w", "notes.txt"],
                    )
            elif binding.execution_kind == "filesystem":
                action = binding.action_kind or str(config_hints.get("action") or "").strip().lower()
                if action == "write" and "content" not in config_hints and not config_hints.get("source_step"):
                    add_hint(
                        field_name="content",
                        description="Text or structured payload to write into the target workspace file.",
                        required=True,
                        step_name=binding.step_name,
                        example="# Output\n\nGenerated content.",
                    )
            elif binding.handler_ref == "repository_source_resolve":
                if not any(config_hints.get(key) for key in ("repo_locator", "repo_url", "source_locator", "repo_root")):
                    add_hint(
                        field_name="repo_locator",
                        description="Repository locator, such as a local workspace path or remote repository URL.",
                        required=True,
                        step_name=binding.step_name,
                        example="/workspace/repo",
                    )
        return hints

    def _write_generated_file(self, file_path: Path, content: Any) -> None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (dict, list)):
            file_path.write_text(json.dumps(content, indent=2), encoding="utf-8")
        else:
            file_path.write_text(str(content), encoding="utf-8")

    def _stringify_content(self, content: Any) -> str:
        if isinstance(content, (dict, list)):
            return json.dumps(content, indent=2)
        return str(content)

    def _token_usage(self, step_name: str, output_payload: dict[str, Any]) -> dict[str, Any]:
        text = json.dumps(output_payload, ensure_ascii=False)
        completion_tokens = max(20, len(text) // 4)
        prompt_tokens = max(12, len(step_name) * 2)
        return {
            "provider": "builder",
            "model_name": "template",
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost_estimate": 0.0,
        }
