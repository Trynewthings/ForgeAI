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
from app.core.enums import BuildStatus, BuildStepStatus, ExecutableStatus, ExecutableType
from app.core.logging import log_event
from app.db.models import Build, BuildStepV2, Executable
from app.db.models.entities import Artifact, TokenUsageLog
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
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# node_spec integration helpers (used inside _execute_session_anchored_build)
# ---------------------------------------------------------------------------


def _load_node_specs_bundle(state_row):
    """Parse ``state_row.node_specs_json`` into a NodeExecutionSpecBundle.

    Returns None when the column is empty (older sessions that never ran
    /specify) or when the payload fails to validate (defensive — we'd
    rather build without specs than crash on stale rows).
    """
    raw = getattr(state_row, "node_specs_json", None)
    if not raw:
        return None
    from app.schemas.node_execution_spec import NodeExecutionSpecBundle

    try:
        return NodeExecutionSpecBundle.model_validate(raw)
    except Exception:  # noqa: BLE001 — defensive
        logger.warning(
            "build.node_specs.invalid_payload state_id=%s — proceeding without specs",
            getattr(state_row, "id", None),
        )
        return None


def _enforce_spec_gates(*, bundle, planner_output) -> None:
    """Hard-block the build when spec-time validators report issues.

    Two gates:
      1. ``edge_contract_report.issues`` — cross-node spec inconsistency
         (already produced by the spec pass; we just refuse to proceed).
      2. ``capability_mismatch_validator`` — planner-vs-spec disagreement
         on a node's compute class (computed here at build time, since
         it depends on both planner output AND specs).

    Both raise RuntimeError so the build's outer try/except marks the
    build failed with a structured error_message.
    """
    edge_report = bundle.edge_contract_report
    if edge_report and edge_report.issues:
        rendered = "; ".join(
            f"[{i.kind}] {i.from_node_id}->{i.to_node_id}: {i.detail}"
            for i in edge_report.issues
        )
        raise RuntimeError(
            f"session_anchored_build_blocked: spec edge_contract has "
            f"{len(edge_report.issues)} unresolved issue(s). {rendered}"
        )

    from app.services.node_spec.capability_mismatch_validator import (
        validate_capability_mismatch,
    )

    cap_report = validate_capability_mismatch(
        planner_output=planner_output,
        specs=list(bundle.specs),
        skipped_node_ids=list(bundle.skipped_node_ids),
    )
    if cap_report.issues:
        rendered = "; ".join(
            f"[{i.kind}] node={i.node_id} picked={i.selected_capability_id} "
            f"expected_class={i.expected_runtime_class} "
            f"allowed={i.allowed_runtime_classes}"
            for i in cap_report.issues
        )
        raise RuntimeError(
            f"session_anchored_build_blocked: capability_mismatch validator "
            f"reports {len(cap_report.issues)} issue(s). {rendered}"
        )


def _apply_specs_to_planner_output(
    *,
    planner_output,
    specs,
    compile_context=None,
    skipped_node_ids=None,
) -> None:
    """Re-stamp each planner node's metadata from its NodeExecutionSpec.

    Before enrichment, override each coding_agent spec's
    ``primary_output.locator_hint`` with the chain-plumbing locator the
    binding will assign at the next step. Without this, the spec-driven
    task_prompt tells the agent to write to the spec-author's guessed
    locator (often the input directory), but the binding's chained
    intermediate path is what the NEXT step expects to read — they
    conflict and step N+1 can't find its input.

    Mutates ``planner_output.node_drafts`` in place. Nodes without a
    matching spec (auto-inserted gate / verify) are untouched.
    """
    from app.services.build.planner_output_bindings import (
        compute_session_anchored_locator_map,
    )
    from app.services.workflow_planner.runtime_field_enricher import (
        apply_spec_to_node,
    )

    spec_by_node_id = {s.node_id: s for s in specs}
    locator_map: dict = {}
    if compile_context is not None:
        locator_map = compute_session_anchored_locator_map(
            planner_output=planner_output,
            compile_context=compile_context,
            skipped_node_ids=list(skipped_node_ids or []),
        )

    for node in planner_output.node_drafts:
        spec = spec_by_node_id.get(node.draft_node_id)
        if spec is None:
            continue
        # Authoritative locator override for chain plumbing — only when
        # the binding has actually computed one for this node (i.e., it's
        # a coding_agent in the session-anchored chain).
        binding_locator = (locator_map.get(node.draft_node_id) or {}).get(
            "output_locator"
        )
        if binding_locator:
            spec.output_contract.primary_output.locator_hint = binding_locator
        apply_spec_to_node(node=node, spec=spec)


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
        executable_repository = ExecutableRepository(db)
        artifact_repository = ArtifactRepository(db)
        token_usage_log_repository = TokenUsageLogRepository(db)

        try:
            build = build_repository.get_by_id(build_id, with_relations=True)
            if build is None:
                raise RuntimeError("Build not found")
            if not build.session_id:
                raise RuntimeError(
                    "Build has no session_id — session-anchored is the only supported path."
                )
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
        except Exception as exc:
            db.rollback()
            build = build_repository.get_by_id(build_id)
            if build is not None:
                build.status = BuildStatus.FAILED.value
                build.error_message = str(exc)
                build.ended_at = datetime.now(timezone.utc)
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

            # Load node_specs (optional). When present, build:
            #   1) hard-blocks if edge_contract_report has issues,
            #   2) hard-blocks if the planner-vs-spec capability
            #      mismatch validator finds issues,
            #   3) re-enriches each planner node's metadata from its
            #      spec (so coding_agent gets a real task_prompt
            #      instead of a placeholder),
            #   4) passes specs into bindings_from_planner_output so
            #      the verify obligation is derived from
            #      acceptance_criteria.
            # Missing node_specs is tolerated (older sessions that
            # never ran /specify still build).
            node_specs_bundle = _load_node_specs_bundle(state_row)
            spec_list = (
                list(node_specs_bundle.specs) if node_specs_bundle else []
            )
            if node_specs_bundle is not None:
                _enforce_spec_gates(
                    bundle=node_specs_bundle,
                    planner_output=planner_output,
                )
                _apply_specs_to_planner_output(
                    planner_output=planner_output,
                    specs=spec_list,
                    compile_context=compile_context_for_adapter,
                    skipped_node_ids=list(node_specs_bundle.skipped_node_ids),
                )

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
                planner_output,
                compile_context=compile_context_for_adapter,
                node_specs=spec_list or None,
            )
            runnable_binding_names = [
                b.step_name for b in bindings if b.runnable
            ]
            blocked_binding_names = [
                b.step_name for b in bindings if not b.runnable
            ]
            # Fail-fast on critical blocked steps. Verify / human_gate
            # nodes are structural inserts that don't affect execution
            # progress; everything else (transform / acquire / act /
            # summarize / persist) blocked means the slice can't carry
            # this plan, even after the coding_agent fallback. Producing
            # a half-broken executable that the runtime then silently
            # exits is worse than failing here.
            critical_blocked = [
                b.step_name
                for b in bindings
                if not b.runnable
                and (b.logical_step_kind or "") not in {"verify", "human_gate"}
            ]
            log_event(
                logger,
                logging.INFO,
                "build.session_anchored.bindings",
                build_id=build.id,
                runnable=runnable_binding_names,
                blocked=blocked_binding_names,
            )
            if critical_blocked:
                raise RuntimeError(
                    "session_anchored_build_blocked: planner picked "
                    "capabilities the slice cannot execute and cannot "
                    f"fall back to coding_agent. blocked_steps={critical_blocked}"
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
