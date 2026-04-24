"""Task 4 tests — contract-bound spec + build.

Coverage:

    T1  Bundle feedback (per-issue dossier) carries upstream output_contract
        AND downstream input_contract for the offending edge.
    T2  Retry prompt body includes the anti-bypass rule visible to the LLM.
    T3  Initial system prompt explicitly forbids bypass.
    T4  Bypass detector flags a downstream slot that reads raw CSV when
        upstream publishes the same data.
    T5  Bypass NOT flagged when the dst is itself an acquisition node.
    T6  Bundle retry exhaustion escalates the surviving issue's
        classification to ``topology_repair_required``.
    T7  /specify endpoint 422 detail carries classification_counts and
        the topology_repair_required boolean.
    T8  Enricher's system_prompt includes contract-bound rule when the
        spec has upstream-bound inputs.
    T9  Enricher's system_prompt omits the upstream-extra-warning when
        the node is acquisition-only (no upstream-bound inputs).
"""
from __future__ import annotations

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
from app.services.node_spec.issue_classifier import (
    classify_issues,
    escalate_persistent_issues,
    issue_keys,
)
from app.services.node_spec.node_spec_service import NodeSpecService
from app.services.provider_runtime import ProviderResponse


# ---------------------------------------------------------------------------
# Reusable helpers (kept local to avoid coupling test files)
# ---------------------------------------------------------------------------


