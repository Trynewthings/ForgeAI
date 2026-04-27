from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from threading import Thread
from typing import Any, Optional

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
from app.services.typed_nodes.planner_pass2.schema import PlannerPass2Output
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


TYPED_NODE_V0_EXECUTION_MODE = "typed-node-v0"
TYPED_NODE_V0_STRATEGY = "typed_node_v0"


def _is_typed_node_plan_payload(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and str(payload.get("plan_version") or "").startswith("typed_planner_pass2")
        and isinstance(payload.get("steps"), list)
    )


def _typed_node_v0_enabled() -> bool:
    return bool(getattr(settings, "typed_node_v0_enabled", False))


def _should_dispatch_typed_node_build(payload: Any) -> bool:
    return _typed_node_v0_enabled() and _is_typed_node_plan_payload(payload)


def _extract_typed_node_pass2_from_node_specs(node_specs_json: Any) -> Optional[PlannerPass2Output]:
    """Recover PlannerPass2Output from a /specify response's typed-node
    sidecar.

    The typed-node config_synthesis adapter writes node_specs_json as
    ``{"schema_kind": "typed_node_v0_config_synthesis", ..., "typed_node_v0":
    {"pass2_output": {...}}}``. Build dispatch reads it from there so
    /plan and /specify can stay decoupled (Pass1 on planner_output_json,
    Pass2 on node_specs_json).
    """
    if not isinstance(node_specs_json, dict):
        return None
    sidecar = node_specs_json.get("typed_node_v0")
    if not isinstance(sidecar, dict):
        return None
    payload = sidecar.get("pass2_output")
    if not _is_typed_node_plan_payload(payload):
        return None
    try:
        return PlannerPass2Output.model_validate(payload)
    except Exception:  # noqa: BLE001 — defensive, drop to legacy fallback
        return None


def _typed_node_steps_document(plan: PlannerPass2Output) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for step in plan.steps:
        steps.append({
            "step_name": step.step_id,
            "step_id": step.step_id,
            "step_type": "typed_node",
            "node_type": step.node_type,
            "intent": step.intent,
            "upstream_step_ids": list(step.upstream_step_ids),
            "consumes_artifact_ids": list(step.consumes_artifact_ids),
            "produces_artifact_ids": list(step.produces_artifact_ids),
            "typed_config": step.typed_config.model_dump(mode="python"),
            "synthesis_notes": list(step.synthesis_notes),
            "escape_hatch_reason": step.escape_hatch_reason,
        })
    return {
        "plan_version": plan.plan_version,
        "draft_status": plan.draft_status,
        "steps": steps,
        "entry_step_ids": list(plan.entry_step_ids),
        "exit_step_ids": list(plan.exit_step_ids),
        "schema_flow": [s.model_dump(mode="python") for s in plan.schema_flow],
        "unresolved_items": [i.model_dump(mode="python") for i in plan.unresolved_items],
        "repaired_step_ids": list(plan.repaired_step_ids),
        "planning_notes": list(plan.planning_notes),
        "planner_trace": dict(plan.planner_trace),
    }


