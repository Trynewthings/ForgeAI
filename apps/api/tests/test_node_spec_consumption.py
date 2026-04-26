"""Task 2: spec-consumption tests.

Coverage matrix:

    C1  capability_mismatch_validator passes when classes align
    C2  capability_mismatch_validator catches specialised mismatch
    C3  capability_mismatch_validator allows coding_agent for any class
    C4  capability_mismatch_validator catches missing capability selection
    N1  apply_spec_to_node stamps task_prompt + system_prompt + args + expected_outputs
    N2  apply_spec_to_node sets agentic flag from runtime_class
    O1  _derive_obligation maps spec.acceptance_criteria.presence -> output_exists
    O2  _derive_obligation maps spec.tabular_contract -> tabular_contract w/ schema_hint headers
    O3  _derive_obligation carries unsupported acceptance kinds as advisory notes
    O4  bindings_from_planner_output threads node_specs to verify obligation
    B1  build hard-blocks when bundle.edge_contract_report has issues
    B2  build hard-blocks when capability_mismatch validator finds issues
    B3  build proceeds when bundle is None (back-compat: no specs is allowed)
"""
from __future__ import annotations

from pathlib import Path

import pytest

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
from app.schemas.workflow_planner import (
    WorkflowPlannerEdgeDraft,
    WorkflowPlannerNodeDraft,
    WorkflowPlannerOutput,
)
from app.services.node_spec.capability_mismatch_validator import (
    validate_capability_mismatch,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _spec(
    node_id: str,
    *,
    runtime_class: str = "bounded_transform",
    out_name: str = "out",
    out_fields: list[str] | None = None,
    out_locator: str | None = None,
    inputs: list[NodeInputSlot] | None = None,
    rules: list[BusinessRule] | None = None,
    criteria: list[AcceptanceCriterion] | None = None,
    sketch: list[str] | None = None,
    max_iterations: int = 1,
    max_duration_seconds: int = 60,
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
                locator_hint=out_locator,
            ),
            secondary_outputs=[],
        ),
        business_rules=rules or [],
        acceptance_criteria=criteria or [],
        execution_constraints=ExecutionConstraints(
            expected_runtime_class=runtime_class,
            max_iterations=max_iterations,
            max_duration_seconds=max_duration_seconds,
        ),
        failure_policy=FailurePolicy(),
        verifier_hints=VerifierHints(),
        implementation_sketch=sketch or [],
    )


def _node(
    *,
    draft_node_id: str,
    semantic_type: str = "transform",
    capability_id: str | None = None,
    upstream: list[str] | None = None,
) -> WorkflowPlannerNodeDraft:
    meta = {"step_name": draft_node_id}
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
        semantic_goal=f"do {draft_node_id}",
        preferred_capability_ids=[capability_id] if capability_id else [],
        candidate_capability_ids=[capability_id] if capability_id else [],
        upstream_dependencies=upstream or [],
        planner_metadata=meta,
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
        entry_node_ids=[nodes[0].draft_node_id] if nodes else [],
        exit_node_ids=[nodes[-1].draft_node_id] if nodes else [],
    )


# ---------------------------------------------------------------------------
# C — capability_mismatch_validator
# ---------------------------------------------------------------------------


def test_c1_validator_passes_on_aligned_classes():
    nodes = [
        _node(draft_node_id="n_read", capability_id="filesystem_read", semantic_type="acquire"),
        _node(draft_node_id="n_calc", capability_id="coding_agent", semantic_type="transform"),
        _node(draft_node_id="n_write", capability_id="filesystem_write", semantic_type="act"),
    ]
    specs = [
        _spec("n_read", runtime_class="bounded_acquire"),
        _spec("n_calc", runtime_class="bounded_transform"),
        _spec("n_write", runtime_class="bounded_persist"),
    ]
    report = validate_capability_mismatch(
        planner_output=_planner(nodes, []),
        specs=specs,
        skipped_node_ids=[],
    )
    assert report.passed
    assert report.checked_nodes == 3


