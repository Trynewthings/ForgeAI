"""Typed-node v0 E2E runner for the spare_parts scenario.

This bypasses the legacy front-chain/capability-binding path on purpose:
it constructs a PlannerPass2Output, materializes the typed-node execution
package, resolves it, then executes each typed node deterministically.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
API_ROOT = REPO_ROOT / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import app.services.typed_nodes.executors  # noqa: E402,F401
from app.db.models import Executable  # noqa: E402
from app.schemas.executable_manifest import ParsedManifest  # noqa: E402
from app.services.build.build_execution_service import (  # noqa: E402
    build_typed_node_execution_package,
)
from app.services.executable_manifest.resolver import (  # noqa: E402
    ExecutableManifestResolver,
)
from app.services.run.case_workloads import execute_typed_node_workload  # noqa: E402
from app.services.typed_nodes.planner_pass2.schema import (  # noqa: E402
    ConfiguredStep,
    PlannerPass2Output,
    SchemaFlowSnapshot,
    TypedConfigPayload,
)


SCENARIO_ROOT = Path(__file__).resolve().parent
INPUT_DIR = SCENARIO_ROOT / "input"
DEFAULT_OUTPUT = SCENARIO_ROOT / "output" / "replenishment_queue.csv"


FINAL_COLUMNS = [
    "queue_id",
    "sku_id",
    "part_name",
    "supplier_id",
    "supplier_name",
    "trigger_type",
    "risk_before",
    "recommended_qty",
    "order_qty_rounded",
    "estimated_cost",
    "approval_mode",
    "expected_arrival_date",
    "status",
    "confidence",
    "calculation_note",
]


def _cfg(node_type: str, config: dict[str, Any], by: str = "llm") -> TypedConfigPayload:
    return TypedConfigPayload(node_type=node_type, config=config, synthesized_by=by)


def _step(
    step_id: str,
    node_type: str,
    config: dict[str, Any],
    *,
    upstream: list[str] | None = None,
    consumes: list[str] | None = None,
    produces: list[str] | None = None,
    intent: str | None = None,
) -> ConfiguredStep:
    return ConfiguredStep(
        step_id=step_id,
        node_type=node_type,
        intent=intent or step_id.replace("_", " "),
        upstream_step_ids=list(upstream or []),
        consumes_artifact_ids=list(consumes or []),
        produces_artifact_ids=list(produces or []),
        typed_config=_cfg(node_type, config),
    )


def _read_csv_config(filename: str, columns: list[str]) -> dict[str, Any]:
    return {
        "path": str(INPUT_DIR / filename),
        "delimiter": ",",
        "dtype_overrides": {column: "str" for column in columns},
    }


def build_spare_parts_plan(output_path: Path = DEFAULT_OUTPUT) -> PlannerPass2Output:
    inventory_columns = [
        "sku_id", "part_name", "warehouse", "category", "criticality",
        "lifecycle", "unit_cost", "supplier_id", "lead_time_days", "moq",
        "reorder_multiple", "on_hand", "reserved", "available_qty",
        "inbound_approved", "last_event_ts",
    ]
    sku_columns = [
        "sku_id", "part_name", "category", "criticality", "unit",
        "unit_cost", "supplier_id", "lead_time_days", "moq",
        "reorder_multiple", "target_service_level", "shelf_life_days",
        "lifecycle", "substitution_group", "max_stock", "base_daily_demand",
    ]
    supplier_columns = [
        "supplier_id", "supplier_name", "region", "contract_leadtime_days",
        "latest_leadtime_days", "on_time_rate", "default_moq", "quality_ppm",
        "reliability_grade", "last_delay_event",
    ]

    steps = [
        _step(
            "read_inventory",
            "read_csv",
            _read_csv_config("inventory_rt.csv", inventory_columns),
            consumes=["inventory_rt"],
            intent="Read real-time inventory.",
        ),
        _step(
            "read_sku_master",
            "read_csv",
            _read_csv_config("sku_master.csv", sku_columns),
            consumes=["sku_master"],
            intent="Read SKU master demand policy fields.",
        ),
        _step(
            "project_sku_policy",
            "project",
            {"columns": ["sku_id", "base_daily_demand", "max_stock"]},
            upstream=["read_sku_master"],
            intent="Keep only SKU policy fields needed downstream.",
        ),
        _step(
            "merge_inventory_policy",
            "merge",
            {"on": "sku_id", "how": "left", "require_no_nulls_after": ["base_daily_demand", "max_stock"]},
            upstream=["read_inventory", "project_sku_policy"],
            intent="Join inventory with SKU demand policy.",
        ),
        _step(
            "read_supplier",
            "read_csv",
            _read_csv_config("supplier_performance.csv", supplier_columns),
            consumes=["supplier_performance"],
            intent="Read supplier performance.",
        ),
        _step(
            "project_supplier",
            "project",
            {"columns": ["supplier_id", "supplier_name", "latest_leadtime_days", "reliability_grade"]},
            upstream=["read_supplier"],
            intent="Keep supplier fields needed for approval and arrival date.",
        ),
        _step(
            "merge_supplier",
            "merge",
            {"on": "supplier_id", "how": "left", "require_no_nulls_after": ["supplier_name", "latest_leadtime_days"]},
            upstream=["merge_inventory_policy", "project_supplier"],
            intent="Join supplier details.",
        ),
        _step(
            "derive_policy_metrics",
            "formula_eval",
            {
                "assignments": [
                    {
                        "target": "avg_daily_demand",
                        "expression": "base_daily_demand",
                        "null_propagation": "raise",
                    },
                    {
                        "target": "criticality_multiplier",
                        "expression": "2.0 if criticality == 'A' else 1.5 if criticality == 'B' else 1.2",
                        "null_propagation": "raise",
                    },
                    {
                        "target": "safety_stock_internal",
                        "expression": "ceil(avg_daily_demand * lead_time_days * (criticality_multiplier - 1))",
                        "null_propagation": "raise",
                    },
                    {
                        "target": "reorder_point_internal",
                        "expression": "ceil(avg_daily_demand * lead_time_days + safety_stock_internal)",
                    },
                    {
                        "target": "target_stock_internal",
                        "expression": "min(max_stock, reorder_point_internal + safety_stock_internal + ceil(avg_daily_demand * lead_time_days))",
                    },
                    {
                        "target": "forecast_28d_qty",
                        "expression": "ceil(avg_daily_demand * 28)",
                    },
                    {
                        "target": "dos_days",
                        "expression": "available_qty / max(avg_daily_demand, 0.1)",
                    },
                ],
                "coerce_string_to_number": True,
                "validate_non_negative": [
                    "safety_stock_internal",
                    "reorder_point_internal",
                    "target_stock_internal",
                    "forecast_28d_qty",
                ],
            },
            upstream=["merge_supplier"],
            intent="Derive stock policy metrics.",
        ),
        _step(
            "classify_risk",
            "classify_state_space",
            {
                "target_field": "risk_before",
                "evidence_field": "risk_evidence",
                "allowed_values": [
                    "Stockout Risk",
                    "Watch",
                    "Normal",
                    "Overstock Risk",
                    "Replenishment Blocked",
                ],
                "decision_list": [
                    {
                        "when": "lifecycle in ['EOL', 'Discontinued']",
                        "then": "Replenishment Blocked",
                        "note": "lifecycle blocked",
                    },
                    {
                        "when": "(available_qty + inbound_approved) < reorder_point_internal and criticality == 'A'",
                        "then": "Stockout Risk",
                        "note": "below reorder point",
                    },
                    {
                        "when": "available_qty > target_stock_internal * 1.35",
                        "then": "Overstock Risk",
                        "note": "above target stock",
                    },
                    {
                        "when": "dos_days < lead_time_days",
                        "then": "Watch",
                        "note": "days of supply below lead time",
                    },
                ],
                "default_value": "Normal",
                "require_match": False,
            },
            upstream=["derive_policy_metrics"],
            intent="Classify inventory risk state.",
        ),
        _step(
            "derive_order_quantities",
            "formula_eval",
            {
                "assignments": [
                    {
                        "target": "target_at_horizon",
                        "expression": "target_stock_internal + forecast_28d_qty",
                    },
                    {
                        "target": "gap_to_target",
                        "expression": "max(0, target_at_horizon - available_qty - inbound_approved)",
                    },
                    {
                        "target": "recommended_qty",
                        "expression": "0 if risk_before in ['Normal', 'Overstock Risk', 'Replenishment Blocked'] else gap_to_target if gap_to_target >= moq else 0",
                    },
                    {
                        "target": "order_qty_rounded",
                        "expression": "ceil(recommended_qty / max(reorder_multiple, 1)) * max(reorder_multiple, 1) if recommended_qty > 0 else 0",
                    },
                    {
                        "target": "estimated_cost",
                        "expression": "order_qty_rounded * unit_cost",
                    },
                    {
                        "target": "effective_lead_time",
                        "expression": "max(latest_leadtime_days, lead_time_days)",
                    },
                    {
                        "target": "expected_arrival_date",
                        "expression": "today_plus_days(effective_lead_time) if order_qty_rounded > 0 else ''",
                    },
                    {
                        "target": "trigger_type",
                        "expression": "'below_reorder_point' if risk_before == 'Stockout Risk' else 'dos_under_threshold' if risk_before == 'Watch' else 'overstock_freeze' if risk_before == 'Overstock Risk' else 'blocked_lifecycle'",
                    },
                ],
                "coerce_string_to_number": True,
                "validate_non_negative": ["recommended_qty", "order_qty_rounded", "estimated_cost"],
            },
            upstream=["classify_risk"],
            intent="Calculate order quantities and trigger type.",
        ),
        _step(
            "classify_approval",
            "classify_state_space",
            {
                "target_field": "approval_mode",
                "allowed_values": ["Auto PO", "Human Approval", "No PO"],
                "decision_list": [
                    {
                        "when": "risk_before in ['Overstock Risk', 'Replenishment Blocked'] or order_qty_rounded == 0",
                        "then": "No PO",
                        "note": "no purchase order should be created",
                    },
                    {
                        "when": "unit_cost >= 5000 or criticality == 'A' or reliability_grade in ['C', 'D']",
                        "then": "Human Approval",
                        "note": "high value, critical, or supplier risk",
                    },
                ],
                "default_value": "Auto PO",
                "require_match": False,
            },
            upstream=["derive_order_quantities"],
            intent="Classify approval mode.",
        ),
        _step(
            "derive_queue_fields",
            "formula_eval",
            {
                "assignments": [
                    {
                        "target": "queue_id",
                        "expression": "'RQ-' + sku_id",
                    },
                    {
                        "target": "status",
                        "expression": "'Purchase Frozen' if risk_before == 'Overstock Risk' else 'Blocked' if risk_before == 'Replenishment Blocked' else 'Drafted'",
                    },
                    {
                        "target": "confidence",
                        "expression": "0.86",
                    },
                    {
                        "target": "calculation_note",
                        "expression": "format('available={a}, inbound={i}, reorder_point={r}, target={t}', a=available_qty, i=inbound_approved, r=reorder_point_internal, t=target_stock_internal)",
                    },
                ],
                "coerce_string_to_number": True,
            },
            upstream=["classify_approval"],
            intent="Populate final queue fields.",
        ),
        _step(
            "filter_actionable",
            "filter",
            {
                "predicate": "risk_before in ['Stockout Risk', 'Watch', 'Overstock Risk', 'Replenishment Blocked']",
                "keep_or_drop": "keep",
                "require_non_empty": True,
            },
            upstream=["derive_queue_fields"],
            intent="Keep actionable replenishment or freeze rows.",
        ),
        _step(
            "project_final_queue",
            "project",
            {"columns": FINAL_COLUMNS, "drop_unselected": True},
            upstream=["filter_actionable"],
            intent="Project final replenishment queue contract.",
        ),
        _step(
            "write_replenishment_queue",
            "write_csv",
            {"path": str(output_path), "columns_in_order": FINAL_COLUMNS},
            upstream=["project_final_queue"],
            produces=["replenishment_queue"],
            intent="Write replenishment queue CSV.",
        ),
    ]
    return PlannerPass2Output(
        draft_status="complete",
        steps=steps,
        entry_step_ids=["read_inventory", "read_sku_master", "read_supplier"],
        exit_step_ids=["write_replenishment_queue"],
        schema_flow=[
            SchemaFlowSnapshot(step_id=step.step_id)
            for step in steps
        ],
        planning_notes=["spare_parts_typed_node_e2e_v0"],
        planner_trace={"scenario": "spare_parts", "source": "typed_node_v0_e2e"},
    )


def resolve_plan(plan: PlannerPass2Output) -> tuple[Any, dict[str, Any]]:
    manifest, files = build_typed_node_execution_package(
        plan=plan,
        build_id="spare-parts-typed-node-e2e",
        executable_id="spare-parts-typed-node-executable",
        project_id="spare-parts",
        build_version="typed-node-v0-e2e",
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
    executable = Executable(
        id="spare-parts-typed-node-executable",
        project_id="spare-parts",
        executable_type="workflow",
    )
    resolved = ExecutableManifestResolver().resolve(
        executable=executable,
        manifest=parsed,
        runtime_profile=None,
        run_input={},
    )
    return resolved, files


def run_plan(plan: PlannerPass2Output) -> dict[str, dict[str, Any]]:
    resolved, _files = resolve_plan(plan)
    previous_outputs: dict[str, dict[str, Any]] = {}
    for resolved_step in resolved.resolved_steps:
        step_definition = resolved_step.payload["step_definition"]
        output = execute_typed_node_workload(
            step_name=resolved_step.step_name,
            step_definition=step_definition,
            previous_outputs_by_step=previous_outputs,
        )
        previous_outputs[resolved_step.step_name] = output
    return previous_outputs


def run_spare_parts_typed_node_e2e(output_path: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    plan = build_spare_parts_plan(output_path=output_path)
    outputs = run_plan(plan)
    final = outputs["write_replenishment_queue"]
    return {
        "output_path": str(output_path),
        "step_count": len(outputs),
        "final_status": final["status"],
        "final_output": final,
    }


def main() -> None:
    result = run_spare_parts_typed_node_e2e()
    print(json.dumps({
        "output_path": result["output_path"],
        "step_count": result["step_count"],
        "final_status": result["final_status"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
