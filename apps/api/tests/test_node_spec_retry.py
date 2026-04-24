"""Node spec retry — both layers.

Layer 1 (per-node): inside _spec_single_node, a parse / schema failure
on attempt N triggers a fresh LLM call on attempt N+1 with the previous
attempt's raw output + error in the prompt. Bounded by
``per_node_max_retries`` (default 1).

Layer 2 (bundle): inside specify_bundle, after every full pass the edge
contract validator runs; if issues remain, the affected nodes are
re-spec'd with neighbour spec contracts + the diagnostic as feedback.
Bounded by ``bundle_max_retries`` (default 2).

Coverage:

    R1  Layer 1: parse failure on attempt 1 → success on attempt 2
    R2  Layer 1: schema failure on attempt 1 → success on attempt 2
    R3  Layer 1: exhausted retries → NodeSpecFailure carries final raw
    R4  Layer 1: per_node_max_retries=0 disables retry (fail on first)
    R5  Layer 2: bundle retry fixes a schema_mismatch on retry 1
    R6  Layer 2: bundle retry exhaustion → bundle returned with issues
    R7  Layer 2: bundle_max_retries=0 disables bundle retry
    R8  Layer 2: respec affects ONLY mismatched nodes (others untouched)
    R9  Layer 2: trace records each attempt with before/after counts
    R10 /specify endpoint surfaces bundle_retry_attempts on 422 detail
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.schemas.node_execution_spec import (
    AcceptanceCriterion,
    BusinessRule,
    EdgeContractIssue,
    EdgeContractReport,
    ExecutionConstraints,
    FailurePolicy,
    NodeExecutionSpec,
    NodeExecutionSpecBundle,
    NodeInputContract,
    NodeInputSlot,
    NodeOutputContract,
    NodeOutputSlot,
    VerifierHints,
)
from app.schemas.problem_formulation import (
    InputClosureContract,
    InputSourceCandidate,
    OutputClosureContract,
    PlanningIntentContract,
    ProblemFormulationContract,
)
from app.schemas.requirement_compiler import CompileContextResponse
from app.schemas.solvability import SolvabilityContract
from app.schemas.workflow_native import ExecutionAdmissibilityContract
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
)
from app.services.node_spec.node_spec_service import (
    EdgeContractFailure,
    NodeSpecFailure,
    NodeSpecService,
)
from app.services.provider_runtime import ProviderResponse


# ---------------------------------------------------------------------------
# Stub adapter — returns canned responses, optionally per-node-keyed
# ---------------------------------------------------------------------------


class _StubAdapter:
    """Returns the next canned response on each .chat() call.

    When ``per_node_responses`` is provided, the stub picks the
    response based on which node_id appears in the prompt body. This is
    how Layer 2 tests simulate "this node is fixed on retry but that
    node still mismatches".
    """

    provider_key = "stub_for_test"

    def __init__(
        self,
        *,
        sequence: list = None,
        per_node_responses: dict = None,
    ) -> None:
        self.sequence = list(sequence or [])
        self.per_node_responses = {
            k: list(v) for k, v in (per_node_responses or {}).items()
        }
        self.call_log: list[dict] = []

    def chat(self, messages):
        prompt = messages[-1]["content"] if messages else ""
        node_id = _find_node_id_in_prompt(prompt)
        attempt_index = len(
            [c for c in self.call_log if c.get("node_id") == node_id]
        )
        is_layer1_retry = "RETRY — your previous attempt" in prompt
        is_layer2_retry = "bundle_retry_feedback" in prompt
        record = {
            "node_id": node_id,
            "attempt_index": attempt_index,
            "is_layer1_retry": is_layer1_retry,
            "is_layer2_retry": is_layer2_retry,
        }

        if self.per_node_responses and node_id in self.per_node_responses:
            queue = self.per_node_responses[node_id]
            if not queue:
                raise RuntimeError(f"stub exhausted for node {node_id}")
            response = queue.pop(0)
        elif self.sequence:
            response = self.sequence.pop(0)
        else:
            raise RuntimeError("stub exhausted (no sequence + no per-node)")

        record["response_preview"] = (response or "")[:60]
        self.call_log.append(record)
        return ProviderResponse(
            content=response,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            raw_metadata={},
        )


def _find_node_id_in_prompt(prompt: str) -> str | None:
    # The prompt embeds JSON with a "node_id" field for the target node.
    import re

    matches = re.findall(r'"node_id":\s*"([^"]+)"', prompt)
    # Heuristic: the FIRST node_id in the JSON payload is the target_node.
    return matches[0] if matches else None


def _stub_factory(adapter):
    def factory(overrides=None):
        return adapter, None  # (adapter, runtime_config=None)

    return factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(*, draft_node_id, semantic_type="transform", upstream=None):
    return WorkflowPlannerNodeDraft(
        draft_node_id=draft_node_id,
        step_name=draft_node_id,
        node_name=draft_node_id,
        node_semantic_type=semantic_type,
        semantic_goal=f"do {draft_node_id}",
        preferred_capability_ids=["coding_agent"],
        candidate_capability_ids=["coding_agent"],
        upstream_dependencies=upstream or [],
        planner_metadata={
            "step_name": draft_node_id,
            "bound_capability": {
                "selected_capability_id": "coding_agent",
                "candidate_ids": ["coding_agent"],
                "coverage": "full",
                "side_effect_class": "none",
                "selection_source": "test",
                "uncovered_step_kinds": [],
            },
        },
    )


def _planner(nodes, edges):
    return WorkflowPlannerOutput(
        node_drafts=nodes,
        edge_drafts=[
            WorkflowPlannerEdgeDraft(
                draft_edge_id=f"e_{src}__{dst}",
                from_node_id=src,
                to_node_id=dst,
            )
            for src, dst in edges
        ],
        entry_node_ids=[nodes[0].draft_node_id],
        exit_node_ids=[nodes[-1].draft_node_id],
    )


def _cc(input_locator: str | None, output_locator: str) -> CompileContextResponse:
    return CompileContextResponse(
        compile_context_version="t",
        requirement_id=None,
        project_id="p",
        selected_blueprint_type=None,
        graph_closure_status="graph_closed",
        selected_template_id=None,
        problem_formulation=ProblemFormulationContract(
            interpreted_problem_kind="local_dataset_task",
            target_object="local_workspace",
            problem_status="solvable",
            current_system_fit="in_scope",
            planning_intent=PlanningIntentContract(
                intent_id="i",
                label="t",
                target_object="local_workspace",
                objective_family="analysis",
            ),
            blocking_axes=[],
            review_axes=[],
            blocking_reasons=[],
            interpreted_goal="goal",
        ),
        input_closure=InputClosureContract(
            target_object="local_workspace",
            planning_intent_id="i",
            candidate_sources=(
                [
                    InputSourceCandidate(
                        candidate_id="s",
                        locator_kind="path",
                        locator_value=input_locator,
                        content_kind="dataset",
                        access_boundary="workspace",
                        acquisition_kind="direct_read",
                        confidence=1.0,
                        evidence=["t"],
                    )
                ]
                if input_locator
                else []
            ),
            resolved_source_kind="file" if input_locator else None,
            resolved_source_locator=input_locator,
            resolved_source_modality="csv" if input_locator else None,
            acquisition_path_kind="direct_read" if input_locator else "unknown",
            closure_status="input_closed",
            blocking_axes=[],
            review_axes=[],
            closure_requirements=[],
            reasoning_summary="t",
            applicability="required" if input_locator else "not_applicable",
        ),
        output_closure=OutputClosureContract(
            deliverable_form="dataset",
            delivery_target=output_locator,
            output_modality="csv",
            closure_requirements=[],
            blocking_axes=[],
            review_axes=[],
            closure_status="output_closed",
            reasoning_summary="t",
        ),
        solvability=SolvabilityContract(
            planning_intent_id="i", solvability_status="solvable"
        ),
        execution_admissibility=ExecutionAdmissibilityContract(
            admissibility_status="supported"
        ),
        capability_envelope={},
        compile_constraints={},
        candidate_work_units=[],
        compile_trace={},
    )


def _spec_payload(
    node_id: str,
    *,
    out_fields: list = None,
    in_slots: list = None,
    locator: str | None = None,
) -> dict:
    """Build a valid NodeExecutionSpec dict (LLM-shaped JSON output)."""
    inputs = []
    for slot in in_slots or []:
        inputs.append({
            "name": slot["name"],
            "kind": slot.get("kind", "upstream_output"),
            "schema_hint": slot.get("schema_hint", []),
            "source_hint": slot.get("source_hint"),
        })
    return {
        "node_id": node_id,
        "semantic_goal": f"do {node_id}",
        "input_contract": {
            "required": inputs,
            "optional": [],
        },
        "output_contract": {
            "primary_output": {
                "name": f"{node_id}_out",
                "kind": "tabular_records",
                "schema_hint": out_fields or [],
                "locator_hint": locator,
            },
            "secondary_outputs": [],
        },
        "business_rules": [],
        "acceptance_criteria": [],
        "execution_constraints": {
            "expected_runtime_class": "bounded_transform",
            "max_iterations": 1,
            "max_duration_seconds": 60,
        },
        "failure_policy": {
            "on_missing_input": "abort",
            "on_rule_violation": "abort",
            "on_empty_output": "emit_warning",
        },
        "verifier_hints": {"suggested_checks": []},
        "implementation_sketch": [],
    }


# ---------------------------------------------------------------------------
# R1, R2, R3, R4 — Layer 1 tests
# ---------------------------------------------------------------------------


def test_r1_layer1_retry_recovers_from_parse_failure(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)
    nodes = [_node(draft_node_id="n1")]
    planner = _planner(nodes, [])
    cc = _cc(None, "/tmp/out.csv")

    # First call: malformed JSON. Second call: valid.
    valid = json.dumps(_spec_payload("n1", out_fields=["a"]))
    adapter = _StubAdapter(sequence=["this is not json", valid])
    service = NodeSpecService(adapter_factory=_stub_factory(adapter))

    bundle = service.specify_bundle(
        planner_output=planner, compile_context=cc
    )
    assert len(bundle.specs) == 1
    assert bundle.specs[0].node_id == "n1"
    # Layer 1 trace shows 2 attempts.
    trace = bundle.llm_trace["per_node"][0]
    assert trace["attempts"] == 2
    # The retry's prompt MUST have included the previous failure.
    assert any(c["is_layer1_retry"] for c in adapter.call_log)


def test_r2_layer1_retry_recovers_from_schema_failure(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)
    nodes = [_node(draft_node_id="n1")]
    cc = _cc(None, "/tmp/out.csv")

    # First call: parseable JSON but missing required fields.
    bad = json.dumps({"node_id": "n1", "semantic_goal": "x"})
    valid = json.dumps(_spec_payload("n1", out_fields=["a"]))
    adapter = _StubAdapter(sequence=[bad, valid])
    service = NodeSpecService(adapter_factory=_stub_factory(adapter))

    bundle = service.specify_bundle(
        planner_output=_planner(nodes, []), compile_context=cc
    )
    assert bundle.specs[0].node_id == "n1"
    assert bundle.llm_trace["per_node"][0]["attempts"] == 2


def test_r3_layer1_exhausted_retries_raises_with_final_raw(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)
    nodes = [_node(draft_node_id="n1")]
    cc = _cc(None, "/tmp/out.csv")

    # Both attempts fail.
    adapter = _StubAdapter(sequence=["nope", "still not json"])
    service = NodeSpecService(adapter_factory=_stub_factory(adapter))
    with pytest.raises(NodeSpecFailure) as exc_info:
        service.specify_bundle(
            planner_output=_planner(nodes, []), compile_context=cc
        )
    msg = str(exc_info.value)
    assert "n1" in msg
    assert "still not json" in msg or "nope" in msg


def test_r4_per_node_retries_zero_disables_retry(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)
    nodes = [_node(draft_node_id="n1")]
    cc = _cc(None, "/tmp/out.csv")

    valid = json.dumps(_spec_payload("n1", out_fields=["a"]))
    adapter = _StubAdapter(sequence=["nope", valid])  # 2nd would succeed
    service = NodeSpecService(adapter_factory=_stub_factory(adapter))
    with pytest.raises(NodeSpecFailure):
        service.specify_bundle(
            planner_output=_planner(nodes, []),
            compile_context=cc,
            per_node_max_retries=0,
        )
    # Stub was only called once — retry was disabled.
    assert len(adapter.call_log) == 1


# ---------------------------------------------------------------------------
# R5, R6, R7, R8, R9 — Layer 2 tests
# ---------------------------------------------------------------------------


def test_r5_bundle_retry_fixes_schema_mismatch(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)
    nodes = [
        _node(draft_node_id="A"),
        _node(draft_node_id="B", upstream=["A"]),
    ]
    planner = _planner(nodes, [("A", "B")])
    cc = _cc(None, "/tmp/out.csv")

    # First pass: A emits ["x"], B expects ["y"] (mismatch).
    a_first = json.dumps(_spec_payload("A", out_fields=["x"]))
    b_first = json.dumps(
        _spec_payload(
            "B",
            in_slots=[
                {"name": "from_a", "schema_hint": ["y"], "source_hint": "A"}
            ],
        )
    )
    # Bundle retry: re-spec affected (A and B). Both align on ["x"].
    a_retry = json.dumps(_spec_payload("A", out_fields=["x"]))
    b_retry = json.dumps(
        _spec_payload(
            "B",
            in_slots=[
                {"name": "from_a", "schema_hint": ["x"], "source_hint": "A"}
            ],
        )
    )

    adapter = _StubAdapter(
        per_node_responses={"A": [a_first, a_retry], "B": [b_first, b_retry]}
    )
    service = NodeSpecService(adapter_factory=_stub_factory(adapter))

    bundle = service.specify_bundle(
        planner_output=planner, compile_context=cc
    )
    assert bundle.edge_contract_report.passed, (
        f"unexpected issues: {bundle.edge_contract_report.issues}"
    )
    attempts = bundle.llm_trace["bundle_retry_attempts"]
    assert len(attempts) == 1
    assert attempts[0]["issues_before"] == 1
    assert attempts[0]["issues_after"] == 0
    # Retry calls were marked as Layer 2 retries in the prompt.
    layer2_calls = [c for c in adapter.call_log if c["is_layer2_retry"]]
    assert layer2_calls
    assert {c["node_id"] for c in layer2_calls} == {"A", "B"}


def test_r6_bundle_retry_exhaustion_returns_bundle_with_issues(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)
    nodes = [
        _node(draft_node_id="A"),
        _node(draft_node_id="B", upstream=["A"]),
    ]
    cc = _cc(None, "/tmp/out.csv")

    # A always emits ["x"], B always expects ["y"] — never resolves.
    a = json.dumps(_spec_payload("A", out_fields=["x"]))
    b = json.dumps(
        _spec_payload(
            "B",
            in_slots=[
                {"name": "from_a", "schema_hint": ["y"], "source_hint": "A"}
            ],
        )
    )
    # Provide enough copies for: initial pass (1 each) + 2 bundle retries
    # × 2 affected nodes each = 5 calls per node max.
    adapter = _StubAdapter(per_node_responses={"A": [a] * 5, "B": [b] * 5})
    service = NodeSpecService(adapter_factory=_stub_factory(adapter))

    bundle = service.specify_bundle(
        planner_output=_planner(nodes, [("A", "B")]),
        compile_context=cc,
        bundle_max_retries=2,
    )
    assert not bundle.edge_contract_report.passed
    attempts = bundle.llm_trace["bundle_retry_attempts"]
    assert len(attempts) == 2
    for a in attempts:
        assert a["issues_after"] == 1


def test_r7_bundle_max_retries_zero_skips_bundle_retry(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)
    nodes = [
        _node(draft_node_id="A"),
        _node(draft_node_id="B", upstream=["A"]),
    ]
    cc = _cc(None, "/tmp/out.csv")

    a = json.dumps(_spec_payload("A", out_fields=["x"]))
    b = json.dumps(
        _spec_payload(
            "B",
            in_slots=[
                {"name": "from_a", "schema_hint": ["y"], "source_hint": "A"}
            ],
        )
    )
    adapter = _StubAdapter(per_node_responses={"A": [a], "B": [b]})
    service = NodeSpecService(adapter_factory=_stub_factory(adapter))

    bundle = service.specify_bundle(
        planner_output=_planner(nodes, [("A", "B")]),
        compile_context=cc,
        bundle_max_retries=0,
    )
    assert not bundle.edge_contract_report.passed
    assert bundle.llm_trace["bundle_retry_attempts"] == []
    # Stub only called once per node (initial pass).
    assert len(adapter.call_log) == 2


def test_r8_bundle_retry_only_respec_affected_nodes(monkeypatch):
    """Three-node chain A → B → C, with mismatch only on B → C edge.
    Bundle retry must not re-spec A (which is fine).
    """
    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)
    nodes = [
        _node(draft_node_id="A"),
        _node(draft_node_id="B", upstream=["A"]),
        _node(draft_node_id="C", upstream=["B"]),
    ]
    cc = _cc(None, "/tmp/out.csv")

    # A → B is fine: A emits ["a"], B reads ["a"].
    # B → C mismatches: B emits ["b1"], C expects ["b2"].
    # On retry, B emits ["b2"] to align with C.
    a = json.dumps(_spec_payload("A", out_fields=["a"]))
    b_first = json.dumps(
        _spec_payload(
            "B",
            out_fields=["b1"],
            in_slots=[
                {"name": "from_a", "schema_hint": ["a"], "source_hint": "A"}
            ],
        )
    )
    b_retry = json.dumps(
        _spec_payload(
            "B",
            out_fields=["b2"],
            in_slots=[
                {"name": "from_a", "schema_hint": ["a"], "source_hint": "A"}
            ],
        )
    )
    c = json.dumps(
        _spec_payload(
            "C",
            in_slots=[
                {"name": "from_b", "schema_hint": ["b2"], "source_hint": "B"}
            ],
        )
    )
    adapter = _StubAdapter(
        per_node_responses={
            "A": [a, a],  # one extra in case retry hits A
            "B": [b_first, b_retry],
            "C": [c, c],  # affected as dst of mismatch
        }
    )
    service = NodeSpecService(adapter_factory=_stub_factory(adapter))

    bundle = service.specify_bundle(
        planner_output=_planner(nodes, [("A", "B"), ("B", "C")]),
        compile_context=cc,
    )
    assert bundle.edge_contract_report.passed
    # A should NOT have been re-spec'd: it never appeared in any
    # affected-node list.
    a_calls = [c for c in adapter.call_log if c["node_id"] == "A"]
    assert len(a_calls) == 1, f"A was re-spec'd unnecessarily: {a_calls}"


def test_r9_bundle_retry_trace_records_progress(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)
    nodes = [
        _node(draft_node_id="A"),
        _node(draft_node_id="B", upstream=["A"]),
    ]
    cc = _cc(None, "/tmp/out.csv")
    a_first = json.dumps(_spec_payload("A", out_fields=["x"]))
    b_first = json.dumps(
        _spec_payload(
            "B",
            in_slots=[
                {"name": "from_a", "schema_hint": ["y"], "source_hint": "A"}
            ],
        )
    )
    a_retry = json.dumps(_spec_payload("A", out_fields=["x", "y"]))
    b_retry = json.dumps(
        _spec_payload(
            "B",
            in_slots=[
                {"name": "from_a", "schema_hint": ["y"], "source_hint": "A"}
            ],
        )
    )
    adapter = _StubAdapter(
        per_node_responses={"A": [a_first, a_retry], "B": [b_first, b_retry]}
    )
    service = NodeSpecService(adapter_factory=_stub_factory(adapter))

    bundle = service.specify_bundle(
        planner_output=_planner(nodes, [("A", "B")]),
        compile_context=cc,
    )
    attempts = bundle.llm_trace["bundle_retry_attempts"]
    assert attempts[0]["issues_before"] == 1
    assert attempts[0]["issues_after"] == 0
    assert set(attempts[0]["respec_node_ids"]) == {"A", "B"}
    assert attempts[0]["per_attempt_calls"] == 2
    # Defaults exposed on trace too.
    assert bundle.llm_trace["bundle_max_retries"] == 2
    assert bundle.llm_trace["per_node_max_retries"] == 1


# ---------------------------------------------------------------------------
# R10 — endpoint surfaces retry trace on 422
# ---------------------------------------------------------------------------


def test_r10_specify_endpoint_surfaces_retry_trace_on_422(client, tmp_path: Path, monkeypatch):
    """When edge_contract retries fail, the 422 detail should include
    bundle_retry_attempts so callers see the validator was working.
    """
    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)
    from app.core.database import get_db
    from app.db.models import FrontChainState
    from app.main import app
    from app.services.node_spec import session_orchestration as orch

    # Project + session.
    proj = client.post(
        "/api/v1/projects", json={"name": "retry-trace", "description": ""}
    ).json()
    sess = client.post(
        "/api/v1/front-chain/sessions", json={"project_id": proj["id"]}
    ).json()
    project_id = proj["id"]
    session_id = sess["id"]

    # Seed state with planner_output (real edges between A and B).
    nodes = [_node(draft_node_id="A"), _node(draft_node_id="B", upstream=["A"])]
    planner_output = _planner(nodes, [("A", "B")])
    cc = _cc(None, "/tmp/out.csv")

    db_dep = app.dependency_overrides[get_db]
    gen = db_dep()
    db = next(gen)
    state = FrontChainState(
        project_id=project_id,
        session_id=session_id,
        raw_intent="seed",
        problem_formulation_json=cc.problem_formulation.model_dump(mode="json"),
        input_closure_json=cc.input_closure.model_dump(mode="json"),
        output_closure_json=cc.output_closure.model_dump(mode="json"),
        merged_gaps_json={},
        top_questions_json=[],
        llm_trace_json={"call_count": 0, "llm_mode": "mock"},
        interpreter_version="t",
        validator_version="t",
        compile_context_json=cc.model_dump(mode="json"),
        compile_version="t",
        planner_output_json=planner_output.model_dump(mode="json"),
        planner_version="workflow_planner_v1",
    )
    db.add(state)
    db.commit()
    try:
        next(gen)
    except StopIteration:
        pass

    # Inject a service that always returns mismatched specs (no fix on retry).
    class _AlwaysMismatchService:
        def specify_bundle(self, *, planner_output, compile_context, **kwargs):
            specs = [
                NodeExecutionSpec.model_validate(_spec_payload("A", out_fields=["x"])),
                NodeExecutionSpec.model_validate(
                    _spec_payload(
                        "B",
                        in_slots=[
                            {"name": "from_a", "schema_hint": ["y"], "source_hint": "A"}
                        ],
                    )
                ),
            ]
            from app.services.node_spec.edge_contract_validator import (
                validate_edge_contracts,
            )

            report = validate_edge_contracts(
                planner_output=planner_output,
                specs=specs,
                skipped_node_ids=[],
            )
            return NodeExecutionSpecBundle(
                specs=specs,
                skipped_node_ids=[],
                profiling_summary={},
                edge_contract_report=report,
                llm_trace={
                    "call_count": 4,
                    "llm_mode": "real",
                    "per_node_max_retries": 1,
                    "bundle_max_retries": 2,
                    "bundle_retry_attempts": [
                        {
                            "attempt_index": 0,
                            "respec_node_ids": ["A", "B"],
                            "issues_before": 1,
                            "issues_after": 1,
                            "per_attempt_calls": 2,
                        },
                        {
                            "attempt_index": 1,
                            "respec_node_ids": ["A", "B"],
                            "issues_before": 1,
                            "issues_after": 1,
                            "per_attempt_calls": 2,
                        },
                    ],
                },
            )

    original_specify = orch.specify_for_session

    def _patched(session_id, *, db, persist=True, service=None):
        return original_specify(
            session_id, db=db, persist=persist, service=_AlwaysMismatchService()
        )

    monkeypatch.setattr("app.api.routes.front_chain.specify_for_session", _patched)

    resp = client.post(f"/api/v1/front-chain/sessions/{session_id}/specify")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "edge_contract_failed"
    assert len(detail["bundle_retry_attempts"]) == 2
    assert detail["bundle_max_retries"] == 2
    assert detail["per_node_max_retries"] == 1