def test_c2_validator_catches_specialised_mismatch():
    # planner picked filesystem_read for a node whose spec says it's
    # actually compute work — exactly the spare-parts scenario failure.
    nodes = [
        _node(draft_node_id="n_analyze", capability_id="filesystem_read", semantic_type="transform"),
    ]
    specs = [_spec("n_analyze", runtime_class="bounded_transform")]
    report = validate_capability_mismatch(
        planner_output=_planner(nodes, []),
        specs=specs,
        skipped_node_ids=[],
    )
    assert not report.passed
    issue = report.issues[0]
    assert issue.kind == "specialised_class_mismatch"
    assert issue.selected_capability_id == "filesystem_read"
    assert issue.expected_runtime_class == "bounded_transform"
    assert issue.allowed_runtime_classes == ["bounded_acquire"]


def test_c3_validator_allows_coding_agent_for_any_class():
    # coding_agent is generalist — no class is a mismatch, even if
    # the node is purely an acquire / persist.
    nodes = [
        _node(draft_node_id="n_acquire_via_ca", capability_id="coding_agent", semantic_type="acquire"),
        _node(draft_node_id="n_persist_via_ca", capability_id="coding_agent", semantic_type="act"),
    ]
    specs = [
        _spec("n_acquire_via_ca", runtime_class="bounded_acquire"),
        _spec("n_persist_via_ca", runtime_class="bounded_persist"),
    ]
    report = validate_capability_mismatch(
        planner_output=_planner(nodes, []),
        specs=specs,
        skipped_node_ids=[],
    )
    assert report.passed


def test_c4_validator_catches_missing_capability_selection():
    nodes = [
        _node(draft_node_id="n_orphan", capability_id=None, semantic_type="transform"),
    ]
    nodes[0].planner_metadata = {"step_name": "n_orphan"}
    specs = [_spec("n_orphan", runtime_class="bounded_transform")]
    report = validate_capability_mismatch(
        planner_output=_planner(nodes, []),
        specs=specs,
        skipped_node_ids=[],
    )
    assert not report.passed
    assert report.issues[0].kind == "missing_capability_selection"


# ---------------------------------------------------------------------------
# N — apply_spec_to_node (enricher)
# ---------------------------------------------------------------------------


def test_n1_apply_spec_stamps_prompts_and_expected_outputs():
    from app.services.workflow_planner.runtime_field_enricher import (
        META_ARGS,
        META_EXPECTED_OUTPUTS,
        META_SPEC_DRIVEN,
        META_SYSTEM_PROMPT,
        META_TASK_PROMPT,
        apply_spec_to_node,
    )

    node = _node(draft_node_id="n1", capability_id="coding_agent")
    spec = _spec(
        "n1",
        runtime_class="bounded_transform",
        out_name="risk_table",
        out_fields=["sku_id", "risk_level"],
        out_locator="/tmp/out.csv",
        inputs=[
            NodeInputSlot(
                name="rows",
                kind="tabular_records",
                schema_hint=["sku_id", "qty"],
                source_hint="upstream_x",
            )
        ],
        rules=[
            BusinessRule(rule_id="r1", rule="No null sku_id", category="data_integrity"),
        ],
        criteria=[
            AcceptanceCriterion(
                criterion_id="c1",
                statement="Output has at least one row",
                kind="row_count",
                params={"min": 1},
            ),
        ],
        sketch=["Read upstream", "Compute risk", "Emit table"],
        max_iterations=3,
        max_duration_seconds=180,
    )

    apply_spec_to_node(node=node, spec=spec)

    md = node.planner_metadata
    assert md[META_SPEC_DRIVEN] is True
    # Task prompt mentions the goal, business rule, and acceptance criterion.
    task_prompt = md[META_TASK_PROMPT]
    assert "do n1" in task_prompt
    assert "No null sku_id" in task_prompt
    assert "row_count" in task_prompt
    assert "[data_integrity]" in task_prompt
    # Sketch made it through.
    assert "Read upstream" in task_prompt
    # System prompt frames execution constraints.
    sys_prompt = md[META_SYSTEM_PROMPT]
    assert "bounded_transform" in sys_prompt
    assert "max_iterations: 3" in sys_prompt
    # Args populated from spec.
    args = md[META_ARGS]
    assert args["max_iterations"] == 3
    assert args["max_duration_seconds"] == 180
    assert args["allow_network"] is False
    # Expected outputs lists the primary output.
    expected = md[META_EXPECTED_OUTPUTS]
    assert expected[0]["name"] == "risk_table"
    assert expected[0]["locator"] == "/tmp/out.csv"
    # schema_hint is serialized as a comma-joined string for the
    # downstream coding_agent ExpectedOutput contract (which requires
    # str, not list).
    assert expected[0]["schema_hint"] == "sku_id, risk_level"


