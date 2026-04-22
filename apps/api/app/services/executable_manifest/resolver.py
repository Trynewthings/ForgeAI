from __future__ import annotations

from typing import Any

from app.db.models import Executable, RuntimeProfile
from app.schemas.executable_manifest import (
    ExecutionPackageBinding,
    ExecutionPackageBindingsDocument,
    ExecutionPackageMetadata,
    ExecutionPackageRuntimeEntry,
    ParsedManifest,
    ResolvedExecutionPlan,
    ResolvedExecutionStep,
    RuntimeInputValidationResult,
    RuntimePreflightResult,
)
from app.services.executable_manifest.validator import ExecutableManifestValidator


def _task_contract_from_step(
    *,
    raw_step: dict,
    binding: ExecutionPackageBinding,
) -> dict[str, Any]:
    """Build a skeletal ``task_contract`` dict for a ``coding_agent`` step.

    We fill in what's known at build time (objective, constraints,
    runtime caps pulled from the capability's policy hints, and any
    expected outputs the blueprint already declared). The runtime
    workload populates the remaining fields (``workspace_dir`` and
    ``input_evidence`` previews) at execution time once the run's
    sandbox directory is allocated.
    """
    config_hints = dict(binding.config_hints or {})
    runtime_cfg = {
        "timeout_seconds": int(
            binding.timeout_seconds
            or config_hints.get("timeout_seconds")
            or 120
        ),
        "memory_mb": int(config_hints.get("memory_mb") or 512),
        "allow_network": bool(
            config_hints.get("allow_network")
            or binding.network_access_required
            or False
        ),
        "max_iterations": int(config_hints.get("max_iterations") or 3),
    }
    constraints = {
        "language": str(config_hints.get("language") or "python"),
    }
    if isinstance(config_hints.get("packages_allowed"), list):
        constraints["packages_allowed"] = list(config_hints["packages_allowed"])
    if isinstance(config_hints.get("packages_forbidden"), list):
        constraints["packages_forbidden"] = list(config_hints["packages_forbidden"])

    # Expected outputs: prefer an explicit ``expected_outputs`` list on
    # the raw_step (planner-emitted), otherwise leave empty and let the
    # runtime default. Keeping this loose at build time avoids baking
    # in guesses the blueprint didn't actually declare.
    expected_outputs = []
    raw_expected = raw_step.get("expected_outputs")
    if isinstance(raw_expected, list):
        expected_outputs = [dict(item) for item in raw_expected if isinstance(item, dict)]

    return {
        "objective": (
            str(raw_step.get("purpose") or raw_step.get("description") or "")
            or f"Execute step {raw_step.get('step_name')} via the coding agent."
        ),
        # workspace_dir is intentionally absent here; the workload fills
        # it in at runtime with a deterministic per-run / per-step path.
        "input_evidence": [],
        "expected_outputs": expected_outputs,
        "constraints": constraints,
        "runtime": runtime_cfg,
        "source_node_id": raw_step.get("node_id") or raw_step.get("draft_node_id"),
    }


