"""Task 6 — contract verifier (unit + integration).

The verifier reads each declared ``ExpectedOutput.required_fields``
after the sandbox returns and rejects iterations whose produced file
header / keys don't match the strict list. It feeds violations back
to the LLM as structured ``contract_violations`` so the next
iteration sees exactly what to fix.

Coverage:

    V1  CSV with all required fields → clean
    V2  CSV missing a required field → missing_fields violation
    V3  CSV with extra field + forbid_extra_fields=True → unexpected
    V4  CSV with extra field + forbid_extra_fields=False → clean
    V5  Declared file does not exist → file_not_found
    V6  JSON top-level dict → keys validated
    V7  JSON array of dicts → first row's keys validated
    V8  Unsupported file format → format_unsupported (no crash)
    V9  ExpectedOutput without required_fields → skipped

    S1  Iteration loop: clean run → ok=True, contract_validation clean
    S2  Iteration loop: field rename → contract violation, retry
    S3  Iteration loop: exhaustion → failure_kind='contract_violation'
    S4  Retry prompt body carries contract_violations block

    E1  enricher emits required_fields on META_EXPECTED_OUTPUTS
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pytest

from app.schemas.coding_agent import (
    CodingAgentConstraints,
    CodingAgentRuntime,
    ContractFieldViolation,
    ContractValidation,
    ExpectedOutput,
    FailureMode,
    InputPolicy,
    OutputPolicy,
    TaskContract,
)
from app.services.coding_agent.contract_verifier import validate_contract
from app.services.coding_agent.prompt_builder import build_messages
from app.services.coding_agent.service import CodingAgentService
from app.services.provider_runtime import ProviderResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


def _eo(
    locator: Path,
    required: list[str] | None = None,
    *,
    forbid_extra: bool = False,
) -> ExpectedOutput:
    return ExpectedOutput(
        locator=str(locator),
        kind="data.derived",
        schema_hint=", ".join(required) if required else None,
        required_fields=required,
        forbid_extra_fields=forbid_extra,
    )


# ---------------------------------------------------------------------------
# V — verifier unit tests
# ---------------------------------------------------------------------------


def test_v1_csv_all_required_fields_clean(tmp_path: Path) -> None:
    out = tmp_path / "out.csv"
    _write_csv(out, ["sku_id", "qty"], [["P-001", 5]])
    res = validate_contract(
        expected_outputs=[_eo(out, required=["sku_id", "qty"])]
    )
    assert res.is_clean
    assert res.checked_files == 1
    assert res.skipped_files == 0


def test_v2_csv_missing_required_field(tmp_path: Path) -> None:
    out = tmp_path / "out.csv"
    _write_csv(out, ["sku_id", "qty"], [["P-001", 5]])
    res = validate_contract(
        expected_outputs=[_eo(out, required=["sku_id", "part_name", "qty"])]
    )
    assert not res.is_clean
    assert len(res.violations) == 1
    v = res.violations[0]
    assert v.error_type == "missing_fields"
    assert v.missing_fields == ["part_name"]
    assert set(v.actual_schema) == {"sku_id", "qty"}


def test_v3_csv_extra_field_forbid_extra(tmp_path: Path) -> None:
    out = tmp_path / "out.csv"
    _write_csv(out, ["sku_id", "qty", "extra_col"], [["P-001", 5, "x"]])
    res = validate_contract(
        expected_outputs=[
            _eo(out, required=["sku_id", "qty"], forbid_extra=True)
        ]
    )
    assert not res.is_clean
    v = res.violations[0]
    assert v.error_type == "unexpected_fields"
    assert v.unexpected_fields == ["extra_col"]


def test_v4_csv_extra_field_allow_extra(tmp_path: Path) -> None:
    out = tmp_path / "out.csv"
    _write_csv(out, ["sku_id", "qty", "extra_col"], [["P-001", 5, "x"]])
    res = validate_contract(
        expected_outputs=[_eo(out, required=["sku_id", "qty"])]
    )
    # forbid_extra=False (default) → extras are fine.
    assert res.is_clean


def test_v5_missing_file(tmp_path: Path) -> None:
    out = tmp_path / "missing.csv"
    res = validate_contract(
        expected_outputs=[_eo(out, required=["sku_id"])]
    )
    assert not res.is_clean
    v = res.violations[0]
    assert v.error_type == "file_not_found"


def test_v6_json_dict_keys(tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    out.write_text(json.dumps({"sku_id": "P-001", "qty": 5}))
    res = validate_contract(
        expected_outputs=[_eo(out, required=["sku_id", "qty"])]
    )
    assert res.is_clean


def test_v7_json_array_of_dicts(tmp_path: Path) -> None:
    out = tmp_path / "rows.json"
    out.write_text(json.dumps([{"sku": "P-001", "qty": 5}, {"sku": "P-002", "qty": 8}]))
    res = validate_contract(
        expected_outputs=[_eo(out, required=["sku", "qty"])]
    )
    assert res.is_clean

    res2 = validate_contract(
        expected_outputs=[_eo(out, required=["sku_id", "qty"])]
    )
    assert not res2.is_clean
    assert res2.violations[0].missing_fields == ["sku_id"]


def test_v8_unsupported_format(tmp_path: Path) -> None:
    out = tmp_path / "data.parquet"
    out.write_bytes(b"PAR1\x00\x00")  # not real parquet, just a binary
    res = validate_contract(
        expected_outputs=[_eo(out, required=["sku_id"])]
    )
    assert not res.is_clean
    assert res.violations[0].error_type == "format_unsupported"


def test_v8b_unknown_extension_with_json_content_sniffs(tmp_path: Path) -> None:
    """Chained intermediate ``.out`` files have no extension hint;
    verifier should sniff content and validate as JSON when it parses."""
    out = tmp_path / "step_00.out"
    out.write_text(json.dumps({"sku_id": "P-001", "qty": 5}))
    res = validate_contract(
        expected_outputs=[_eo(out, required=["sku_id", "qty"])]
    )
    assert res.is_clean


def test_v8c_unknown_extension_with_csv_content_sniffs(tmp_path: Path) -> None:
    out = tmp_path / "blob.out"
    out.write_text("sku_id,qty\nP-001,5\n")
    res = validate_contract(
        expected_outputs=[_eo(out, required=["sku_id", "qty"])]
    )
    assert res.is_clean


def test_v9_no_required_fields_skipped(tmp_path: Path) -> None:
    out = tmp_path / "anything.csv"
    out.write_text("a,b,c\n1,2,3\n")
    res = validate_contract(
        expected_outputs=[
            ExpectedOutput(
                locator=str(out),
                kind="data.derived",
                schema_hint=None,
                required_fields=None,
            )
        ]
    )
    assert res.is_clean
    assert res.checked_files == 0
    assert res.skipped_files == 1


# ---------------------------------------------------------------------------
# S — service / iteration-loop integration
# ---------------------------------------------------------------------------


class _StubAdapter:
    provider_key = "stub"

    def __init__(self, *, scripts: list[dict]):
        self.scripts = list(scripts)
        self.calls: list[dict] = []

    def chat_with_tools(self, *, messages, tools):
        self.calls.append({"messages": messages, "tools": tools})
        if not self.scripts:
            raise RuntimeError("stub script queue exhausted")
        item = self.scripts.pop(0)
        # Provider-side tool-call shape.
        return ProviderResponse(
            content=None,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            raw_metadata={
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "write_and_run",
                            "arguments": json.dumps(
                                {
                                    "language": item["language"],
                                    "script_source": item["script_source"],
                                    "rationale": item.get("rationale", "test"),
                                }
                            ),
                        },
                    }
                ],
            },
        )


def _adapter_factory_for(adapter):
    def _factory():
        return adapter, None
    return _factory


def _contract_for(workspace: Path, expected: ExpectedOutput, *, max_iter: int = 3):
    return TaskContract(
        objective="Write a CSV with declared columns.",
        workspace_dir=str(workspace),
        input_evidence=[],
        input_policy=InputPolicy(),
        expected_outputs=[expected],
        output_policy=OutputPolicy(),
        constraints=CodingAgentConstraints(language="python"),
        failure_mode=FailureMode(),
        runtime=CodingAgentRuntime(
            timeout_seconds=10,
            memory_mb=128,
            allow_network=False,
            max_iterations=max_iter,
        ),
    )


def _script_writes_csv(out_path: Path, header: list[str]) -> str:
    cols_repr = json.dumps(header)
    return (
        "import csv\n"
        f"PATH = {str(out_path)!r}\n"
        f"COLS = {cols_repr}\n"
        "with open(PATH, 'w', newline='') as f:\n"
        "    w = csv.writer(f)\n"
        "    w.writerow(COLS)\n"
        "    w.writerow(['x'] * len(COLS))\n"
    )


def _provider_response_with_tool_call(provider_response):
    """The service unwraps response.response.tool_calls and also dict
    form. This helper makes a ProviderResponse expose tool_calls via
    raw_metadata (which the service's _extract_tool_calls handles via
    duck typing on dict form)."""
    return provider_response


@pytest.mark.skipif(
    True,
    reason="real sandbox execution; covered by S1/S2 below using a forked process",
)
def test_s_placeholder():
    pass


def test_s1_clean_iteration_passes_contract_check(tmp_path: Path, monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)

    out = tmp_path / "out.csv"
    eo = _eo(out, required=["sku_id", "qty"])
    contract = _contract_for(tmp_path, eo, max_iter=2)

    adapter = _StubAdapter(
        scripts=[
            {
                "language": "python",
                "script_source": _script_writes_csv(out, ["sku_id", "qty"]),
                "rationale": "Write the declared columns.",
            }
        ]
    )
    # The service uses chat_with_tools and extracts tool_calls from
    # the response. Our stub returns ProviderResponse with raw_metadata
    # carrying tool_calls; _extract_tool_calls handles that via the
    # dict-shape branch. Service unwraps `.response`.
    service = CodingAgentService(adapter_factory=_adapter_factory_for(adapter))
    # The service's _extract_tool_calls only inspects dict / .tool_calls
    # — wrap the ProviderResponse so its raw_metadata is the response.
    # Simpler path: monkeypatch the unwrap step. Instead, monkeypatch
    # _call_llm to read scripts directly.
    from app.services.coding_agent import service as svc_mod

    def _patched_call_llm(self, *, messages):
        return svc_mod._ParsedToolCall(
            language="python",
            script_source=_script_writes_csv(out, ["sku_id", "qty"]),
            run_args="",
            rationale="Write the declared columns.",
        )

    monkeypatch.setattr(svc_mod.CodingAgentService, "_call_llm", _patched_call_llm)
    result = service.execute(contract)
    assert result.ok is True, f"unexpected failure: {result.failure_kind}"
    assert result.failure_kind == "ok"
    assert len(result.iterations) == 1
    it = result.iterations[0]
    assert it.contract_validation.is_clean
    assert it.output_policy_violation.is_clean


def test_s2_field_rename_triggers_contract_violation_and_retry(
    tmp_path: Path, monkeypatch
) -> None:
    from app.core.config import settings
    from app.services.coding_agent import service as svc_mod

    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)

    out = tmp_path / "out.csv"
    eo = _eo(out, required=["sku_id", "part_name", "qty"])
    contract = _contract_for(tmp_path, eo, max_iter=3)

    # Iteration 1: agent renames part_name -> sku_name (violation).
    # Iteration 2: agent uses correct field names (passes).
    scripts = [
        _script_writes_csv(out, ["sku_id", "sku_name", "qty"]),
        _script_writes_csv(out, ["sku_id", "part_name", "qty"]),
    ]
    state = {"i": 0}

    def _patched_call_llm(self, *, messages):
        idx = state["i"]
        state["i"] += 1
        return svc_mod._ParsedToolCall(
            language="python",
            script_source=scripts[idx],
            run_args="",
            rationale=f"attempt-{idx}",
        )

    monkeypatch.setattr(svc_mod.CodingAgentService, "_call_llm", _patched_call_llm)
    service = CodingAgentService()
    result = service.execute(contract)
    assert result.ok is True
    assert len(result.iterations) == 2
    # First iter: contract_validation has missing_fields=['part_name'].
    cv1 = result.iterations[0].contract_validation
    assert not cv1.is_clean
    v1 = cv1.violations[0]
    assert v1.error_type == "missing_fields"
    assert v1.missing_fields == ["part_name"]
    # Second iter: clean.
    assert result.iterations[1].contract_validation.is_clean


def test_s3_exhaustion_classifies_as_contract_violation(
    tmp_path: Path, monkeypatch
) -> None:
    from app.core.config import settings
    from app.services.coding_agent import service as svc_mod

    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)

    out = tmp_path / "out.csv"
    eo = _eo(out, required=["sku_id", "part_name"])
    contract = _contract_for(tmp_path, eo, max_iter=2)

    # Both iterations rename — never converges.
    bad_script = _script_writes_csv(out, ["sku_id", "sku_name"])

    def _patched_call_llm(self, *, messages):
        return svc_mod._ParsedToolCall(
            language="python",
            script_source=bad_script,
            run_args="",
            rationale="never fixes",
        )

    monkeypatch.setattr(svc_mod.CodingAgentService, "_call_llm", _patched_call_llm)
    service = CodingAgentService()
    result = service.execute(contract)
    assert result.ok is False
    assert result.failure_kind == "contract_violation"
    assert len(result.iterations) == 2
    for it in result.iterations:
        assert not it.contract_validation.is_clean


def test_s4_retry_prompt_carries_contract_violations(tmp_path: Path) -> None:
    from app.schemas.coding_agent import (
        CodingAgentIteration,
        ContractFieldViolation,
        ContractValidation,
        OutputPolicyViolation,
    )

    iteration = CodingAgentIteration(
        iteration_index=1,
        language="python",
        script_source="...",
        rationale="r",
        exit_code=0,
        timed_out=False,
        stdout_tail="",
        stderr_tail="",
        produced_files=[],
        output_policy_violation=OutputPolicyViolation(),
        contract_validation=ContractValidation(
            checked_files=1,
            violations=[
                ContractFieldViolation(
                    locator=str(tmp_path / "out.csv"),
                    error_type="missing_fields",
                    expected_schema=["sku_id", "part_name"],
                    actual_schema=["sku_id", "sku_name"],
                    missing_fields=["part_name"],
                    unexpected_fields=[],
                )
            ],
        ),
        wall_time_ms=10,
        sandbox_mode="subprocess",
    )
    contract = _contract_for(
        tmp_path, _eo(tmp_path / "out.csv", required=["sku_id", "part_name"])
    )
    messages = build_messages(contract, previous_iteration=iteration)
    # The retry-feedback user message — anchor on the structured
    # block name. After 7.6D, this message is now PREFIXED with a
    # CONTRACT REPAIR MODE header before the ``previous_iteration:``
    # block, so we anchor on either the header OR the block name.
    feedback = next(
        m["content"]
        for m in messages
        if m["role"] == "user"
        and (
            "CONTRACT REPAIR MODE" in m["content"]
            or m["content"].lstrip().startswith("previous_iteration:")
        )
    )
    assert "contract_violations" in feedback
    assert "missing_fields" in feedback
    assert "[\"part_name\"]" in feedback
    assert "REPAIR DIRECTIVE" in feedback
    assert "Do NOT rename" in feedback
    # 7.6D: the new header forbids exploration on retry.
    assert "CONTRACT REPAIR MODE" in feedback


# ---------------------------------------------------------------------------
# E — enricher integration
# ---------------------------------------------------------------------------


def test_e1_enricher_emits_required_fields_on_meta_expected_outputs() -> None:
    from app.schemas.node_execution_spec import (
        AcceptanceCriterion,
        BusinessRule,
        ExecutionConstraints,
        FailurePolicy,
        NodeExecutionSpec,
        NodeInputContract,
        NodeOutputContract,
        NodeOutputSlot,
    )
    from app.schemas.workflow_planner import WorkflowPlannerNodeDraft
    from app.services.workflow_planner.runtime_field_enricher import (
        META_EXPECTED_OUTPUTS,
        apply_spec_to_node,
    )

    spec = NodeExecutionSpec(
        node_id="n1",
        semantic_goal="produce the table",
        input_contract=NodeInputContract(),
        output_contract=NodeOutputContract(
            primary_output=NodeOutputSlot(
                name="risk_table",
                kind="tabular_records",
                schema_hint=["sku_id", "part_name", "qty"],
                locator_hint="/tmp/out.csv",
            ),
        ),
        business_rules=[],
        acceptance_criteria=[],
        execution_constraints=ExecutionConstraints(),
        failure_policy=FailurePolicy(),
    )
    node = WorkflowPlannerNodeDraft(
        draft_node_id="n1",
        step_name="n1",
        node_name="n1",
        node_semantic_type="transform",
        semantic_goal="produce the table",
        preferred_capability_ids=["coding_agent"],
        candidate_capability_ids=["coding_agent"],
        upstream_dependencies=[],
        planner_metadata={},
    )
    apply_spec_to_node(node=node, spec=spec)
    expected = node.planner_metadata[META_EXPECTED_OUTPUTS]
    entry = expected[0]
    assert entry["required_fields"] == ["sku_id", "part_name", "qty"]
    assert entry["schema_hint"] == "sku_id, part_name, qty"
    assert entry["locator"] == "/tmp/out.csv"