def test_n2_apply_spec_sets_agentic_when_runtime_class_is_agentic():
    from app.services.workflow_planner.runtime_field_enricher import (
        META_ARGS,
        apply_spec_to_node,
    )

    node = _node(draft_node_id="n2", capability_id="coding_agent")
    spec = _spec("n2", runtime_class="bounded_agentic", max_iterations=5)
    apply_spec_to_node(node=node, spec=spec)
    assert node.planner_metadata[META_ARGS]["agentic"] is True

    node2 = _node(draft_node_id="n3", capability_id="coding_agent")
    spec2 = _spec("n3", runtime_class="bounded_transform")
    apply_spec_to_node(node=node2, spec=spec2)
    assert node2.planner_metadata[META_ARGS]["agentic"] is False


# ---------------------------------------------------------------------------
# O — spec-driven obligation (_derive_obligation)
# ---------------------------------------------------------------------------


def test_o1_derive_obligation_maps_presence_to_output_exists(tmp_path: Path):
    from app.services.build.planner_output_bindings import _derive_obligation

    target_path = str(tmp_path / "out.csv")
    verify_node = _node(draft_node_id="verify_after_n", semantic_type="verify")
    spec = _spec(
        "n",
        out_locator=target_path,
        criteria=[
            AcceptanceCriterion(
                criterion_id="c1",
                statement="output exists",
                kind="presence",
                params={},
            )
        ],
    )

    obligation = _derive_obligation(
        verify_node=verify_node,
        target_locator=target_path,
        target_is_fs_write=True,
        target_spec=spec,
    )
    presence_checks = [
        c for c in obligation.checks if c.check_type == "output_exists"
    ]
    # Spec-derived check + always-on hygiene check both present.
    assert len(presence_checks) >= 2
    spec_check = next(c for c in obligation.checks if c.check_id.endswith("_spec_c1"))
    assert spec_check.check_type == "output_exists"
    assert spec_check.params["path"] == target_path


def test_o2_derive_obligation_maps_tabular_with_schema_hint_headers(tmp_path: Path):
    from app.services.build.planner_output_bindings import _derive_obligation

    target_path = str(tmp_path / "out.csv")
    verify_node = _node(draft_node_id="verify_after_n", semantic_type="verify")
    spec = _spec(
        "n",
        out_fields=["sku_id", "warehouse", "qty"],
        out_locator=target_path,
        criteria=[
            AcceptanceCriterion(
                criterion_id="c2",
                statement="output csv contract",
                kind="tabular_contract",
                params={"min_rows": 5},
            )
        ],
    )

    obligation = _derive_obligation(
        verify_node=verify_node,
        target_locator=target_path,
        target_is_fs_write=True,
        target_spec=spec,
    )
    spec_tabular = next(c for c in obligation.checks if c.check_id.endswith("_spec_c2"))
    assert spec_tabular.check_type == "tabular_contract"
    assert spec_tabular.params["min_rows"] == 5
    assert spec_tabular.params["required_headers"] == ["sku_id", "warehouse", "qty"]
    # Hygiene tabular check should NOT be added when spec already adds one
    # (avoid duplicate row_count enforcement).
    hygiene_tabulars = [
        c for c in obligation.checks if c.check_type == "tabular_contract"
    ]
    assert len(hygiene_tabulars) == 1


def test_o3_derive_obligation_carries_unsupported_kinds_as_advisory(tmp_path: Path):
    from app.services.build.planner_output_bindings import _derive_obligation

    target_path = str(tmp_path / "out.csv")
    verify_node = _node(draft_node_id="verify_after_n", semantic_type="verify")
    spec = _spec(
        "n",
        out_locator=target_path,
        criteria=[
            AcceptanceCriterion(
                criterion_id="c3",
                statement="risk_level is in {Low, Medium, High}",
                kind="enum_membership",
                params={"field": "risk_level", "allowed": ["Low", "Medium", "High"]},
            ),
            AcceptanceCriterion(
                criterion_id="c4",
                statement="sku_id has no duplicates",
                kind="uniqueness",
                params={"field": "sku_id"},
            ),
        ],
    )

    obligation = _derive_obligation(
        verify_node=verify_node,
        target_locator=target_path,
        target_is_fs_write=True,
        target_spec=spec,
    )
    # No verifier impl exists yet for enum / uniqueness; they should NOT
    # appear as checks (otherwise the verifier would error out as
    # "unknown check_type"). They DO appear in the description for audit.
    check_types = {c.check_type for c in obligation.checks}
    assert "enum_membership" not in check_types
    assert "uniqueness" not in check_types
    assert "[enum_membership]" in obligation.description
    assert "[uniqueness]" in obligation.description


