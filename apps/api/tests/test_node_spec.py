"""Node spec pass — unit + integration tests.

Covers:

    D1  data_profiling_service reads CSV header + sample rows
    D2  data_profiling_service handles missing / non-path locators
    D3  data_profiling_service profiles directories (multiple files)
    S1  NodeSpecService (mock) produces valid bundle from synthetic planner_output
    S2  NodeSpecService (mock) skips human_gate / verify auto-insertions
    S3  NodeSpecService (mock) threads upstream source_hint by node_id
    V1  edge validator passes when source_hint + schema_hint align
    V2  edge validator catches missing_input_reference
    V3  edge validator catches schema_mismatch
    V4  edge validator drops edges into verify_after_* terminators
    V5  edge validator bridges human_gate_* pass-through (5 → gate → 6)
    E1  /specify endpoint returns 404 when session has no states
    E2  /specify endpoint returns 422 when session has states but no planner_output
    E3  /specify endpoint full path: project → session → turn → plan → specify
    E4  /specify endpoint is idempotent — second call overwrites bundle
    E5  /specify endpoint returns 422 when edge contract fails

All tests run in mock LLM mode via the conftest client fixture.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from app.core.config import settings
from app.schemas.node_execution_spec import (
    NODE_EXECUTION_SPEC_VERSION,
    AcceptanceCriterion,
    BusinessRule,
    ExecutionConstraints,
    FailurePolicy,
    NodeExecutionSpec,
    NodeExecutionSpecBundle,
    NodeInputContract,
    NodeInputSlot,
    NodeOutputContract,
    NodeOutputSlot,
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
from app.services.node_spec.data_profiling_service import (
    DataProfilingResult,
    profile_locator,
)
from app.services.node_spec.edge_contract_validator import validate_edge_contracts
from app.services.node_spec.node_spec_service import (
    EdgeContractFailure,
    NodeSpecService,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


def _mk_node(
    *,
    draft_node_id: str,
    semantic_type: str = "transform",
    semantic_goal: str = "",
    upstream: list | None = None,
    preferred: list | None = None,
    capability_id: str | None = None,
) -> WorkflowPlannerNodeDraft:
    meta: dict = {"step_name": draft_node_id}
    if capability_id is not None:
        meta["bound_capability"] = {
            "selected_capability_id": capability_id,
            "candidate_ids": [capability_id],
            "coverage": "full",
            "side_effect_class": "none",
            "selection_source": "test_fixture",
            "uncovered_step_kinds": [],
        }
    return WorkflowPlannerNodeDraft(
        draft_node_id=draft_node_id,
        step_name=draft_node_id,
        node_name=draft_node_id,
        node_semantic_type=semantic_type,
        semantic_goal=semantic_goal or f"do {draft_node_id}",
        preferred_capability_ids=preferred or [],
        candidate_capability_ids=preferred or [],
        upstream_dependencies=upstream or [],
        planner_metadata=meta,
    )


def _synthetic_planner_output() -> WorkflowPlannerOutput:
    return WorkflowPlannerOutput(
        node_drafts=[
            _mk_node(
                draft_node_id="n_ingest",
                semantic_type="acquire",
                semantic_goal="Read input CSV",
                preferred=["filesystem_read"],
                capability_id="filesystem_read",
            ),
            _mk_node(
                draft_node_id="n_analyze",
                semantic_type="transform",
                semantic_goal="Analyze rows and emit risk table",
                upstream=["n_ingest"],
                preferred=["coding_agent"],
                capability_id="coding_agent",
            ),
            _mk_node(
                draft_node_id="human_gate_before_n_persist",
                semantic_type="human_gate",
                semantic_goal="Pause for human review",
                upstream=["n_analyze"],
                preferred=["human_review_gate"],
                capability_id="human_review_gate",
            ),
            _mk_node(
                draft_node_id="n_persist",
                semantic_type="act",
                semantic_goal="Write result to output",
                upstream=["human_gate_before_n_persist"],
                preferred=["filesystem_write"],
                capability_id="filesystem_write",
            ),
            _mk_node(
                draft_node_id="verify_after_n_persist",
                semantic_type="verify",
                semantic_goal="Verify output contract",
                upstream=["n_persist"],
                preferred=["validate_schema"],
                capability_id="validate_schema",
            ),
        ],
        entry_node_ids=["n_ingest"],
        exit_node_ids=["verify_after_n_persist"],
    )


def _synthetic_compile_context(input_locator: str, output_locator: str) -> CompileContextResponse:
    pf = ProblemFormulationContract(
        interpreted_problem_kind="local_dataset_task",
        target_object="local_workspace",
        problem_status="solvable",
        current_system_fit="in_scope",
        planning_intent=PlanningIntentContract(
            intent_id="intent_1",
            label="Analyze CSV data",
            target_object="local_workspace",
            objective_family="analysis",
        ),
        blocking_axes=[],
        review_axes=[],
        blocking_reasons=[],
        interpreted_goal="Analyze the input CSV and write a summary",
    )
    ic = InputClosureContract(
        target_object="local_workspace",
        planning_intent_id="intent_1",
        candidate_sources=[
            InputSourceCandidate(
                candidate_id="src_1",
                locator_kind="path",
                locator_value=input_locator,
                content_kind="dataset",
                access_boundary="workspace",
                acquisition_kind="direct_read",
                confidence=0.95,
                evidence=["seeded"],
            )
        ],
        resolved_source_kind="file",
        resolved_source_locator=input_locator,
        resolved_source_modality="csv",
        acquisition_path_kind="direct_read",
        closure_status="input_closed",
        blocking_axes=[],
        review_axes=[],
        closure_requirements=[],
        reasoning_summary="test",
    )
    oc = OutputClosureContract(
        deliverable_form="dataset",
        delivery_target=output_locator,
        output_modality="csv",
        closure_requirements=[],
        blocking_axes=[],
        review_axes=[],
        closure_status="output_closed",
        reasoning_summary="test",
    )
    solvability = SolvabilityContract(
        planning_intent_id="intent_1",
        solvability_status="solvable",
        required_subgoals=[],
        capability_matches=[],
        problem_kind="local_dataset_task",
    )
    admissibility = ExecutionAdmissibilityContract(
        admissibility_status="supported",
    )
    return CompileContextResponse(
        compile_context_version="test-v1",
        requirement_id=None,
        project_id="proj-1",
        selected_blueprint_type=None,
        graph_closure_status="graph_closed",
        selected_template_id=None,
        problem_formulation=pf,
        input_closure=ic,
        output_closure=oc,
        solvability=solvability,
        execution_admissibility=admissibility,
        capability_envelope={},
        compile_constraints={},
        candidate_work_units=[],
        compile_trace={"authority_source": "test"},
    )


# ---------------------------------------------------------------------------
# D — data_profiling_service unit tests
# ---------------------------------------------------------------------------


def test_d1_profile_csv_header_and_samples(tmp_path: Path) -> None:
    path = tmp_path / "orders.csv"
    _write_csv(
        path,
        ["sku", "qty", "region"],
        [["P-001", 10, "east"], ["P-002", 5, "west"], ["P-003", 0, "north"]],
    )

    result = profile_locator(str(path))
    assert not result.missing
    assert len(result.files) == 1
    prof = result.files[0]
    assert prof.kind == "csv"
    assert prof.columns == ["sku", "qty", "region"]
    assert len(prof.sample_rows) == 3
    assert prof.sample_rows[0] == {"sku": "P-001", "qty": "10", "region": "east"}


def test_d2_profile_missing_and_empty(tmp_path: Path) -> None:
    empty = profile_locator(None)
    assert empty.missing is True
    assert empty.files == []

    blank = profile_locator("")
    assert blank.missing is True

    nowhere = profile_locator(str(tmp_path / "does_not_exist"))
    assert nowhere.missing is True


def test_d3_profile_directory_multiple_files(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("id,name\n1,alpha\n2,beta\n", encoding="utf-8")
    (tmp_path / "b.json").write_text('{"key": "value", "n": 42}', encoding="utf-8")
    (tmp_path / "c.txt").write_text("hello\nworld\n", encoding="utf-8")

    result = profile_locator(str(tmp_path))
    kinds = {f.path.split("/")[-1]: f.kind for f in result.files}
    assert kinds["a.csv"] == "csv"
    assert kinds["b.json"] == "json"
    assert kinds["c.txt"] == "text"


# ---------------------------------------------------------------------------
# S — NodeSpecService unit tests (mock LLM)
# ---------------------------------------------------------------------------


def test_s1_service_mock_produces_valid_bundle(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)

    input_path = tmp_path / "in.csv"
    _write_csv(input_path, ["sku", "qty"], [["P-001", 10]])
    output_path = tmp_path / "out.csv"

    planner_output = _synthetic_planner_output()
    compile_context = _synthetic_compile_context(str(input_path), str(output_path))

    bundle = NodeSpecService().specify_bundle(
        planner_output=planner_output,
        compile_context=compile_context,
    )
    assert bundle.spec_version == NODE_EXECUTION_SPEC_VERSION
    # ingest + analyze + persist are specced; human_gate + verify are skipped
    spec_ids = [s.node_id for s in bundle.specs]
    assert spec_ids == ["n_ingest", "n_analyze", "n_persist"]
    assert set(bundle.skipped_node_ids) == {
        "human_gate_before_n_persist",
        "verify_after_n_persist",
    }
    # LLM trace captured
    assert bundle.llm_trace["call_count"] == 3
    assert bundle.llm_trace["llm_mode"] == "mock"
    # Profiling summary included
    assert bundle.profiling_summary["missing"] is False
    # Persist node's primary_output gets the OC delivery target as locator_hint
    persist_spec = next(s for s in bundle.specs if s.node_id == "n_persist")
    assert persist_spec.output_contract.primary_output.locator_hint == str(output_path)


def test_s2_service_mock_upstream_source_hint_by_node_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)

    input_path = tmp_path / "in.csv"
    _write_csv(input_path, ["sku"], [["P-001"]])
    output_path = tmp_path / "out.csv"

    planner_output = _synthetic_planner_output()
    compile_context = _synthetic_compile_context(str(input_path), str(output_path))

    bundle = NodeSpecService().specify_bundle(
        planner_output=planner_output, compile_context=compile_context
    )
    # The ingest node reads from the file (source_hint is a path).
    ingest = next(s for s in bundle.specs if s.node_id == "n_ingest")
    assert ingest.input_contract.required
    assert str(input_path) in (ingest.input_contract.required[0].source_hint or "")
    # The analyze node consumes from n_ingest (source_hint is the upstream node_id).
    analyze = next(s for s in bundle.specs if s.node_id == "n_analyze")
    assert analyze.input_contract.required
    assert analyze.input_contract.required[0].source_hint == "n_ingest"


def test_s3_bundle_serializes_json_roundtrip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)
    input_path = tmp_path / "in.csv"
    _write_csv(input_path, ["a"], [["1"]])
    output_path = tmp_path / "out.csv"

    bundle = NodeSpecService().specify_bundle(
        planner_output=_synthetic_planner_output(),
        compile_context=_synthetic_compile_context(str(input_path), str(output_path)),
    )
    payload = bundle.model_dump(mode="json")
    reloaded = NodeExecutionSpecBundle.model_validate(payload)
    assert reloaded.spec_version == bundle.spec_version
    assert len(reloaded.specs) == len(bundle.specs)


# ---------------------------------------------------------------------------
# E — endpoint integration tests
# ---------------------------------------------------------------------------


def test_e1_specify_404_when_session_has_no_states(client) -> None:
    proj = client.post(
        "/api/v1/projects", json={"name": "t-e1", "description": ""}
    ).json()
    sess = client.post(
        "/api/v1/front-chain/sessions", json={"project_id": proj["id"]}
    ).json()
    resp = client.post(f"/api/v1/front-chain/sessions/{sess['id']}/specify")
    # Session exists but has no states at all → 404 from orchestration
    assert resp.status_code == 404, resp.text


def test_e2_specify_422_when_session_has_no_planner_output(client, tmp_path: Path) -> None:
    # Session with states but no plan stage run yet.
    proj = client.post(
        "/api/v1/projects", json={"name": "t-e2", "description": ""}
    ).json()
    sess = client.post(
        "/api/v1/front-chain/sessions", json={"project_id": proj["id"]}
    ).json()
    turn_resp = client.post(
        f"/api/v1/front-chain/sessions/{sess['id']}/turn",
        json={
            "kind": "raw_intent",
            "raw_intent": (
                "Read the CSV at /tmp/nonexistent.csv and summarize. "
                "Write output to /tmp/out.csv"
            ),
        },
    )
    assert turn_resp.status_code == 201, turn_resp.text
    # No /plan call yet → /specify should return 422
    resp = client.post(f"/api/v1/front-chain/sessions/{sess['id']}/specify")
    assert resp.status_code == 422, resp.text


def _seed_state_with_planner_output(
    client, tmp_path: Path, *, input_locator: str, output_locator: str
) -> tuple[str, str]:
    """Create project + session and seed a FrontChainState row directly
    with planner_output_json + compile_context_json. Bypasses the
    front-chain turn path which in mock mode does not always reach
    compile — this keeps the endpoint test focused on the spec stage.
    """
    from sqlalchemy.orm import sessionmaker

    from app.core.database import get_db
    from app.db.models import FrontChainState
    from app.main import app

    proj = client.post(
        "/api/v1/projects", json={"name": "seeded", "description": ""}
    ).json()
    sess = client.post(
        "/api/v1/front-chain/sessions", json={"project_id": proj["id"]}
    ).json()
    project_id = proj["id"]
    session_id = sess["id"]

    compile_context = _synthetic_compile_context(input_locator, output_locator)
    planner_output = _synthetic_planner_output()

    db_dep = app.dependency_overrides[get_db]
    gen = db_dep()
    db = next(gen)
    state = FrontChainState(
        project_id=project_id,
        session_id=session_id,
        raw_intent="seeded for node_spec endpoint test",
        problem_formulation_json=compile_context.problem_formulation.model_dump(mode="json"),
        input_closure_json=compile_context.input_closure.model_dump(mode="json"),
        output_closure_json=compile_context.output_closure.model_dump(mode="json"),
        merged_gaps_json={},
        top_questions_json=[],
        llm_trace_json={"call_count": 0, "llm_mode": "mock"},
        interpreter_version="mock-interpreter-v1",
        validator_version="mock-validator-v1",
        compile_context_json=compile_context.model_dump(mode="json"),
        compile_version="test-v1",
        planner_output_json=planner_output.model_dump(mode="json"),
        planner_version="workflow_planner_v1",
    )
    db.add(state)
    db.commit()
    try:
        next(gen)
    except StopIteration:
        pass
    return project_id, session_id


def test_e3_specify_full_path_with_plan(client, tmp_path: Path) -> None:
    input_path = tmp_path / "orders.csv"
    _write_csv(input_path, ["sku", "qty"], [["P-001", 10], ["P-002", 5]])
    output_path = tmp_path / "summary.csv"

    _, session_id = _seed_state_with_planner_output(
        client, tmp_path, input_locator=str(input_path), output_locator=str(output_path)
    )

    spec_resp = client.post(f"/api/v1/front-chain/sessions/{session_id}/specify")
    assert spec_resp.status_code == 201, spec_resp.text
    body = spec_resp.json()
    assert body["node_specs_version"] == NODE_EXECUTION_SPEC_VERSION
    bundle = NodeExecutionSpecBundle.model_validate(body["node_specs"])
    # 3 real nodes (ingest, analyze, persist); 2 skippable.
    assert len(bundle.specs) == 3
    assert set(bundle.skipped_node_ids) == {
        "human_gate_before_n_persist",
        "verify_after_n_persist",
    }
    # Profiling saw the real CSV.
    profile_paths = [f["path"] for f in bundle.profiling_summary["files"]]
    assert str(input_path) in profile_paths


# ---------------------------------------------------------------------------
# V — edge_contract_validator unit tests
# ---------------------------------------------------------------------------


def _spec(
    node_id: str,
    *,
    out_name: str,
    out_fields: list[str] | None = None,
    inputs: list[NodeInputSlot] | None = None,
) -> NodeExecutionSpec:
    return NodeExecutionSpec(
        node_id=node_id,
        semantic_goal=f"do {node_id}",
        input_contract=NodeInputContract(required=inputs or [], optional=[]),
        output_contract=NodeOutputContract(
            primary_output=NodeOutputSlot(
                name=out_name,
                kind="tabular_records",
                schema_hint=out_fields or [],
            ),
            secondary_outputs=[],
        ),
        business_rules=[],
        acceptance_criteria=[],
        execution_constraints=ExecutionConstraints(),
        failure_policy=FailurePolicy(),
    )


def _planner_with_edges(
    nodes: list[WorkflowPlannerNodeDraft],
    edges: list[tuple[str, str]],
) -> WorkflowPlannerOutput:
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


def test_v1_validator_passes_on_aligned_specs() -> None:
    nodes = [
        _mk_node(draft_node_id="A", semantic_type="acquire", capability_id="filesystem_read"),
        _mk_node(draft_node_id="B", semantic_type="transform", upstream=["A"], capability_id="coding_agent"),
    ]
    planner = _planner_with_edges(nodes, [("A", "B")])
    specs = [
        _spec("A", out_name="raw_table", out_fields=["sku", "qty"]),
        _spec(
            "B",
            out_name="result",
            out_fields=["sku", "score"],
            inputs=[
                NodeInputSlot(
                    name="raw_table",
                    kind="upstream_output",
                    schema_hint=["sku", "qty"],
                    source_hint="A",
                )
            ],
        ),
    ]
    report = validate_edge_contracts(planner_output=planner, specs=specs, skipped_node_ids=[])
    assert report.passed
    assert report.checked_edges == 1


def test_v2_validator_catches_missing_input_reference() -> None:
    nodes = [
        _mk_node(draft_node_id="A", semantic_type="acquire", capability_id="filesystem_read"),
        _mk_node(draft_node_id="B", semantic_type="transform", upstream=["A"], capability_id="coding_agent"),
    ]
    planner = _planner_with_edges(nodes, [("A", "B")])
    specs = [
        _spec("A", out_name="raw_table", out_fields=["sku"]),
        # B's input slot does NOT reference A in any way.
        _spec(
            "B",
            out_name="result",
            inputs=[
                NodeInputSlot(
                    name="something_else",
                    kind="file_path",
                    schema_hint=[],
                    source_hint="/tmp/other.csv",
                )
            ],
        ),
    ]
    report = validate_edge_contracts(planner_output=planner, specs=specs, skipped_node_ids=[])
    assert not report.passed
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.kind == "missing_input_reference"
    assert issue.from_node_id == "A"
    assert issue.to_node_id == "B"


def test_v3_validator_catches_schema_mismatch() -> None:
    nodes = [
        _mk_node(draft_node_id="A", semantic_type="acquire", capability_id="filesystem_read"),
        _mk_node(draft_node_id="B", semantic_type="transform", upstream=["A"], capability_id="coding_agent"),
    ]
    planner = _planner_with_edges(nodes, [("A", "B")])
    specs = [
        _spec("A", out_name="raw_table", out_fields=["sku", "qty"]),
        # B requires fields that A does NOT emit.
        _spec(
            "B",
            out_name="result",
            inputs=[
                NodeInputSlot(
                    name="raw_table",
                    kind="upstream_output",
                    schema_hint=["sku", "warehouse", "lead_time"],
                    source_hint="A",
                )
            ],
        ),
    ]
    report = validate_edge_contracts(planner_output=planner, specs=specs, skipped_node_ids=[])
    assert not report.passed
    issue = report.issues[0]
    assert issue.kind == "schema_mismatch"
    assert set(issue.missing_fields) == {"warehouse", "lead_time"}


def test_v4_validator_drops_edges_into_verify_terminator() -> None:
    nodes = [
        _mk_node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _mk_node(
            draft_node_id="verify_after_A",
            semantic_type="verify",
            upstream=["A"],
            capability_id="validate_schema",
        ),
    ]
    planner = _planner_with_edges(nodes, [("A", "verify_after_A")])
    specs = [_spec("A", out_name="result", out_fields=["sku"])]
    report = validate_edge_contracts(
        planner_output=planner, specs=specs, skipped_node_ids=["verify_after_A"]
    )
    # Edge into a verify terminator is observed-only, not validated.
    assert report.checked_edges == 0
    assert report.passed


def test_v5_validator_bridges_human_gate_passthrough() -> None:
    nodes = [
        _mk_node(draft_node_id="A", semantic_type="transform", capability_id="coding_agent"),
        _mk_node(
            draft_node_id="human_gate_before_B",
            semantic_type="human_gate",
            upstream=["A"],
            capability_id="human_review_gate",
        ),
        _mk_node(
            draft_node_id="B",
            semantic_type="act",
            upstream=["human_gate_before_B"],
            capability_id="filesystem_write",
        ),
    ]
    planner = _planner_with_edges(
        nodes, [("A", "human_gate_before_B"), ("human_gate_before_B", "B")]
    )
    # B's source_hint points at the gate (the LLM's likely choice). The
    # bridging logic should still resolve the edge as A → B and detect
    # the schema mismatch between A's output and B's expected input.
    specs = [
        _spec("A", out_name="result", out_fields=["sku", "qty", "extra"]),
        _spec(
            "B",
            out_name="written",
            inputs=[
                NodeInputSlot(
                    name="result",
                    kind="upstream_output",
                    schema_hint=["sku", "qty", "missing_field"],
                    source_hint="human_gate_before_B",
                )
            ],
        ),
    ]
    report = validate_edge_contracts(
        planner_output=planner,
        specs=specs,
        skipped_node_ids=["human_gate_before_B"],
    )
    # Effective edge A → B was computed across the gate. Because B asks
    # for `missing_field` which A does not emit, schema_mismatch fires.
    assert not report.passed
    assert report.checked_edges >= 1
    issue = next(i for i in report.issues if i.from_node_id == "A" and i.to_node_id == "B")
    assert issue.kind == "schema_mismatch"
    assert "missing_field" in issue.missing_fields


def test_e5_specify_endpoint_returns_422_on_edge_contract_failure(
    client, tmp_path: Path, monkeypatch
) -> None:
    """End-to-end: stub NodeSpecService so it returns mismatched specs,
    then check that /specify surfaces the issues with HTTP 422.
    """
    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)
    input_path = tmp_path / "in.csv"
    _write_csv(input_path, ["sku"], [["P-001"]])
    output_path = tmp_path / "out.csv"

    project_id, session_id = _seed_state_with_planner_output(
        client, tmp_path, input_locator=str(input_path), output_locator=str(output_path)
    )

    # Override the stored planner_output to one with edges + monkeypatch
    # the spec service so it returns specs we control. We do this by
    # injecting a custom NodeSpecService that returns specs that violate
    # the contract.
    from app.services.node_spec import session_orchestration as orch

    class _BrokenSpecService:
        def specify_bundle(self, *, planner_output, compile_context):
            from app.schemas.node_execution_spec import (
                EdgeContractReport,
                NodeExecutionSpecBundle,
            )

            specs = [
                _spec("n_ingest", out_name="raw_table", out_fields=["sku"]),
                _spec(
                    "n_analyze",
                    out_name="analysis",
                    out_fields=["sku", "score"],
                    inputs=[
                        # Wrong source_hint — claims a non-existent upstream.
                        NodeInputSlot(
                            name="something",
                            kind="upstream_output",
                            schema_hint=[],
                            source_hint="nowhere",
                        )
                    ],
                ),
                _spec("n_persist", out_name="written", inputs=[]),
            ]
            from app.services.node_spec.edge_contract_validator import (
                validate_edge_contracts,
            )

            report = validate_edge_contracts(
                planner_output=planner_output,
                specs=specs,
                skipped_node_ids=[
                    "human_gate_before_n_persist",
                    "verify_after_n_persist",
                ],
            )
            return NodeExecutionSpecBundle(
                spec_version=NODE_EXECUTION_SPEC_VERSION,
                specs=specs,
                skipped_node_ids=[
                    "human_gate_before_n_persist",
                    "verify_after_n_persist",
                ],
                profiling_summary={},
                edge_contract_report=report,
                llm_trace={"call_count": 0, "llm_mode": "mock"},
            )

    # Patch the FastAPI dependency: the route resolves the service via
    # a fresh NodeSpecService() inside specify_for_session. Replace the
    # function with a wrapper that injects the broken service.
    original = orch.specify_for_session

    def _patched(session_id, *, db, persist=True, service=None):
        return original(
            session_id, db=db, persist=persist, service=_BrokenSpecService()
        )

    monkeypatch.setattr(
        "app.api.routes.front_chain.specify_for_session", _patched
    )

    # Patch the planner_output stored on the seeded state so it has
    # edges that link n_ingest → n_analyze.
    from app.core.database import get_db
    from app.db.models import FrontChainState
    from app.main import app

    db_dep = app.dependency_overrides[get_db]
    gen = db_dep()
    db = next(gen)
    row = (
        db.query(FrontChainState)
        .filter(FrontChainState.session_id == session_id)
        .first()
    )
    po = WorkflowPlannerOutput.model_validate(row.planner_output_json)
    po.edge_drafts = [
        WorkflowPlannerEdgeDraft(
            draft_edge_id="e1", from_node_id="n_ingest", to_node_id="n_analyze"
        )
    ]
    row.planner_output_json = po.model_dump(mode="json")
    db.add(row)
    db.commit()
    try:
        next(gen)
    except StopIteration:
        pass

    resp = client.post(f"/api/v1/front-chain/sessions/{session_id}/specify")
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "edge_contract_failed"
    assert detail["checked_edges"] >= 1
    assert detail["issues"]
    assert detail["issues"][0]["kind"] == "missing_input_reference"


def test_e4_specify_is_idempotent(client, tmp_path: Path) -> None:
    input_path = tmp_path / "orders.csv"
    _write_csv(input_path, ["sku", "qty"], [["P-001", 10]])
    output_path = tmp_path / "summary.csv"

    _, session_id = _seed_state_with_planner_output(
        client, tmp_path, input_locator=str(input_path), output_locator=str(output_path)
    )

    first = client.post(f"/api/v1/front-chain/sessions/{session_id}/specify")
    second = client.post(f"/api/v1/front-chain/sessions/{session_id}/specify")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["state_id"] == second.json()["state_id"]