def _spec(
    node_id: str,
    *,
    out_name: str = "out",
    out_fields: list = None,
    in_slots: list = None,
    out_locator: str | None = None,
    out_kind: str = "tabular_records",
) -> NodeExecutionSpec:
    inputs = []
    for slot in in_slots or []:
        inputs.append(
            NodeInputSlot(
                name=slot["name"],
                kind=slot.get("kind", "upstream_output"),
                schema_hint=slot.get("schema_hint", []),
                source_hint=slot.get("source_hint"),
            )
        )
    return NodeExecutionSpec(
        node_id=node_id,
        semantic_goal=f"do {node_id}",
        input_contract=NodeInputContract(required=inputs, optional=[]),
        output_contract=NodeOutputContract(
            primary_output=NodeOutputSlot(
                name=out_name,
                kind=out_kind,
                schema_hint=out_fields or [],
                locator_hint=out_locator,
            ),
            secondary_outputs=[],
        ),
        business_rules=[],
        acceptance_criteria=[],
        execution_constraints=ExecutionConstraints(),
        failure_policy=FailurePolicy(),
        verifier_hints=VerifierHints(),
        implementation_sketch=[],
    )


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
    node_id: str, *, out_fields=None, in_slots=None, out_kind="tabular_records"
) -> dict:
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
        "input_contract": {"required": inputs, "optional": []},
        "output_contract": {
            "primary_output": {
                "name": f"{node_id}_out",
                "kind": out_kind,
                "schema_hint": out_fields or [],
                "locator_hint": None,
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


class _StubAdapter:
    provider_key = "stub"

    def __init__(self, *, per_node_responses):
        self.per_node_responses = {k: list(v) for k, v in per_node_responses.items()}
        self.captured_prompts: list[str] = []

    def chat(self, messages):
        prompt = messages[-1]["content"] if messages else ""
        self.captured_prompts.append(prompt)
        # First node_id in JSON body is the target_node.
        import re

        matches = re.findall(r'"node_id":\s*"([^"]+)"', prompt)
        node_id = matches[0] if matches else None
        queue = self.per_node_responses.get(node_id, [])
        if not queue:
            raise RuntimeError(f"stub exhausted for node {node_id}")
        return ProviderResponse(
            content=queue.pop(0),
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            raw_metadata={},
        )


def _stub_factory(adapter):
    def factory(overrides=None):
        return adapter, None

    return factory


# ---------------------------------------------------------------------------
# T1 + T2 — bundle feedback structure + anti-bypass rule in retry prompt
# ---------------------------------------------------------------------------


def test_t1_bundle_feedback_carries_upstream_output_and_downstream_input_contract(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)
    nodes = [_node(draft_node_id="A"), _node(draft_node_id="B", upstream=["A"])]
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
    # Bundle retry exhausts at 1 initial + 2 retries = 3 calls per node.
    # The test only cares about the prompt CONTENT during retries.
    adapter = _StubAdapter(
        per_node_responses={"A": [a_first] * 3, "B": [b_first] * 3}
    )
    service = NodeSpecService(adapter_factory=_stub_factory(adapter))
    service.specify_bundle(
        planner_output=_planner(nodes, [("A", "B")]),
        compile_context=cc,
    )

    # Find the prompt where B is the target (re-spec) — that's where we
    # expect the upstream_output_contract to contain A's emitted field.
    retry_prompts = [p for p in adapter.captured_prompts if "bundle_retry_feedback" in p]
    assert retry_prompts, "no bundle_retry_feedback prompt was sent"
    import re

    b_retry_prompt = next(
        (
            p
            for p in retry_prompts
            if re.search(r'"target_node":\s*\{\s*"node_id":\s*"B"', p)
        ),
        None,
    )
    assert b_retry_prompt is not None, "B was not re-spec'd in bundle retry"

    # The prompt body is plain text + JSON; we can grep for required keys.
    assert "per_issue" in b_retry_prompt
    assert "upstream_output_contract" in b_retry_prompt
    assert "downstream_input_contract" in b_retry_prompt
    assert "validator_detail" in b_retry_prompt
    assert "repair_instruction" in b_retry_prompt
    # And the actual upstream A's emitted field must show up in the
    # upstream_output_contract section (A emitted ["x"]).
    assert '"x"' in b_retry_prompt


def test_t2_retry_prompt_includes_anti_bypass_rule(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)
    nodes = [_node(draft_node_id="A"), _node(draft_node_id="B", upstream=["A"])]
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
    adapter = _StubAdapter(
        per_node_responses={"A": [a] * 3, "B": [b] * 3}
    )
    service = NodeSpecService(adapter_factory=_stub_factory(adapter))
    service.specify_bundle(
        planner_output=_planner(nodes, [("A", "B")]),
        compile_context=cc,
    )
    retry_prompts = [p for p in adapter.captured_prompts if "bundle_retry_feedback" in p]
    assert retry_prompts
    # Anti-bypass rule appears in the feedback block.
    assert any("anti_bypass_rule" in p for p in retry_prompts)
    assert any(
        "MUST be consumed via source_hint" in p for p in retry_prompts
    )


def test_t3_initial_system_prompt_forbids_bypass():
    """The system prompt itself (regardless of retry state) should
    establish the contract-bound consumption rule."""
    from app.services.node_spec.node_spec_service import _SYSTEM_PROMPT

    assert "CONTRACT-BOUND CONSUMPTION" in _SYSTEM_PROMPT
    assert "MUST NOT bypass" in _SYSTEM_PROMPT
    assert "source_hint" in _SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# T4 + T5 — bypass detector
# ---------------------------------------------------------------------------


def test_t4_bypass_detector_flags_raw_csv_consumption_when_upstream_publishes_data():
    src = _spec("A", out_name="parsed_table", out_fields=["sku", "qty"])
    # B claims to read /tmp/raw.csv directly, but src already publishes
    # sku / qty fields. That's a bypass.
    dst = _spec(
        "B",
        in_slots=[
            {
                "name": "raw_table",
                "kind": "tabular_records",
                "schema_hint": ["sku", "qty"],
                "source_hint": "/tmp/raw.csv",
            }
        ],
    )
    issue = EdgeContractIssue(
        edge_id="e1",
        from_node_id="A",
        to_node_id="B",
        kind="schema_mismatch",
        detail="dst expects fields not emitted by src",
        missing_fields=["sku"],
    )
    classified = classify_issues(
        issues=[issue],
        specs=[src, dst],
        semantic_types_by_node_id={"A": "acquire", "B": "transform"},
    )
    assert classified[0].classification == "contract_alignment_repairable"
    assert classified[0].bypass_signal is True


def test_t5_bypass_detector_does_not_flag_when_dst_is_acquisition_node():
    src = _spec("A", out_name="meta", out_fields=["sku"])
    # B is itself an acquisition node — direct file access is its job.
    dst = _spec(
        "B",
        in_slots=[
            {
                "name": "raw_table",
                "kind": "tabular_records",
                "schema_hint": ["sku"],
                "source_hint": "/tmp/raw.csv",
            }
        ],
    )
    issue = EdgeContractIssue(
        edge_id="e1",
        from_node_id="A",
        to_node_id="B",
        kind="schema_mismatch",
        detail="d",
    )
    classified = classify_issues(
        issues=[issue],
        specs=[src, dst],
        semantic_types_by_node_id={"A": "transform", "B": "acquire"},
    )
    assert classified[0].bypass_signal is False


# ---------------------------------------------------------------------------
# T6 — escalation to topology_repair_required after retry exhaustion
# ---------------------------------------------------------------------------


def test_t6_persistent_issue_escalates_to_topology_repair_required(monkeypatch):
    monkeypatch.setattr(settings, "llm_mode", "real", raising=False)
    nodes = [_node(draft_node_id="A"), _node(draft_node_id="B", upstream=["A"])]
    cc = _cc(None, "/tmp/out.csv")

    # A always emits ["x"], B always expects ["y"] → never resolves.
    a = json.dumps(_spec_payload("A", out_fields=["x"]))
    b = json.dumps(
        _spec_payload(
            "B",
            in_slots=[
                {"name": "from_a", "schema_hint": ["y"], "source_hint": "A"}
            ],
        )
    )
    # Initial pass + 2 bundle retries × 2 nodes = 5 calls per node max.
    adapter = _StubAdapter(per_node_responses={"A": [a] * 5, "B": [b] * 5})
    service = NodeSpecService(adapter_factory=_stub_factory(adapter))
    bundle = service.specify_bundle(
        planner_output=_planner(nodes, [("A", "B")]),
        compile_context=cc,
        bundle_max_retries=2,
    )
    # Surviving issue must be tagged as topology_repair_required.
    assert bundle.edge_contract_report.issues
    issue = bundle.edge_contract_report.issues[0]
    assert issue.classification == "topology_repair_required", (
        f"expected topology_repair_required, got {issue.classification}"
    )


def test_t6b_escalate_persistent_issues_helper_only_escalates_seen_keys():
    """Issues that newly appeared on the LAST retry should NOT be
    escalated to topology_repair_required — they could still be
    contract-alignment-repairable in a future iteration."""
    seen = {("A", "B", "schema_mismatch")}
    persistent = EdgeContractIssue(
        edge_id="e1",
        from_node_id="A",
        to_node_id="B",
        kind="schema_mismatch",
        detail="x",
    )
    new = EdgeContractIssue(
        edge_id="e2",
        from_node_id="C",
        to_node_id="D",
        kind="schema_mismatch",
        detail="y",
    )
    out = escalate_persistent_issues(
        final_issues=[persistent, new], initial_issue_keys=seen
    )
    assert out[0].classification == "topology_repair_required"
    # New issue keeps default classification.
    assert out[1].classification == "contract_alignment_repairable"


# ---------------------------------------------------------------------------
# T7 — endpoint 422 detail surfaces classification_counts
# ---------------------------------------------------------------------------


def test_t7_specify_endpoint_422_surfaces_classification_counts(client, tmp_path: Path, monkeypatch):
    """When edge_contract retries fail, the 422 detail should include
    classification_counts and topology_repair_required boolean."""
    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)
    from app.core.database import get_db
    from app.db.models import FrontChainState
    from app.main import app
    from app.services.node_spec import session_orchestration as orch

    proj = client.post(
        "/api/v1/projects", json={"name": "cls-counts", "description": ""}
    ).json()
    sess = client.post(
        "/api/v1/front-chain/sessions", json={"project_id": proj["id"]}
    ).json()
    project_id = proj["id"]
    session_id = sess["id"]

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

    class _PersistentMismatchService:
        def specify_bundle(self, *, planner_output, compile_context, **kwargs):
            specs = [
                _spec("A", out_fields=["x"]),
                _spec(
                    "B",
                    in_slots=[
                        {
                            "name": "from_a",
                            "schema_hint": ["y"],
                            "source_hint": "A",
                        }
                    ],
                ),
            ]
            issue = EdgeContractIssue(
                edge_id="e1",
                from_node_id="A",
                to_node_id="B",
                kind="schema_mismatch",
                detail="d",
                classification="topology_repair_required",
            )
            return NodeExecutionSpecBundle(
                specs=specs,
                skipped_node_ids=[],
                profiling_summary={},
                edge_contract_report=EdgeContractReport(
                    checked_edges=1, issues=[issue]
                ),
                llm_trace={
                    "call_count": 6,
                    "llm_mode": "real",
                    "per_node_max_retries": 1,
                    "bundle_max_retries": 2,
                    "bundle_retry_attempts": [],
                },
            )

    original_specify = orch.specify_for_session

    def _patched(session_id, *, db, persist=True, service=None):
        return original_specify(
            session_id, db=db, persist=persist, service=_PersistentMismatchService()
        )

    monkeypatch.setattr(
        "app.api.routes.front_chain.specify_for_session", _patched
    )

    resp = client.post(f"/api/v1/front-chain/sessions/{session_id}/specify")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["classification_counts"] == {"topology_repair_required": 1}
    assert detail["topology_repair_required"] is True


# ---------------------------------------------------------------------------
# T8 + T9 — enricher's system_prompt
# ---------------------------------------------------------------------------


def test_t8_enricher_system_prompt_includes_contract_bound_rule_with_upstream_inputs():
    from app.services.workflow_planner.runtime_field_enricher import (
        apply_spec_to_node,
    )

    spec = _spec(
        "n_compute",
        in_slots=[
            {
                "name": "from_upstream",
                "kind": "upstream_output",
                "schema_hint": ["sku", "qty"],
                "source_hint": "n_acquire",  # not a path → upstream node
            }
        ],
    )
    node = _node(draft_node_id="n_compute")
    apply_spec_to_node(node=node, spec=spec)

    sys_prompt = node.planner_metadata["system_prompt"]
    assert "Contract-bound data access (HARD RULE)" in sys_prompt
    assert "input_contract bindings are AUTHORITATIVE" in sys_prompt
    # Use fragments robust to line wrapping in the prompt body.
    assert "re-read raw files" in sys_prompt
    assert "Do NOT infer additional file paths" in sys_prompt
    # The "this node has upstream-bound inputs" warning fires.
    assert "upstream-bound inputs" in sys_prompt


def test_t9_enricher_system_prompt_omits_upstream_warning_for_acquisition_node():
    from app.services.workflow_planner.runtime_field_enricher import (
        apply_spec_to_node,
    )

    # Acquisition node: source_hint IS a file path (no upstream node).
    spec = _spec(
        "n_acquire",
        in_slots=[
            {
                "name": "raw_input",
                "kind": "tabular_records",
                "schema_hint": ["sku"],
                "source_hint": "/tmp/in.csv",
            }
        ],
    )
    node = _node(draft_node_id="n_acquire", semantic_type="acquire")
    apply_spec_to_node(node=node, spec=spec)

    sys_prompt = node.planner_metadata["system_prompt"]
    # The hard rule still appears (it's universal).
    assert "Contract-bound data access (HARD RULE)" in sys_prompt
    # But the upstream-extra-warning does NOT.
    assert "upstream-bound inputs" not in sys_prompt