def test_o4_bindings_threads_specs_to_verify_obligation(tmp_path: Path):
    from app.services.build.planner_output_bindings import (
        bindings_from_planner_output,
    )

    out_path = str(tmp_path / "out.csv")
    nodes = [
        _node(draft_node_id="n_persist", capability_id="filesystem_write", semantic_type="act"),
        _node(
            draft_node_id="verify_after_n_persist",
            capability_id="validate_schema",
            semantic_type="verify",
            upstream=["n_persist"],
        ),
    ]
    planner = _planner(nodes, [("n_persist", "verify_after_n_persist")])

    # Minimal compile_context with output locator.
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

    cc = CompileContextResponse(
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
            candidate_sources=[
                InputSourceCandidate(
                    candidate_id="s",
                    locator_kind="path",
                    locator_value=str(tmp_path / "in.csv"),
                    content_kind="dataset",
                    access_boundary="workspace",
                    acquisition_kind="direct_read",
                    confidence=1.0,
                    evidence=["t"],
                )
            ],
            resolved_source_kind="file",
            resolved_source_locator=str(tmp_path / "in.csv"),
            resolved_source_modality="csv",
            acquisition_path_kind="direct_read",
            closure_status="input_closed",
            blocking_axes=[],
            review_axes=[],
            closure_requirements=[],
            reasoning_summary="t",
        ),
        output_closure=OutputClosureContract(
            deliverable_form="dataset",
            delivery_target=out_path,
            output_modality="csv",
            closure_requirements=[],
            blocking_axes=[],
            review_axes=[],
            closure_status="output_closed",
            reasoning_summary="t",
        ),
        solvability=SolvabilityContract(planning_intent_id="i", solvability_status="solvable"),
        execution_admissibility=ExecutionAdmissibilityContract(
            admissibility_status="supported"
        ),
        capability_envelope={},
        compile_constraints={},
        candidate_work_units=[],
        compile_trace={},
    )
    specs = [
        _spec(
            "n_persist",
            runtime_class="bounded_persist",
            out_locator=out_path,
            out_fields=["sku_id", "qty"],
            criteria=[
                AcceptanceCriterion(
                    criterion_id="cN",
                    statement="output is a valid CSV",
                    kind="tabular_contract",
                    params={"min_rows": 1},
                )
            ],
        ),
    ]

    _, bindings = bindings_from_planner_output(
        planner, compile_context=cc, node_specs=specs
    )
    verify_binding = next(
        b for b in bindings if b.execution_kind == "verify"
    )
    obligation = verify_binding.config_hints["obligation"]
    spec_check = next(
        c for c in obligation["checks"] if c["check_id"].endswith("_spec_cN")
    )
    assert spec_check["check_type"] == "tabular_contract"
    assert spec_check["params"]["min_rows"] == 1
    assert spec_check["params"]["required_headers"] == ["sku_id", "qty"]


# ---------------------------------------------------------------------------
# B — build hard-block integration
# ---------------------------------------------------------------------------


def test_b1_build_hard_blocks_on_edge_contract_issues():
    from app.services.build.build_execution_service import _enforce_spec_gates

    nodes = [
        _node(draft_node_id="A", capability_id="filesystem_read", semantic_type="acquire"),
        _node(draft_node_id="B", capability_id="coding_agent", semantic_type="transform", upstream=["A"]),
    ]
    bundle = NodeExecutionSpecBundle(
        specs=[
            _spec("A", runtime_class="bounded_acquire"),
            _spec("B", runtime_class="bounded_transform"),
        ],
        skipped_node_ids=[],
        edge_contract_report=EdgeContractReport(
            checked_edges=1,
            issues=[
                EdgeContractIssue(
                    edge_id="e1",
                    from_node_id="A",
                    to_node_id="B",
                    kind="schema_mismatch",
                    detail="dst expects [foo] not in src [bar]",
                    missing_fields=["foo"],
                )
            ],
        ),
    )
    with pytest.raises(RuntimeError, match="edge_contract"):
        _enforce_spec_gates(bundle=bundle, planner_output=_planner(nodes, [("A", "B")]))