def build_typed_node_execution_package(
    *,
    plan: PlannerPass2Output,
    build_id: str | None = None,
    executable_id: str | None = None,
    project_id: str | None = None,
    build_version: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (manifest, generated_files) for a PlannerPass2Output."""
    steps_document = _typed_node_steps_document(plan)
    runnable_step_names = [step.step_id for step in plan.steps]
    generated_files: dict[str, Any] = {
        "steps.json": steps_document,
        "runtime_entry.json": ExecutionPackageRuntimeEntry(
            entry_version="0.1",
            loader_kind="typed_node_v0_execution_package",
            execution_mode=TYPED_NODE_V0_EXECUTION_MODE,
            manifest_path="manifest.json",
            steps_source="steps.json",
            bindings_source="",
            package_metadata_source="package_metadata.json",
            supported_execution_kinds=["typed_node"],
            runnable_step_names=runnable_step_names,
            blocked_step_names=[],
            review_required_step_names=[],
            active_binding_step_names=runnable_step_names,
            is_runnable=bool(runnable_step_names) and plan.draft_status in {"complete", "partial"},
            notes=["typed_node_v0", f"draft_status={plan.draft_status}"],
        ).model_dump(mode="python"),
        "package_metadata.json": ExecutionPackageMetadata(
            package_version="0.1",
            package_kind="typed_node_v0_execution_package",
            build_id=build_id,
            executable_id=executable_id,
            blueprint_id=None,
            executable_type=ExecutableType.WORKFLOW_BUNDLE.value,
            total_steps=len(plan.steps),
            bound_step_count=len(plan.steps),
            runnable_step_count=len(runnable_step_names),
            blocked_step_count=0,
            review_required_step_count=0,
            bound_capability_ids=sorted({s.node_type for s in plan.steps}),
            runnable_capability_ids=sorted({s.node_type for s in plan.steps}),
            runtime_entry_ready=True,
            runtime_input_hints=[],
            notes=["typed_node_v0"],
        ).model_dump(mode="python"),
        "input.schema.json": {"type": "object", "properties": {}},
        "output.schema.json": {"type": "object", "properties": {}},
        "runtime.defaults.json": {},
    }
    manifest: dict[str, Any] = {
        "manifest_version": "0.1",
        "executable_id": executable_id,
        "build_id": build_id,
        "build_version": build_version,
        "project_id": project_id,
        "blueprint_id": None,
        "executable_type": "workflow",
        "compatibility": {"mode": "typed_node_v0"},
        "package": {
            "bindings": None,
            "runtime_entry": "runtime_entry.json",
            "package_metadata": "package_metadata.json",
            "asset_index": None,
        },
        "entrypoint": {"kind": "steps_file", "path": "steps.json", "handler": None},
        "schemas": {
            "input": "input.schema.json",
            "output": "output.schema.json",
            "config": "runtime.defaults.json",
        },
        "execution": {
            "strategy": TYPED_NODE_V0_STRATEGY,
            "steps_source": "steps.json",
            "nodes_source": None,
            "retry_policy_ref": None,
            "timeout_seconds": None,
        },
        "runtime": {"default_env": {}, "env_required": []},
        "artifacts": {"spec_file": None, "default_outputs": ["typed_node_outputs"]},
        "observability": {
            "trace_enabled": True,
            "step_logs": True,
            "token_usage": False,
            "metrics": [],
        },
        "files": [
            {"path": "manifest.json", "kind": "manifest"},
            *[{"path": name, "kind": "generated"} for name in generated_files.keys()],
        ],
        "metadata": {
            "typed_node_v0": {
                "plan_version": plan.plan_version,
                "draft_status": plan.draft_status,
                "step_count": len(plan.steps),
                "repaired_step_ids": list(plan.repaired_step_ids),
            }
        },
    }
    return manifest, generated_files


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


def _enforce_spec_gates(
    *,
    bundle,
    planner_output,
    compile_context=None,
    max_repair_attempts: int = 1,
) -> dict:
    """Hard-block the build when spec-time validators report issues.

    Two gates:
      1. ``edge_contract_report.issues`` — cross-node spec inconsistency
         (already produced by the spec pass; we just refuse to proceed).
      2. ``capability_mismatch_validator`` — planner-vs-spec disagreement
         on a node's compute class OR I/O shape (computed here at build
         time, since it depends on both planner output AND specs).

    On a capability_mismatch, the build first tries an auto-repair
    pass (``capability_repair.repair_capability_picks``) which rebinds
    each offending node deterministically. After up to
    ``max_repair_attempts`` rounds of repair → re-validate, surviving
    issues hard-block the build.

    Returns a dict describing what happened:
        {
            "repair_attempts": [...],      # list[CapabilityRepairAttempt]
            "planner_output_modified": bool,
        }
    so the caller can persist the repaired planner_output back to
    FrontChainState (the planner's emission was mutated in place).
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
    from app.services.workflow_planner.capability_repair import (
        repair_capability_picks,
    )

    repair_attempts: list = []
    planner_output_modified = False

    cap_report = validate_capability_mismatch(
        planner_output=planner_output,
        specs=list(bundle.specs),
        skipped_node_ids=list(bundle.skipped_node_ids),
        compile_context=compile_context,
    )

    for attempt_idx in range(max_repair_attempts):
        if not cap_report.issues:
            break
        log_event(
            logger,
            logging.INFO,
            "build.capability_repair.attempt",
            attempt=attempt_idx + 1,
            issue_count=len(cap_report.issues),
            issue_kinds=sorted({i.kind for i in cap_report.issues}),
        )
        repair_report = repair_capability_picks(
            planner_output=planner_output,
            specs=list(bundle.specs),
            issues=cap_report.issues,
        )
        repair_attempts.extend(repair_report.attempts)
        if repair_report.made_any_change:
            planner_output_modified = True
        if not repair_report.made_any_change:
            # No progress was possible — break out of the retry loop;
            # the post-loop block below will raise.
            break
        cap_report = validate_capability_mismatch(
            planner_output=planner_output,
            specs=list(bundle.specs),
            skipped_node_ids=list(bundle.skipped_node_ids),
            compile_context=compile_context,
        )

    if cap_report.issues:
        rendered = "; ".join(
            f"[{i.kind}] node={i.node_id} picked={i.selected_capability_id} "
            f"expected_class={i.expected_runtime_class} "
            f"allowed={i.allowed_runtime_classes}"
            for i in cap_report.issues
        )
        repaired_part = (
            f" (after {len(repair_attempts)} repair attempt(s))"
            if repair_attempts
            else ""
        )
        raise RuntimeError(
            f"session_anchored_build_blocked: capability_mismatch validator "
            f"reports {len(cap_report.issues)} issue(s){repaired_part}. {rendered}"
        )

    spec_reconciliations = [
        {
            "node_id": a.spec_reconciliation.node_id,
            "old_capability": a.spec_reconciliation.old_capability,
            "new_capability": a.spec_reconciliation.new_capability,
            "added_business_rule_id": a.spec_reconciliation.added_business_rule_id,
            "added_business_rule_text": a.spec_reconciliation.added_business_rule_text,
            "added_implementation_sketch_line": (
                a.spec_reconciliation.added_implementation_sketch_line
            ),
            "bumped_max_iterations_from": (
                a.spec_reconciliation.bumped_max_iterations_from
            ),
            "bumped_max_iterations_to": a.spec_reconciliation.bumped_max_iterations_to,
            "rationale": a.spec_reconciliation.rationale,
        }
        for a in repair_attempts
        if a.spec_reconciliation is not None
    ]
    return {
        "repair_attempts": [
            {
                "node_id": a.node_id,
                "issue_kind": a.issue_kind,
                "expected_runtime_class": a.expected_runtime_class,
                "original_capability": a.original_capability,
                "new_capability": a.new_capability,
                "repair_source": a.repair_source,
                "rationale": a.rationale,
                "spec_reconciled": a.spec_reconciliation is not None,
            }
            for a in repair_attempts
        ],
        "spec_reconciliations": spec_reconciliations,
        "planner_output_modified": planner_output_modified,
    }


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
            # Typed-node v0 dispatch: prefer the Pass 2 output stashed
            # under node_specs_json by the config_synthesis adapter (the
            # /plan + /specify two-stage path), fall back to the legacy
            # single-shot path where planner_output_json IS the Pass 2
            # output (used by the standalone E2E script).
            typed_node_plan = None
            if _typed_node_v0_enabled():
                typed_node_plan = _extract_typed_node_pass2_from_node_specs(
                    state_row.node_specs_json
                )
                if typed_node_plan is None and _is_typed_node_plan_payload(
                    state_row.planner_output_json
                ):
                    typed_node_plan = PlannerPass2Output.model_validate(
                        state_row.planner_output_json
                    )
            if typed_node_plan is not None:
                self._execute_typed_node_build_from_plan(
                    db=db,
                    build=build,
                    build_step_repository=build_step_repository,
                    executable_repository=executable_repository,
                    artifact_repository=artifact_repository,
                    token_usage_log_repository=token_usage_log_repository,
                    plan=typed_node_plan,
                    planner_state_id=str(state_row.id),
                )
                return
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

            # Load node_specs. Build hard-requires specs because the
            # spec pass is what carries the obligations (input/output
            # contract, business_rules, acceptance_criteria, expected
            # runtime class) the build's spec gates enforce. Without
            # specs the gates skip entirely and bad planner picks
            # (filesystem_read on a transform, validate_schema on a
            # write step) silently end up in the executable, only to
            # surface as a runtime error N steps later.
            #
            # If state_row.node_specs_json is missing, we lazy-call
            # specify_for_session here — there is no legitimate path
            # by which a session has planner_output but no specs at
            # build time. The legacy "tolerate missing specs" branch
            # used to mask the forgeDemo client bug where /builds
            # was called without /specify; that fallback is gone.
            node_specs_bundle = _load_node_specs_bundle(state_row)
            if node_specs_bundle is None:
                from app.services.node_spec.session_orchestration import (
                    specify_for_session,
                )

                log_event(
                    logger,
                    logging.INFO,
                    "build.specify.lazy_run_started",
                    build_id=build.id,
                    session_id=build.session_id,
                    reason="state_row missing node_specs_json",
                )
                # specify_for_session persists into the same state_row
                # we already have a handle on (it queries by
                # session_id + latest planner_output, which will
                # match). After it runs, refresh state_row + reload
                # planner_output (the specify pass enriched no
                # planner_output keys, but recompute defensively).
                specify_for_session(build.session_id, db=db)
                db.refresh(state_row)
                planner_output = WorkflowPlannerOutput.model_validate(
                    state_row.planner_output_json
                )
                node_specs_bundle = _load_node_specs_bundle(state_row)
                if node_specs_bundle is None:
                    raise RuntimeError(
                        "session_anchored_build_blocked: lazy specify_for_session "
                        "completed but state_row.node_specs_json still empty — "
                        "this is unexpected and indicates a deeper bug in "
                        "specify persistence."
                    )
                log_event(
                    logger,
                    logging.INFO,
                    "build.specify.lazy_run_completed",
                    build_id=build.id,
                    session_id=build.session_id,
                    spec_count=len(node_specs_bundle.specs),
                )
            spec_list = list(node_specs_bundle.specs)

            # Task 7.6B — normalize planner-emit verify-like nodes
            # into verifier obligations BEFORE the spec gates run.
            # The LLM planner sometimes authors its own verify step
            # tagged ``semantic_type=verify`` (or worse,
            # ``human_gate``) while binding ``validate_schema``.
            # Such nodes don't follow the auto-insert
            # ``verify_after_<target>`` convention; the dispatcher's
            # ``_is_verify_node`` may reject them and emit a
            # ``runnable=False`` binding, which the runtime preflight
            # then refuses to start. Lift them out of the execution
            # graph and express the verification semantic as an
            # obligation on the appropriate ``verify_after_<target>``
            # so the user's intent survives without polluting the
            # graph with non-runnable nodes.
            from app.services.build.planner_verify_normalizer import (
                normalize_planner_verify_nodes,
            )

            verify_normalizations = normalize_planner_verify_nodes(
                planner_output=planner_output,
                specs=spec_list,
            )
            verify_normalization_modified_planner = bool(verify_normalizations)
            if verify_normalizations:
                log_event(
                    logger,
                    logging.INFO,
                    "build.planner_verify_normalized",
                    build_id=build.id,
                    count=len(verify_normalizations),
                    node_ids=[n.original_node_id for n in verify_normalizations],
                )

            if node_specs_bundle is not None:
                gate_outcome = _enforce_spec_gates(
                    bundle=node_specs_bundle,
                    planner_output=planner_output,
                    compile_context=compile_context_for_adapter,
                )
                # Persist the repaired planner_output back to the
                # state row so subsequent build attempts (or a UI
                # listing) see the corrected capability picks. The
                # repair_attempts list goes into a sidecar key so
                # observability can show the audit trail without
                # mutating the planner emission's shape.
                planner_persist_required = (
                    gate_outcome.get("planner_output_modified")
                    or verify_normalization_modified_planner
                )
                if planner_persist_required:
                    state_row.planner_output_json = planner_output.model_dump(
                        mode="json"
                    )
                    repair_trace = list(
                        (state_row.planner_output_json.get("planner_metadata") or {}).get(
                            "capability_repair_attempts"
                        )
                        or []
                    )
                    repair_trace.extend(gate_outcome.get("repair_attempts") or [])
                    reconciliation_trace = list(
                        (state_row.planner_output_json.get("planner_metadata") or {}).get(
                            "spec_reconciliations"
                        )
                        or []
                    )
                    reconciliation_trace.extend(
                        gate_outcome.get("spec_reconciliations") or []
                    )
                    verify_norm_trace = list(
                        (state_row.planner_output_json.get("planner_metadata") or {}).get(
                            "planner_verify_normalizations"
                        )
                        or []
                    )
                    verify_norm_trace.extend(
                        [
                            {
                                "original_node_id": n.original_node_id,
                                "target_node_id": n.target_node_id,
                                "obligation_target_id": n.obligation_target_id,
                                "created_new_verify_after": n.created_new_verify_after,
                                "merged_into_existing": n.merged_into_existing,
                                "rerouted_incoming_edges": n.rerouted_incoming_edges,
                                "rerouted_outgoing_edges": n.rerouted_outgoing_edges,
                                "rationale": n.rationale,
                            }
                            for n in (verify_normalizations or [])
                        ]
                    )
                    pmeta = dict(
                        state_row.planner_output_json.get("planner_metadata") or {}
                    )
                    pmeta["capability_repair_attempts"] = repair_trace
                    if reconciliation_trace:
                        pmeta["spec_reconciliations"] = reconciliation_trace
                    if verify_norm_trace:
                        pmeta["planner_verify_normalizations"] = verify_norm_trace
                    state_row.planner_output_json["planner_metadata"] = pmeta
                    from sqlalchemy.orm.attributes import flag_modified

                    flag_modified(state_row, "planner_output_json")

                    # Persist the reconciled specs back. The repair
                    # mutated business_rules / implementation_sketch /
                    # execution_constraints.max_iterations on each
                    # reconciled spec; without re-serializing,
                    # subsequent reads (audit, re-run) would see the
                    # original LLM-authored spec without the
                    # reconciliation. The bundle's other fields
                    # (skipped_node_ids, edge_contract_report,
                    # profiling_summary, llm_trace) are unchanged so
                    # we re-emit the full payload structure.
                    if reconciliation_trace:
                        node_specs_bundle_dump = (
                            node_specs_bundle.model_dump(mode="json")
                        )
                        # NodeExecutionSpecBundle.model_dump emits the
                        # same shape /specify persists, so we can
                        # write it straight back.
                        existing_specs_json = state_row.node_specs_json or {}
                        existing_specs_json["specs"] = (
                            node_specs_bundle_dump.get("specs") or []
                        )
                        state_row.node_specs_json = existing_specs_json
                        flag_modified(state_row, "node_specs_json")

                    db.add(state_row)
                    db.flush()
                    log_event(
                        logger,
                        logging.INFO,
                        "build.capability_repair.persisted",
                        build_id=build.id,
                        repair_count=len(gate_outcome.get("repair_attempts") or []),
                        spec_reconciliation_count=len(reconciliation_trace),
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

    def _execute_typed_node_build_from_plan(
        self,
        *,
        db,
        build: Build,
        build_step_repository: BuildStepRepository,
        executable_repository: ExecutableRepository,
        artifact_repository: ArtifactRepository,
        token_usage_log_repository: TokenUsageLogRepository,
        plan: PlannerPass2Output,
        planner_state_id: str | None = None,
    ) -> None:
        current_step: BuildStepV2 | None = None
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

        manifest, generated_files = build_typed_node_execution_package(
            plan=plan,
            build_id=build.id,
            executable_id=executable.id,
            project_id=build.project_id,
            build_version=build.build_version,
        )
        if planner_state_id is not None:
            manifest.setdefault("metadata", {}).setdefault("typed_node_v0", {})[
                "planner_state_id"
            ] = planner_state_id

        try:
            for step in build_step_repository.list_by_build(build.id):
                current_step = step
                step.status = BuildStepStatus.RUNNING.value
                step.started_at = datetime.now(timezone.utc)
                step.input_payload_json = {
                    "session_id": build.session_id,
                    "build_version": build.build_version,
                    "typed_node_v0": True,
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
                    "typed_node_v0": True,
                    "typed_node_step_count": len(plan.steps),
                }
                step.log_excerpt = f"[completed] {step.step_name} (typed_node_v0)"
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
                        model_name="typed_node_v0",
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
                f"Typed-node v0 build: {len(plan.steps)} typed-node steps materialized."
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
                "build.typed_node_v0.completed",
                build_id=build.id,
                executable_id=executable.id,
                step_count=len(plan.steps),
            )
        except Exception as exc:
            db.rollback()
            if current_step is not None:
                current_step.status = BuildStepStatus.FAILED.value
                current_step.finished_at = datetime.now(timezone.utc)
                current_step.log_excerpt = (
                    (current_step.log_excerpt or "") + f"\n[failed] {exc}"
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
