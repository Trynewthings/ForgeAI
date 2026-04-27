"""Tests for typed-node v0 build materialization and resolver dispatch."""
from __future__ import annotations

from app.db.models import Executable
from app.schemas.executable_manifest import ParsedManifest
import app.services.build.build_execution_service as build_service
from app.services.build.build_execution_service import (
    TYPED_NODE_V0_EXECUTION_MODE,
    TYPED_NODE_V0_STRATEGY,
    _is_typed_node_plan_payload,
    _should_dispatch_typed_node_build,
    build_typed_node_execution_package,
)
from app.services.executable_manifest.resolver import ExecutableManifestResolver
from app.services.executable_manifest.validator import ExecutableManifestValidator
from app.services.typed_nodes.planner_pass2.schema import (
    ConfiguredStep,
    PlannerPass2Output,
    SchemaFlowSnapshot,
    TypedConfigPayload,
)


def _plan() -> PlannerPass2Output:
    return PlannerPass2Output(
        draft_status="complete",
        steps=[
            ConfiguredStep(
                step_id="read_input",
                node_type="read_csv",
                intent="Read input CSV",
                consumes_artifact_ids=["input"],
                typed_config=TypedConfigPayload(
                    node_type="read_csv",
                    config={
                        "path": "/tmp/input.csv",
                        "delimiter": ",",
                        "dtype_overrides": {"sku_id": "str"},
                    },
                    synthesized_by="llm",
                ),
            ),
            ConfiguredStep(
                step_id="write_output",
                node_type="write_csv",
                intent="Write output CSV",
                upstream_step_ids=["read_input"],
                produces_artifact_ids=["output"],
                typed_config=TypedConfigPayload(
                    node_type="write_csv",
                    config={
                        "path": "/tmp/output.csv",
                        "columns_in_order": ["sku_id"],
                    },
                    synthesized_by="llm",
                ),
            ),
        ],
        entry_step_ids=["read_input"],
        exit_step_ids=["write_output"],
        schema_flow=[
            SchemaFlowSnapshot(
                step_id="read_input",
                output_columns=["sku_id"],
            ),
            SchemaFlowSnapshot(
                step_id="write_output",
                upstream_step_ids=["read_input"],
                upstream_columns=[["sku_id"]],
                output_columns=[],
            ),
        ],
    )


def test_typed_node_plan_detection():
    assert _is_typed_node_plan_payload(_plan().model_dump(mode="python"))
    assert not _is_typed_node_plan_payload({"steps": []})


def test_typed_node_build_dispatch_respects_feature_flag(monkeypatch):
    payload = _plan().model_dump(mode="python")

    monkeypatch.setattr(
        build_service.settings,
        "typed_node_v0_enabled",
        True,
        raising=False,
    )
    assert _should_dispatch_typed_node_build(payload)

    monkeypatch.setattr(
        build_service.settings,
        "typed_node_v0_enabled",
        False,
        raising=False,
    )
    assert not _should_dispatch_typed_node_build(payload)


def test_build_typed_node_execution_package_shape():
    manifest, files = build_typed_node_execution_package(
        plan=_plan(),
        build_id="build-1",
        executable_id="exe-1",
        project_id="proj-1",
        build_version="v1",
    )
    assert manifest["execution"]["strategy"] == TYPED_NODE_V0_STRATEGY
    assert manifest["metadata"]["typed_node_v0"]["step_count"] == 2
    assert files["runtime_entry.json"]["execution_mode"] == TYPED_NODE_V0_EXECUTION_MODE
    assert files["runtime_entry.json"]["supported_execution_kinds"] == ["typed_node"]
    steps = files["steps.json"]["steps"]
    assert steps[0]["step_type"] == "typed_node"
    assert steps[0]["node_type"] == "read_csv"
    assert steps[0]["typed_config"]["config"]["path"] == "/tmp/input.csv"


def test_manifest_validator_accepts_typed_node_strategy():
    manifest, files = build_typed_node_execution_package(plan=_plan())
    parsed = ParsedManifest.model_validate({
        **manifest,
        "loaded_documents": {
            "entrypoint": files["steps.json"],
            "execution.steps_source": files["steps.json"],
            "schemas.input": files["input.schema.json"],
            "schemas.output": files["output.schema.json"],
            "schemas.config": files["runtime.defaults.json"],
            "package.runtime_entry": files["runtime_entry.json"],
            "package.package_metadata": files["package_metadata.json"],
        },
    })
    result = ExecutableManifestValidator().validate(parsed)
    assert result.valid
    assert result.errors == []


def test_resolver_builds_typed_node_resolved_steps():
    manifest, files = build_typed_node_execution_package(
        plan=_plan(),
        executable_id="exe-1",
    )
    parsed = ParsedManifest.model_validate({
        **manifest,
        "loaded_documents": {
            "entrypoint": files["steps.json"],
            "execution.steps_source": files["steps.json"],
            "schemas.input": files["input.schema.json"],
            "schemas.output": files["output.schema.json"],
            "schemas.config": files["runtime.defaults.json"],
            "package.runtime_entry": files["runtime_entry.json"],
            "package.package_metadata": files["package_metadata.json"],
        },
    })
    executable = Executable(id="exe-1", project_id="proj-1", executable_type="workflow")

    plan = ExecutableManifestResolver().resolve(
        executable=executable,
        manifest=parsed,
        runtime_profile=None,
        run_input={},
    )

    assert plan.execution_mode == TYPED_NODE_V0_EXECUTION_MODE
    assert len(plan.resolved_steps) == 2
    first = plan.resolved_steps[0]
    assert first.step_name == "read_input"
    assert first.execution_kind == "typed_node"
    assert first.binding_status == "runnable"
    assert first.runnable is True
    step_def = first.payload["step_definition"]
    assert step_def["step_type"] == "typed_node"
    assert step_def["typed_config"]["config"]["path"] == "/tmp/input.csv"