def test_b2_build_repairs_capability_mismatch_then_proceeds():
    """Task 7: capability_mismatch on a transform node is auto-repaired
    to coding_agent. The build proceeds; repair attempts are returned."""
    from app.services.build.build_execution_service import _enforce_spec_gates

    nodes = [
        _node(draft_node_id="n_compute", capability_id="filesystem_read", semantic_type="transform"),
    ]
    bundle = NodeExecutionSpecBundle(
        specs=[_spec("n_compute", runtime_class="bounded_transform")],
        skipped_node_ids=[],
        edge_contract_report=EdgeContractReport(checked_edges=0),
    )
    planner = _planner(nodes, [])
    outcome = _enforce_spec_gates(bundle=bundle, planner_output=planner)
    # The repair re-bound n_compute from filesystem_read to coding_agent.
    assert outcome["planner_output_modified"] is True
    assert len(outcome["repair_attempts"]) == 1
    a = outcome["repair_attempts"][0]
    assert a["node_id"] == "n_compute"
    assert a["original_capability"] == "filesystem_read"
    assert a["new_capability"] == "coding_agent"
    assert a["repair_source"] == "deterministic_generalist"
    # And the in-memory planner_output reflects the change.
    bound = planner.node_drafts[0].planner_metadata["bound_capability"]
    assert bound["selected_capability_id"] == "coding_agent"
    assert bound["selection_source"] == "capability_repair"


def test_b3_load_node_specs_bundle_returns_none_when_state_has_no_specs():
    """``_load_node_specs_bundle`` is a pure loader — returns None on a
    state row that has no node_specs_json. The build pipeline now
    treats None as a signal to lazy-call ``specify_for_session``
    rather than tolerating missing specs (the legacy back-compat
    fallback was masking the forgeDemo-skips-/specify bug). This
    unit test just pins the loader contract."""
    from app.services.build.build_execution_service import _load_node_specs_bundle

    class _StubRow:
        node_specs_json = None
        id = "stub"

    assert _load_node_specs_bundle(_StubRow()) is None


def test_b3b_build_proceeds_when_bundle_passes_all_gates():
    from app.services.build.build_execution_service import _enforce_spec_gates

    nodes = [
        _node(draft_node_id="A", capability_id="filesystem_read", semantic_type="acquire"),
    ]
    bundle = NodeExecutionSpecBundle(
        specs=[_spec("A", runtime_class="bounded_acquire")],
        skipped_node_ids=[],
        edge_contract_report=EdgeContractReport(checked_edges=0),
    )
    # Should NOT raise.
    _enforce_spec_gates(bundle=bundle, planner_output=_planner(nodes, []))