class ExecutableManifestResolver:
    def __init__(self) -> None:
        self.validator = ExecutableManifestValidator()

    def resolve(
        self,
        *,
        executable: Executable,
        manifest: ParsedManifest,
        runtime_profile: RuntimeProfile | None,
        run_input: dict,
        reused_input: dict | None = None,
        input_reuse: dict | None = None,
    ) -> ResolvedExecutionPlan:
        runtime_entry = self._load_runtime_entry(manifest)
        package_metadata = self._load_package_metadata(manifest)
        package_bindings = self._load_package_bindings(manifest)
        binding_map = {item.step_name: item for item in package_bindings}
        resolved_env = dict(manifest.runtime.default_env or {})
        if runtime_profile is not None:
            resolved_env.update(runtime_profile.env_vars_json or {})

        missing_env = [name for name in manifest.runtime.env_required if name not in resolved_env]

        resolved_input = {}
        field_sources: dict[str, str] = {}
        for key, value in (reused_input or {}).items():
            resolved_input[key] = value
            field_sources[str(key)] = "reused_successful_run_input"
        if runtime_profile is not None:
            for key, value in (runtime_profile.input_overrides_json or {}).items():
                resolved_input[key] = value
                field_sources[str(key)] = "runtime_profile_override"
        for key, value in (run_input or {}).items():
            resolved_input[key] = value
            field_sources[str(key)] = "run_input"

        input_validation = self.validator.validate_runtime_input(
            execution_mode=runtime_entry.execution_mode if runtime_entry is not None else "executable-manifest",
            manifest=manifest,
            resolved_input=resolved_input,
            field_sources=field_sources,
        )
        preflight = self._build_preflight_result(
            execution_mode=runtime_entry.execution_mode if runtime_entry is not None else "executable-manifest",
            input_validation=input_validation,
            missing_env=missing_env,
            input_reuse=input_reuse,
            package_runtime_entry=runtime_entry,
            package_bindings=package_bindings,
        )
        raw_steps = self._load_raw_steps(manifest)
        resolved_steps = self._build_resolved_steps(
            raw_steps=raw_steps,
            binding_map=binding_map,
            runtime_entry=runtime_entry,
            resolved_input=resolved_input,
            runtime_profile_id=runtime_profile.id if runtime_profile else None,
        )

        artifacts_spec = {
            "default_outputs": manifest.artifacts.default_outputs,
            "spec_document": manifest.loaded_documents.get("artifacts.spec_file", {}),
        }
        observability = {
            "trace_enabled": manifest.observability.trace_enabled,
            "step_logs": manifest.observability.step_logs,
            "token_usage": manifest.observability.token_usage,
            "metrics": manifest.observability.metrics,
            "debug_options": runtime_profile.debug_options_json if runtime_profile else {},
        }
        workflow_native_sidecar = dict(manifest.metadata.get("workflow_native_sidecar") or {})
        execution_package_compatibility_view = dict(manifest.metadata.get("execution_package_compatibility_view") or {})
        runtime_compatibility_sources = dict(manifest.metadata.get("runtime_compatibility_sources") or {})
        workflow_artifact = manifest.loaded_documents.get("workflow_native.workflow_artifact") or {}
        compatibility_bundle = self._build_runtime_compatibility_bundle(
            workflow_artifact=workflow_artifact if isinstance(workflow_artifact, dict) else {},
            package_bindings=package_bindings,
            runtime_entry=runtime_entry,
            resolved_steps=resolved_steps,
            step_sources=self._compatibility_step_sources(manifest),
            workflow_native_sidecar=workflow_native_sidecar,
            execution_package_compatibility_view=execution_package_compatibility_view,
            runtime_compatibility_sources=runtime_compatibility_sources,
        )

        return ResolvedExecutionPlan(
            execution_mode=runtime_entry.execution_mode if runtime_entry is not None else "executable-manifest",
            executable_id=executable.id,
            runtime_profile_id=runtime_profile.id if runtime_profile else None,
            manifest_version=manifest.manifest_version,
            executable_type=manifest.executable_type,
            compatibility_mode=manifest.compatibility.mode,
            resolved_input=resolved_input,
            resolved_input_sources=field_sources,
            resolved_env=resolved_env,
            resolved_steps=resolved_steps,
            resolved_steps_compatibility_view=compatibility_bundle["resolved_steps_compatibility_view"],
            compatibility_execution_graph_projection=compatibility_bundle["compatibility_execution_graph_projection"],
            resolved_bindings=package_bindings,
            runtime_entry=runtime_entry,
            package_metadata=package_metadata,
            workflow_artifact=workflow_artifact if isinstance(workflow_artifact, dict) else {},
            workflow_native_explainability=compatibility_bundle["workflow_native_explainability"],
            resolved_artifacts_spec=artifacts_spec,
            resolved_observability=observability,
            input_reuse=dict(input_reuse or {}),
            input_validation=input_validation,
            preflight=preflight,
        )

    def _load_raw_steps(self, manifest: ParsedManifest) -> list:
        steps_document = manifest.loaded_documents.get("execution.steps_source") or {}
        return steps_document.get("steps", []) if isinstance(steps_document, dict) else []

    def _compatibility_step_sources(self, manifest: ParsedManifest) -> list[str]:
        return [
            item
            for item in [manifest.execution.steps_source, manifest.execution.nodes_source]
            if item
        ]

    def _build_resolved_steps(
        self,
        *,
        raw_steps: list,
        binding_map: dict[str, ExecutionPackageBinding],
        runtime_entry: ExecutionPackageRuntimeEntry | None,
        resolved_input: dict,
        runtime_profile_id: str | None,
    ) -> list[ResolvedExecutionStep]:
        resolved_steps: list[ResolvedExecutionStep] = []
        for index, item in enumerate(raw_steps):
            step_name = item.get("step_name") or item.get("name") or f"step_{index + 1}" if isinstance(item, dict) else f"step_{index + 1}"
            binding = binding_map.get(step_name) if isinstance(item, dict) else None
            resolved_steps.append(
                ResolvedExecutionStep(
                    step_name=step_name,
                    node_name=item.get("node_name") if isinstance(item, dict) else None,
                    selected_capability_id=binding.selected_capability_id if binding is not None else None,
                    execution_kind=binding.execution_kind if binding is not None else None,
                    runtime_handler_ref=binding.handler_ref if binding is not None else None,
                    binding_status=binding.binding_status if binding is not None else "unbound",
                    runnable=binding.runnable if binding is not None else False,
                    manual_review_required=binding.manual_review_required if binding is not None else False,
                    payload={
                        "step_definition": self._build_step_definition(
                            raw_step=item if isinstance(item, dict) else {"step_name": str(item)},
                            binding=binding,
                        ),
                        "binding": binding.model_dump(mode="python") if binding is not None else {},
                        "runtime_entry": runtime_entry.model_dump(mode="python") if runtime_entry is not None else {},
                        "resolved_input": resolved_input,
                        "runtime_profile_id": runtime_profile_id,
                    },
                )
            )
        return resolved_steps

    def _build_runtime_compatibility_bundle(
        self,
        *,
        workflow_artifact: dict,
        package_bindings: list[ExecutionPackageBinding],
        runtime_entry: ExecutionPackageRuntimeEntry | None,
        resolved_steps: list[ResolvedExecutionStep],
        step_sources: list[str],
        workflow_native_sidecar: dict,
        execution_package_compatibility_view: dict,
        runtime_compatibility_sources: dict,
    ) -> dict:
        compatibility_execution_graph_projection = self._build_compatibility_execution_graph_projection(
            resolved_steps=resolved_steps,
            step_sources=step_sources,
        )
        resolved_steps_compatibility_view = {
            "semantic_role": "legacy_execution_compatibility_view",
            "preferred_for": ["current_runtime_step_execution", "legacy_runtime_debugging"],
            "not_authoritative_for": ["workflow_native_execution_policy", "workflow_native_explainability"],
            "backed_by_sources": step_sources,
        }
        workflow_native_explainability = {
            "preferred_entry": workflow_native_sidecar.get("preferred_explainability_entry")
            or workflow_native_sidecar.get("workflow_artifact"),
            "assembled_truth_candidate": bool(workflow_artifact),
            "workflow_artifact_path": workflow_native_sidecar.get("workflow_artifact"),
            "workflow_graph_path": workflow_native_sidecar.get("workflow_graph"),
            "projection_applied": bool(workflow_native_sidecar.get("projection_applied")),
            "verification_binding_status": workflow_native_sidecar.get("verification_binding_status"),
            "execution_package_compatibility_view": execution_package_compatibility_view,
            "runtime_compatibility_sources": runtime_compatibility_sources,
            "workflow_native_execution_policy_primary_source": (
                "workflow_native/workflow_artifact.json" if workflow_native_sidecar.get("workflow_artifact") else None
            ),
            "compatibility_execution_graph_projection": compatibility_execution_graph_projection,
            "workflow_graph_shadow_comparison": self._build_workflow_graph_shadow_comparison(
                workflow_artifact=workflow_artifact,
                compatibility_projection=compatibility_execution_graph_projection,
            ),
            "workflow_native_shadow_authority": self._build_workflow_native_shadow_authority(
                workflow_artifact=workflow_artifact,
                package_bindings=package_bindings,
                runtime_entry=runtime_entry,
            ),
            "runtime_authority_map": self._build_runtime_authority_map(),
        }
        return {
            "resolved_steps_compatibility_view": resolved_steps_compatibility_view,
            "compatibility_execution_graph_projection": compatibility_execution_graph_projection,
            "workflow_native_explainability": workflow_native_explainability,
        }

    def _build_preflight_result(
        self,
        *,
        execution_mode: str,
        input_validation: RuntimeInputValidationResult,
        missing_env: list[str],
        input_reuse: dict | None = None,
        package_runtime_entry: ExecutionPackageRuntimeEntry | None = None,
        package_bindings: list[ExecutionPackageBinding] | None = None,
    ) -> RuntimePreflightResult:
        errors: list[str] = []
        warnings = list(input_validation.warnings)
        failure_class = None
        failure_stage = None
        failure_detail = None

        dropped_keys = input_reuse.get("dropped_keys", []) if isinstance(input_reuse, dict) else []
        if dropped_keys:
            warnings.append(f"Input reuse dropped keys: {', '.join(sorted(str(key) for key in dropped_keys))}")

        env_valid = len(missing_env) == 0
        if not env_valid:
            detail = f"Missing required runtime env vars: {', '.join(sorted(missing_env))}"
            errors.append(detail)
            failure_class = "runtime_env_missing"
            failure_stage = "preflight"
            failure_detail = detail

        if not input_validation.valid:
            errors.extend(input_validation.errors)
            if failure_class is None:
                failure_class = "runtime_input_invalid"
                failure_stage = "preflight"
                failure_detail = "; ".join(input_validation.errors)

        blocked_bindings = [
            item
            for item in (package_bindings or [])
            if item.binding_status in {"blocked", "unbound", "review_needed"} and not item.runnable
        ]
        if blocked_bindings:
            detail = "Execution package contains non-runnable steps: " + ", ".join(
                sorted(f"{item.step_name}({item.binding_status})" for item in blocked_bindings)
            )
            errors.append(detail)
            if failure_class is None:
                failure_class = "execution_package_blocked"
                failure_stage = "preflight"
                failure_detail = detail

        if package_runtime_entry is not None and not package_runtime_entry.is_runnable and failure_class is None:
            warnings.append("Execution package runtime entry reports non-runnable or partially blocked steps.")

        return RuntimePreflightResult(
            valid=env_valid and input_validation.valid and not blocked_bindings,
            execution_mode=execution_mode,
            manifest_valid=True,
            input_valid=input_validation.valid,
            env_valid=env_valid,
            config_valid=True,
            failure_class=failure_class,
            failure_stage=failure_stage,
            failure_detail=failure_detail,
            errors=errors,
            warnings=warnings,
            input_validation=input_validation,
        )

    def _load_runtime_entry(self, manifest: ParsedManifest) -> ExecutionPackageRuntimeEntry | None:
        payload = manifest.loaded_documents.get("package.runtime_entry")
        if isinstance(payload, dict):
            return ExecutionPackageRuntimeEntry.model_validate(payload)
        return None

    def _load_package_bindings(self, manifest: ParsedManifest) -> list[ExecutionPackageBinding]:
        payload = manifest.loaded_documents.get("package.bindings")
        if not isinstance(payload, dict):
            return []
        return ExecutionPackageBindingsDocument.model_validate(payload).bindings

    def _load_package_metadata(self, manifest: ParsedManifest) -> ExecutionPackageMetadata | None:
        payload = manifest.loaded_documents.get("package.package_metadata")
        if isinstance(payload, dict):
            return ExecutionPackageMetadata.model_validate(payload)
        return None

    def _build_step_definition(
        self,
        *,
        raw_step: dict,
        binding: ExecutionPackageBinding | None,
    ) -> dict:
        base = dict(raw_step)
        if binding is None:
            base.setdefault("step_type", str(base.get("step_type") or "llm_task"))
            return base
        if binding.execution_kind == "python_function":
            return {
                **base,
                "step_type": "python_function",
                "function_name": binding.handler_ref or binding.selected_capability_id or raw_step.get("step_name"),
                "args": dict(binding.config_hints or {}),
                "selected_capability_id": binding.selected_capability_id,
            }
        if binding.execution_kind == "artifact_writer":
            return {
                **base,
                "step_type": "artifact_writer",
                "writer_kind": binding.handler_ref or binding.selected_capability_id or raw_step.get("step_name"),
                "args": dict(binding.config_hints or {}),
                "selected_capability_id": binding.selected_capability_id,
            }
        if binding.execution_kind in {"web_search", "network_fetch", "filesystem", "terminal_command", "browser_interaction", "template_runtime"}:
            return {
                **base,
                "step_type": binding.execution_kind,
                "handler_ref": binding.handler_ref or binding.selected_capability_id or raw_step.get("step_name"),
                "runtime_adapter": binding.runtime_adapter,
                "args": dict(binding.config_hints or {}),
                "selected_capability_id": binding.selected_capability_id,
                "action_kind": binding.action_kind,
                "requires_confirmation": binding.requires_confirmation,
                "network_access_required": binding.network_access_required,
                "allowed_domains": list(binding.allowed_domains or []),
                "source_mode": binding.source_mode,
                "policy_scope": binding.policy_scope,
                "timeout_seconds": binding.timeout_seconds,
            }
        if binding.execution_kind == "sandboxed_script":
            # coding_agent capability — the workload will author + run
            # a python/bash script in a bounded sandbox. Build-time we
            # only stitch together what's already on the raw step +
            # binding; the rest of the task_contract (workspace_dir,
            # missing previews) is populated at runtime.
            return {
                **base,
                "step_type": "coding_agent",
                "handler_ref": binding.handler_ref or binding.selected_capability_id or "coding_agent",
                "runtime_adapter": binding.runtime_adapter or "bounded_code_runner",
                "args": dict(binding.config_hints or {}),
                "selected_capability_id": binding.selected_capability_id or "coding_agent",
                "timeout_seconds": binding.timeout_seconds,
                "task_contract": _task_contract_from_step(
                    raw_step=raw_step, binding=binding
                ),
            }
        if binding.execution_kind == "blocked":
            return {
                **base,
                "step_type": "blocked",
                "selected_capability_id": binding.selected_capability_id,
                "blocked_reason": binding.blocked_reason,
                "manual_review_required": binding.manual_review_required,
            }
        step_def: dict = {
            **base,
            "step_type": "llm_task",
            "task_kind": binding.handler_ref or binding.selected_capability_id or raw_step.get("step_name"),
            "instructions": raw_step.get("purpose") or raw_step.get("description") or f"Execute {raw_step.get('step_name')}.",
            "selected_capability_id": binding.selected_capability_id,
            "expected_output": {
                "logical_step_kind": binding.logical_step_kind,
                "output_intent": raw_step.get("output_intent"),
            },
        }
        # Merge raw step args (from blueprint) into step_def args — preserves allow_network, allowed_domains, etc.
        raw_step_args = raw_step.get("args") if isinstance(raw_step.get("args"), dict) else {}
        if raw_step_args:
            step_def.setdefault("args", {}).update(raw_step_args)

        # Propagate agentic loop declaration from config_hints into args
        config_hints = dict(binding.config_hints or {})
        available_tools = config_hints.get("available_tools")
        loop_config = config_hints.get("loop_config")
        if isinstance(available_tools, list) and available_tools:
            args = step_def.setdefault("args", {})
            args["available_tools"] = available_tools
            if isinstance(loop_config, dict):
                args["loop_config"] = loop_config
            # Propagate system/task prompt from config_hints if present
            if config_hints.get("system_prompt"):
                step_def["system_prompt"] = config_hints["system_prompt"]
            if config_hints.get("task_prompt"):
                step_def["task_prompt"] = config_hints["task_prompt"]
            # Mark side effect class for eligibility check
            step_def.setdefault("side_effect_class", config_hints.get("side_effect_class", "external_read"))
        return step_def

    def _build_compatibility_execution_graph_projection(
        self,
        *,
        resolved_steps: list[ResolvedExecutionStep],
        step_sources: list[str],
    ) -> dict:
        nodes: list[dict] = []
        edges: list[dict] = []
        for index, step in enumerate(resolved_steps, start=1):
            node_id = f"node_{index}"
            nodes.append(
                {
                    "node_id": node_id,
                    "step_name": step.step_name,
                    "node_name": step.node_name,
                    "selected_capability_id": step.selected_capability_id,
                    "execution_kind": step.execution_kind,
                    "binding_status": step.binding_status,
                    "runnable": step.runnable,
                    "manual_review_required": step.manual_review_required,
                }
            )
            if index > 1:
                edges.append(
                    {
                        "edge_id": f"compat_edge_{index - 1}_{index}",
                        "edge_type": "data_flow",
                        "from_node_id": f"node_{index - 1}",
                        "to_node_id": node_id,
                    }
                )
        return {
            "graph_id": "compatibility_execution_graph_projection_v1",
            "semantic_role": "runtime_shadow_compatibility_projection",
            "projection_source": "resolved_steps_and_bindings",
            "backed_by_sources": list(step_sources),
            "nodes": nodes,
            "edges": edges,
            "entry_nodes": [nodes[0]["node_id"]] if nodes else [],
            "exit_nodes": [nodes[-1]["node_id"]] if nodes else [],
        }

    def _build_workflow_graph_shadow_comparison(
        self,
        *,
        workflow_artifact: dict,
        compatibility_projection: dict,
    ) -> dict:
        workflow_graph = workflow_artifact.get("workflow_graph", {}) if isinstance(workflow_artifact, dict) else {}
        workflow_nodes = workflow_graph.get("node_instances", []) if isinstance(workflow_graph, dict) else []
        workflow_edges = workflow_graph.get("edge_set", []) if isinstance(workflow_graph, dict) else []
        compatibility_nodes = compatibility_projection.get("nodes", []) if isinstance(compatibility_projection, dict) else []
        compatibility_edges = compatibility_projection.get("edges", []) if isinstance(compatibility_projection, dict) else []

        def workflow_step_name(node: dict, index: int) -> str:
            hints = node.get("instance_execution_hints", {}) if isinstance(node, dict) else {}
            if isinstance(hints, dict):
                hint_step_name = hints.get("step_name")
                if isinstance(hint_step_name, str) and hint_step_name:
                    return hint_step_name
            spec_ref = node.get("spec_ref") if isinstance(node, dict) else None
            if isinstance(spec_ref, str) and spec_ref.endswith("_spec"):
                return spec_ref[:-5]
            node_id = node.get("node_id") if isinstance(node, dict) else None
            return str(node_id or f"node_{index}")

        workflow_step_names = [
            workflow_step_name(node, index + 1)
            for index, node in enumerate(workflow_nodes)
            if isinstance(node, dict)
        ]
        compatibility_step_names = [
            str(node.get("step_name") or f"step_{index + 1}")
            for index, node in enumerate(compatibility_nodes)
            if isinstance(node, dict)
        ]
        workflow_set = set(workflow_step_names)
        compatibility_set = set(compatibility_step_names)
        missing_in_workflow = sorted(compatibility_set - workflow_set)
        missing_in_compatibility = sorted(workflow_set - compatibility_set)
        aligned_step_names = [name for name in compatibility_step_names if name in workflow_set]
        structural_drift_detected = bool(
            missing_in_workflow
            or missing_in_compatibility
            or len(workflow_edges) != len(compatibility_edges)
        )

        return {
            "comparison_mode": "workflow_graph_vs_runtime_compatibility_projection",
            "workflow_graph_present": bool(workflow_nodes),
            "compatibility_projection_present": bool(compatibility_nodes),
            "workflow_graph_node_count": len(workflow_nodes),
            "compatibility_projection_node_count": len(compatibility_nodes),
            "workflow_graph_edge_count": len(workflow_edges),
            "compatibility_projection_edge_count": len(compatibility_edges),
            "aligned_step_names": aligned_step_names,
            "missing_in_workflow_graph": missing_in_workflow,
            "missing_in_compatibility_projection": missing_in_compatibility,
            "structural_drift_detected": structural_drift_detected,
        }

    def _build_workflow_native_shadow_authority(
        self,
        *,
        workflow_artifact: dict,
        package_bindings: list[ExecutionPackageBinding],
        runtime_entry: ExecutionPackageRuntimeEntry | None,
    ) -> dict:
        execution_contract = (
            workflow_artifact.get("execution_contract", {})
            if isinstance(workflow_artifact, dict)
            else {}
        )
        candidate_intervention = (
            execution_contract.get("intervention_legality", {})
            if isinstance(execution_contract, dict)
            else {}
        )
        candidate_rerun = (
            execution_contract.get("rerun_legality_policy", {})
            if isinstance(execution_contract, dict)
            else {}
        )
        candidate_human_gate = (
            execution_contract.get("human_gate_policy", {})
            if isinstance(execution_contract, dict)
            else {}
        )
        candidate_failure_escalation = (
            execution_contract.get("failure_escalation_policy", {})
            if isinstance(execution_contract, dict)
            else {}
        )

        compatibility_intervention = self._build_compatibility_intervention_authority(
            package_bindings=package_bindings,
            runtime_entry=runtime_entry,
        )
        compatibility_rerun = self._build_compatibility_rerun_authority(
            package_bindings=package_bindings,
            runtime_entry=runtime_entry,
        )
        compatibility_human_gate = self._build_compatibility_human_gate_policy(
            package_bindings=package_bindings,
            runtime_entry=runtime_entry,
        )
        compatibility_failure_escalation = self._build_compatibility_failure_escalation_hints(
            package_bindings=package_bindings,
            runtime_entry=runtime_entry,
        )

        intervention_match = (
            bool(candidate_intervention)
            and candidate_intervention == compatibility_intervention
        )
        rerun_match = bool(candidate_rerun) and candidate_rerun == compatibility_rerun
        human_gate_match = bool(candidate_human_gate) and candidate_human_gate == compatibility_human_gate
        failure_escalation_match = (
            bool(candidate_failure_escalation)
            and candidate_failure_escalation == compatibility_failure_escalation
        )

        return {
            "shadow_mode": "advisory_only",
            "primary_candidate_source": "workflow_native/workflow_artifact.json",
            "compatibility_source": "resolved_steps_and_bindings",
            "intervention_legality": {
                "candidate": candidate_intervention,
                "compatibility_projection": compatibility_intervention,
                "matches": intervention_match,
                "structural_drift_detected": bool(candidate_intervention) and not intervention_match,
            },
            "rerun_legality": {
                "candidate": candidate_rerun,
                "compatibility_projection": compatibility_rerun,
                "matches": rerun_match,
                "structural_drift_detected": bool(candidate_rerun) and not rerun_match,
            },
            "human_gate_policy": {
                "candidate": candidate_human_gate,
                "compatibility_projection": compatibility_human_gate,
                "matches": human_gate_match,
                "structural_drift_detected": bool(candidate_human_gate) and not human_gate_match,
            },
            "failure_escalation_hints": {
                "candidate": candidate_failure_escalation,
                "compatibility_projection": compatibility_failure_escalation,
                "matches": failure_escalation_match,
                "structural_drift_detected": bool(candidate_failure_escalation) and not failure_escalation_match,
            },
        }

    def _build_runtime_authority_map(self) -> dict:
        return {
            "phase": "stage5_partial_authority_migration",
            "structure_truth": {
                "preferred_source": "workflow_native/workflow_artifact.json#workflow_graph",
                "compatibility_fallbacks": [
                    "workflow_native/workflow_graph.json",
                    "steps.json",
                    "nodes.json",
                    "workflow.bundle.json",
                ],
                "status": "workflow_native_preferred_with_compatibility_fallback",
            },
            "runtime_policy_authority": {
                "execution_order_authority": {
                    "status": "partially_migrated",
                    "primary_source": "workflow_artifact.workflow_graph.linear_order",
                    "fallback_source": "resolved_steps.compatibility_sequence",
                    "consumed_by": [
                        "run_execution.scheduler_selection",
                        "run_execution.pending_step_checkpoint_routing",
                    ],
                },
                "human_gate_policy": {
                    "status": "partially_migrated",
                    "primary_source": "workflow_artifact.execution_contract.human_gate_policy",
                    "fallback_source": "resolved_steps.compatibility_step_definition.requires_confirmation",
                    "consumed_by": ["run_execution.confirmation_gate_check"],
                },
                "failure_escalation_policy": {
                    "status": "partially_migrated",
                    "primary_source": "workflow_artifact.execution_contract.failure_escalation_policy",
                    "fallback_source": "resolved_steps_and_bindings.compatibility_projection",
                    "consumed_by": ["run_execution.failure_to_intervention_routing"],
                },
                "rerun_legality_policy": {
                    "status": "partially_migrated",
                    "primary_source": "workflow_artifact.execution_contract.rerun_legality_policy",
                    "fallback_source": "resolved_steps_and_bindings.compatibility_projection",
                    "consumed_by": [
                        "run_execution.failure_recovery_options",
                        "run_recovery.retry_step_admission",
                    ],
                },
                "intervention_legality": {
                    "status": "partially_migrated",
                    "primary_source": "workflow_artifact.execution_contract.intervention_legality",
                    "fallback_source": "resolved_steps_and_bindings.compatibility_projection",
                    "consumed_by": [
                        "run_execution.failure_recovery_options",
                        "run_resume.resume_admission",
                        "run_recovery.retry_step_admission",
                    ],
                },
            },
            "compatibility_views": {
                "execution_package_metadata": "legacy_execution_compatibility_view",
                "resolved_steps": "legacy_execution_compatibility_view",
                "runtime_sources": "current_runtime_compatibility_source",
            },
        }

    def _build_compatibility_intervention_authority(
        self,
        *,
        package_bindings: list[ExecutionPackageBinding],
        runtime_entry: ExecutionPackageRuntimeEntry | None,
    ) -> dict:
        requires_confirmation = any(item.requires_confirmation for item in package_bindings)
        requires_manual_review = any(item.manual_review_required for item in package_bindings)
        allowed_modes = ["inspect", "rerun"]
        if requires_confirmation or requires_manual_review or (
            runtime_entry is not None and bool(runtime_entry.review_required_step_names)
        ):
            allowed_modes.extend(["approve", "reject"])
        return {
            "allowed_modes": allowed_modes,
            "required_before_execute": requires_confirmation,
            "required_before_commit": requires_manual_review or (
                runtime_entry is not None and bool(runtime_entry.review_required_step_names)
            ),
            "editable_fields": [],
            "skip_policy": "forbidden",
            "resume_policy": "resume_from_current",
        }

    def _build_compatibility_rerun_authority(
        self,
        *,
        package_bindings: list[ExecutionPackageBinding],
        runtime_entry: ExecutionPackageRuntimeEntry | None,
    ) -> dict:
        runnable_bindings = [item for item in package_bindings if item.runnable]
        return {
            "rerun_allowed": all(item.execution_kind != "artifact_writer" for item in runnable_bindings),
            "rerun_requires_human_approval": any(
                item.requires_confirmation or item.manual_review_required for item in package_bindings
            ) or (runtime_entry is not None and bool(runtime_entry.review_required_step_names)),
            "rerun_invalidates_downstream": True,
            "rerun_creates_branch": False,
        }

    def _build_compatibility_human_gate_policy(
        self,
        *,
        package_bindings: list[ExecutionPackageBinding],
        runtime_entry: ExecutionPackageRuntimeEntry | None,
    ) -> dict:
        required_gate_node_ids = [
            f"node_{index}"
            for index, binding in enumerate(package_bindings, start=1)
            if binding.requires_confirmation or binding.manual_review_required
        ]
        if runtime_entry is not None and runtime_entry.review_required_step_names:
            for index, binding in enumerate(package_bindings, start=1):
                if binding.step_name in runtime_entry.review_required_step_names:
                    node_id = f"node_{index}"
                    if node_id not in required_gate_node_ids:
                        required_gate_node_ids.append(node_id)
        return {
            "required_gate_node_ids": required_gate_node_ids,
            "approval_timeout_seconds": None,
        }

    def _build_compatibility_failure_escalation_hints(
        self,
        *,
        package_bindings: list[ExecutionPackageBinding],
        runtime_entry: ExecutionPackageRuntimeEntry | None,
    ) -> dict:
        has_human_gate = any(item.requires_confirmation or item.manual_review_required for item in package_bindings) or (
            runtime_entry is not None and bool(runtime_entry.review_required_step_names)
        )
        has_blocked_or_nonrunnable = any(
            item.binding_status in {"blocked", "unbound"} or not item.runnable
            for item in package_bindings
        ) or (runtime_entry is not None and bool(runtime_entry.blocked_step_names))
        return {
            "escalate_on_node_failure": True,
            "escalate_to_human_gate": has_human_gate,
            "stop_graph_on_unverified_side_effect": True,
            "compatibility_runtime_outcome_on_failure": (
                "waiting_for_intervention" if has_human_gate or has_blocked_or_nonrunnable else "failed_terminal"
            ),
        }