def test_b4_build_lazy_runs_specify_when_state_has_no_specs(
    client, tmp_path: Path, monkeypatch,
):
    """Build must NOT silently proceed with missing specs. When a
    session has planner_output but no node_specs_json (e.g. a client
    that skipped /specify between /plan and /builds), the build path
    lazy-calls specify_for_session and then enforces gates against
    the resulting specs.

    This test pins the new contract by:
      1. Seeding state with planner_output but null node_specs_json,
      2. Triggering build via the API,
      3. Asserting node_specs_json is populated after build.
    """
    from app.core.config import settings
    from app.core.database import get_db
    from app.db.models import FrontChainState
    from app.main import app
    from app.services.job.job_execution_service import JobExecutionService

    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)
    monkeypatch.setenv("CODING_AGENT_SANDBOX", "subprocess")

    proj = client.post(
        "/api/v1/projects", json={"name": "lazy-spec", "description": ""}
    ).json()
    project_id = proj["id"]
    sess = client.post(
        "/api/v1/front-chain/sessions", json={"project_id": project_id}
    ).json()
    session_id = sess["id"]

    # Seed state: planner_output + compile_context + null specs.
    output_path = tmp_path / "out.txt"
    nodes = [
        _node(
            draft_node_id="node_1",
            capability_id="coding_agent",
            semantic_type="transform",
        )
    ]
    nodes[0].planner_metadata["args"] = {
        "available_tools": [],
        "loop_config": {"max_iterations": 1},
        "agentic": False,
    }
    nodes[0].planner_metadata["expected_outputs"] = [
        {"locator": str(output_path), "kind": "text.report"}
    ]
    planner_output = _planner(nodes, [])

    # Compile context — same shape as test_e2e_mock_spec_drives_build_bindings.
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

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "a.csv").write_text("col\n1\n")

    cc = CompileContextResponse(
        compile_context_version="t",
        requirement_id=None,
        project_id=project_id,
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
            candidate_sources=[
                InputSourceCandidate(
                    candidate_id="s",
                    locator_kind="path",
                    locator_value=str(input_dir),
                    content_kind="dataset",
                    access_boundary="workspace",
                    acquisition_kind="direct_read",
                    confidence=1.0,
                    evidence=["t"],
                )
            ],
            resolved_source_kind="directory",
            resolved_source_locator=str(input_dir),
            resolved_source_modality="csv",
            acquisition_path_kind="direct_read",
            closure_status="input_closed",
            blocking_axes=[],
            review_axes=[],
            closure_requirements=[],
            reasoning_summary="t",
        ),
        output_closure=OutputClosureContract(
            deliverable_form="dataset",
            delivery_target=str(output_path),
            output_modality="text",
            closure_requirements=[],
            blocking_axes=[],
            review_axes=[],
            closure_status="output_closed",
            reasoning_summary="t",
        ),
        solvability=SolvabilityContract(
            planning_intent_id="i",
            solvability_status="solvable",
        ),
        execution_admissibility=ExecutionAdmissibilityContract(
            admissibility_status="supported",
        ),
        capability_envelope={},
        compile_constraints={},
        candidate_work_units=[],
        compile_trace={},
    )

    db_dep = app.dependency_overrides[get_db]
    gen = db_dep()
    db = next(gen)
    state = FrontChainState(
        project_id=project_id,
        session_id=session_id,
        raw_intent="seeded with planner_output but no specs",
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
        node_specs_json=None,  # critical: NO specs persisted
        node_specs_version=None,
    )
    db.add(state)
    db.commit()
    try:
        next(gen)
    except StopIteration:
        pass

    # Trigger build via API + force in-process execution.
    bid_resp = client.post(
        "/api/v1/builds",
        json={"project_id": project_id, "session_id": session_id},
    )
    assert bid_resp.status_code == 201, bid_resp.text
    bid = bid_resp.json()["id"]
    started = client.post(f"/api/v1/builds/{bid}/start")
    assert started.status_code == 200
    JobExecutionService().execute_job(started.json()["active_job_id"])

    # Build must have completed (not blocked) AND specs must now be
    # persisted on the state row.
    final_build = client.get(f"/api/v1/builds/{bid}").json()
    assert final_build["status"] == "completed", (
        f"expected completed, got {final_build['status']} "
        f"err={final_build.get('error_message')}"
    )

    gen = db_dep()
    db = next(gen)
    try:
        latest = (
            db.query(FrontChainState)
            .filter(FrontChainState.session_id == session_id)
            .order_by(FrontChainState.created_at.desc())
            .first()
        )
        assert latest is not None
        assert latest.node_specs_json is not None, (
            "build did NOT lazy-call specify — node_specs_json still null"
        )
        assert (latest.node_specs_json.get("specs") or []), (
            "lazy specify must persist a non-empty specs list"
        )
    finally:
        try:
            next(gen)
        except StopIteration:
            pass


def test_e2e_mock_spec_drives_build_bindings(client, tmp_path: Path, monkeypatch):
    """Full chain: project → seeded planner+spec state → build → bindings.

    Verifies:
      * build completes (status=completed)
      * bindings carry the spec-driven task_prompt (not the placeholder)
      * verify obligation has the spec-derived check
    """
    from app.core.config import settings
    from app.core.database import get_db
    from app.db.models import FrontChainState
    from app.main import app
    from app.services.job.job_execution_service import JobExecutionService
    from app.services.workflow_planner.runtime_field_enricher import (
        META_SPEC_DRIVEN,
        META_TASK_PROMPT,
    )

    monkeypatch.setattr(settings, "llm_mode", "mock", raising=False)
    monkeypatch.setenv("CODING_AGENT_SANDBOX", "subprocess")

    # Project + session via API.
    proj = client.post(
        "/api/v1/projects", json={"name": "spec-e2e", "description": ""}
    ).json()
    project_id = proj["id"]
    sess = client.post(
        "/api/v1/front-chain/sessions", json={"project_id": project_id}
    ).json()
    session_id = sess["id"]

    # Seed state: planner_output + matching spec bundle directly.
    output_path = tmp_path / "out.txt"
    nodes = [
        _node(
            draft_node_id="node_1",
            capability_id="coding_agent",
            semantic_type="transform",
        )
    ]
    nodes[0].planner_metadata["args"] = {
        "available_tools": [],
        "loop_config": {"max_iterations": 1},
        "agentic": False,
    }
    nodes[0].planner_metadata["system_prompt"] = "placeholder"
    nodes[0].planner_metadata["task_prompt"] = "placeholder"
    nodes[0].planner_metadata["expected_outputs"] = [
        {"locator": str(output_path), "kind": "text.report"}
    ]
    planner_output = _planner(nodes, [])

    spec = _spec(
        "node_1",
        runtime_class="bounded_transform",
        out_locator=str(output_path),
        out_fields=[],
        rules=[
            BusinessRule(
                rule_id="r1",
                rule="Write exactly one short report line.",
                category="completeness",
            )
        ],
        criteria=[
            AcceptanceCriterion(
                criterion_id="c1",
                statement="output exists",
                kind="presence",
                params={},
            )
        ],
        sketch=["Open the output path", "Write one line", "Flush"],
    )
    bundle = NodeExecutionSpecBundle(
        specs=[spec],
        skipped_node_ids=[],
        edge_contract_report=EdgeContractReport(checked_edges=0),
    )

    # Compile context — minimal but enough for build to derive workspace.
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

    cc = CompileContextResponse(
        compile_context_version="t",
        requirement_id=None,
        project_id=project_id,
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
            interpreted_goal="Write a one-line report.",
        ),
        input_closure=InputClosureContract(
            target_object="local_workspace",
            planning_intent_id="i",
            candidate_sources=[],
            resolved_source_kind=None,
            resolved_source_locator=None,
            resolved_source_modality=None,
            acquisition_path_kind="unknown",
            closure_status="input_closed",
            blocking_axes=[],
            review_axes=[],
            closure_requirements=[],
            reasoning_summary="t",
            applicability="not_applicable",
        ),
        output_closure=OutputClosureContract(
            deliverable_form="text",
            delivery_target=str(output_path),
            output_modality="text",
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

    db_dep = app.dependency_overrides[get_db]
    gen = db_dep()
    db = next(gen)
    state = FrontChainState(
        project_id=project_id,
        session_id=session_id,
        raw_intent="seeded for spec e2e",
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
        node_specs_json=bundle.model_dump(mode="json"),
        node_specs_version="node_execution_spec_v1",
    )
    db.add(state)
    db.commit()
    try:
        next(gen)
    except StopIteration:
        pass

    # Create + start + execute build.
    build_resp = client.post(
        "/api/v1/builds",
        json={
            "project_id": project_id,
            "session_id": session_id,
            "build_options_json": {"target": "spec_e2e"},
        },
    )
    assert build_resp.status_code == 201, build_resp.text
    build_id = build_resp.json()["id"]
    started = client.post(f"/api/v1/builds/{build_id}/start")
    assert started.status_code == 200
    JobExecutionService().execute_job(started.json()["active_job_id"])

    final_build = client.get(f"/api/v1/builds/{build_id}").json()
    assert final_build["status"] == "completed", (
        f"build expected completed; got {final_build['status']} "
        f"err={final_build.get('error_message')}"
    )

    # Inspect bindings.json from disk — the build wrote it to the
    # executable's source_path. config_hints.task_prompt should now
    # carry the spec-derived content (not the placeholder).
    import json as _json

    exe = client.get(f"/api/v1/executables/{final_build['executable_id']}").json()
    source_path = Path(exe["source_path"])
    bindings_path = source_path / "bindings.json"
    assert bindings_path.exists(), f"bindings.json missing at {bindings_path}"
    bindings_json = _json.loads(bindings_path.read_text())
    coding_binding = next(
        b for b in bindings_json["bindings"]
        if b["selected_capability_id"] == "coding_agent"
    )
    task_prompt = coding_binding["config_hints"]["task_prompt"]
    assert "Write exactly one short report line" in task_prompt, task_prompt
    assert "[completeness]" in task_prompt
    assert "presence" in task_prompt
    assert "Open the output path" in task_prompt
