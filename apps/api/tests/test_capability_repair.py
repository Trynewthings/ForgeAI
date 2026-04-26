"""Task 7 — capability repair tests.

Coverage:

    R1  specialised_class_mismatch on transform → rebound to coding_agent
    R2  io_shape_mismatch (filesystem_read on directory) → rebound to coding_agent
    R3  missing_capability_selection → rebound to coding_agent
    R4  Already-correct generalist pick is NOT touched
    R5  Already-correct specialist pick is NOT touched
    R6  Persist node bound_capability mutation marks selection_source="capability_repair"
    R7  No-spec node is recorded as unrepaired (not silently skipped)
    R8  CapabilityMismatchValidator detects io_shape_mismatch on directory locator
    R9  Build path: repair attempts surfaced via _enforce_spec_gates outcome
    R10 Repair report carries per-node audit (issue_kind, original/new, source)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.schemas.node_execution_spec import (
    EdgeContractReport,
    ExecutionConstraints,
    FailurePolicy,
    NodeExecutionSpec,
    NodeExecutionSpecBundle,
    NodeInputContract,
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
from app.services.node_spec.capability_mismatch_validator import (
    CapabilityMismatchIssue,
    validate_capability_mismatch,
)
from app.services.workflow_planner.capability_repair import (
    SpecReconciliation,
    repair_capability_picks,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _spec(
    node_id: str,
    *,
    runtime_class: str = "bounded_transform",
) -> NodeExecutionSpec:
    return NodeExecutionSpec(
        node_id=node_id,
        semantic_goal=f"do {node_id}",
        input_contract=NodeInputContract(),
        output_contract=NodeOutputContract(
            primary_output=NodeOutputSlot(
                name="out",
                kind="tabular_records",
                schema_hint=[],
            ),
        ),
        business_rules=[],
        acceptance_criteria=[],
        execution_constraints=ExecutionConstraints(
            expected_runtime_class=runtime_class,
        ),
        failure_policy=FailurePolicy(),
        verifier_hints=VerifierHints(),
        implementation_sketch=[],
    )


def _node(
    *,
    draft_node_id: str,
    capability_id: str | None,
    semantic_type: str = "transform",
    upstream: list | None = None,
    candidate_ids: list[str] | None = None,
):
    meta = {"step_name": draft_node_id}
    if capability_id is not None or candidate_ids is not None:
        meta["bound_capability"] = {
            "selected_capability_id": capability_id,
            "candidate_ids": list(
                candidate_ids if candidate_ids is not None else (
                    [capability_id] if capability_id else []
                )
            ),
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
        candidate_capability_ids=list(
            candidate_ids if candidate_ids is not None else (
                [capability_id] if capability_id else []
            )
        ),
        upstream_dependencies=upstream or [],
        planner_metadata=meta,
    )


def _planner(nodes, edges=None):
    return WorkflowPlannerOutput(
        node_drafts=nodes,
        edge_drafts=[
            WorkflowPlannerEdgeDraft(
                draft_edge_id=f"e_{src}__{dst}",
                from_node_id=src,
                to_node_id=dst,
            )
            for (src, dst) in (edges or [])
        ],
        entry_node_ids=[nodes[0].draft_node_id],
        exit_node_ids=[nodes[-1].draft_node_id],
    )


def _cc_with_input_dir(input_dir: Path) -> CompileContextResponse:
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
            delivery_target=str(input_dir.parent / "output" / "out.csv"),
            output_modality="csv",
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


# ---------------------------------------------------------------------------
# R1-R3: rebind paths
# ---------------------------------------------------------------------------


def test_r1_specialised_class_mismatch_repairs_to_coding_agent():
    nodes = [
        _node(
            draft_node_id="n_compute",
            capability_id="filesystem_read",
            semantic_type="transform",
        )
    ]
    planner = _planner(nodes)
    issues = [
        CapabilityMismatchIssue(
            node_id="n_compute",
            selected_capability_id="filesystem_read",
            expected_runtime_class="bounded_transform",
            allowed_runtime_classes=["bounded_acquire"],
            kind="specialised_class_mismatch",
            detail="t",
        )
    ]
    report = repair_capability_picks(
        planner_output=planner,
        specs=[_spec("n_compute", runtime_class="bounded_transform")],
        issues=issues,
    )
    assert report.made_any_change
    assert len(report.attempts) == 1
    attempt = report.attempts[0]
    assert attempt.original_capability == "filesystem_read"
    assert attempt.new_capability == "coding_agent"
    assert attempt.repair_source == "deterministic_generalist"
    bound = planner.node_drafts[0].planner_metadata["bound_capability"]
    assert bound["selected_capability_id"] == "coding_agent"
    assert bound["selection_source"] == "capability_repair"
    # And coding_agent was added to candidate_ids if it wasn't there.
    assert "coding_agent" in bound["candidate_ids"]


def test_r2_io_shape_mismatch_repairs_to_coding_agent():
    nodes = [
        _node(
            draft_node_id="n_acquire",
            capability_id="filesystem_read",
            semantic_type="acquire",
        )
    ]
    planner = _planner(nodes)
    issues = [
        CapabilityMismatchIssue(
            node_id="n_acquire",
            selected_capability_id="filesystem_read",
            expected_runtime_class="bounded_acquire",
            allowed_runtime_classes=[],
            kind="io_shape_mismatch",
            detail="directory locator on single-file primitive",
            locator="/tmp/some_dir",
            locator_shape="directory",
        )
    ]
    report = repair_capability_picks(
        planner_output=planner,
        specs=[_spec("n_acquire", runtime_class="bounded_acquire")],
        issues=issues,
    )
    assert report.made_any_change
    a = report.attempts[0]
    # Even though filesystem_read IS class-compatible with
    # bounded_acquire, io_shape_mismatch forbids picking another
    # single-file primitive — must fall to coding_agent.
    assert a.new_capability == "coding_agent"


def test_r3_missing_capability_selection_repairs_to_coding_agent():
    nodes = [
        _node(
            draft_node_id="n_orphan",
            capability_id=None,
            semantic_type="transform",
        )
    ]
    nodes[0].planner_metadata = {"step_name": "n_orphan"}  # no bound_capability
    planner = _planner(nodes)
    issues = [
        CapabilityMismatchIssue(
            node_id="n_orphan",
            selected_capability_id=None,
            expected_runtime_class="bounded_transform",
            allowed_runtime_classes=[],
            kind="missing_capability_selection",
            detail="no pick",
        )
    ]
    report = repair_capability_picks(
        planner_output=planner,
        specs=[_spec("n_orphan", runtime_class="bounded_transform")],
        issues=issues,
    )
    assert report.made_any_change
    a = report.attempts[0]
    assert a.new_capability == "coding_agent"
    bound = planner.node_drafts[0].planner_metadata["bound_capability"]
    assert bound["selected_capability_id"] == "coding_agent"


# ---------------------------------------------------------------------------
# R4-R5: leave already-correct picks alone (nothing in issues, nothing changed)
# ---------------------------------------------------------------------------


def test_r4_no_change_when_no_issues():
    nodes = [
        _node(
            draft_node_id="n",
            capability_id="coding_agent",
            semantic_type="transform",
        )
    ]
    planner = _planner(nodes)
    report = repair_capability_picks(
        planner_output=planner,
        specs=[_spec("n", runtime_class="bounded_transform")],
        issues=[],
    )
    assert not report.attempts
    assert not report.made_any_change


def test_r5_specialist_correctly_used_no_issue_no_repair():
    """validate_capability_mismatch shouldn't flag a properly-bound
    specialist; consequently repair_capability_picks gets an empty
    issue list and makes no changes."""
    nodes = [
        _node(
            draft_node_id="n_acq",
            capability_id="filesystem_read",
            semantic_type="acquire",
        )
    ]
    planner = _planner(nodes)
    cap_report = validate_capability_mismatch(
        planner_output=planner,
        specs=[_spec("n_acq", runtime_class="bounded_acquire")],
        skipped_node_ids=[],
    )
    assert not cap_report.issues
    repair_report = repair_capability_picks(
        planner_output=planner,
        specs=[_spec("n_acq", runtime_class="bounded_acquire")],
        issues=cap_report.issues,
    )
    assert not repair_report.made_any_change


# ---------------------------------------------------------------------------
# R6-R7: audit trail + unrepaired tracking
# ---------------------------------------------------------------------------


def test_r6_repaired_node_carries_audit_record_in_planner_metadata():
    nodes = [
        _node(
            draft_node_id="n",
            capability_id="filesystem_read",
            semantic_type="transform",
        )
    ]
    planner = _planner(nodes)
    issues = [
        CapabilityMismatchIssue(
            node_id="n",
            selected_capability_id="filesystem_read",
            expected_runtime_class="bounded_transform",
            allowed_runtime_classes=["bounded_acquire"],
            kind="specialised_class_mismatch",
            detail="t",
        )
    ]
    repair_capability_picks(
        planner_output=planner,
        specs=[_spec("n", runtime_class="bounded_transform")],
        issues=issues,
    )
    record = planner.node_drafts[0].planner_metadata.get(
        "capability_repair_record"
    )
    assert record is not None
    assert record["original_capability"] == "filesystem_read"
    assert record["new_capability"] == "coding_agent"
    assert record["issue_kind"] == "specialised_class_mismatch"
    assert record["expected_runtime_class"] == "bounded_transform"


def test_r7_unrepairable_node_recorded_when_spec_missing():
    nodes = [
        _node(
            draft_node_id="n",
            capability_id="filesystem_read",
            semantic_type="transform",
        )
    ]
    planner = _planner(nodes)
    issues = [
        CapabilityMismatchIssue(
            node_id="n",
            selected_capability_id="filesystem_read",
            expected_runtime_class="bounded_transform",
            allowed_runtime_classes=["bounded_acquire"],
            kind="specialised_class_mismatch",
            detail="t",
        )
    ]
    # No specs supplied → can't repair (no expected_runtime_class to
    # consult beyond the issue itself, but we still need the spec object
    # for the lookup).
    report = repair_capability_picks(
        planner_output=planner,
        specs=[],
        issues=issues,
    )
    assert "n" in report.unrepaired_node_ids
    a = report.attempts[0]
    assert a.repair_source == "no_repair_possible"
    assert a.new_capability is None


# ---------------------------------------------------------------------------
# R8: validator detects io_shape_mismatch on directory locator
# ---------------------------------------------------------------------------


def test_r8_validator_flags_io_shape_mismatch_on_directory(tmp_path: Path):
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "a.csv").write_text("a\n", encoding="utf-8")

    nodes = [
        _node(
            draft_node_id="n_acquire",
            capability_id="filesystem_read",
            semantic_type="acquire",
        )
    ]
    planner = _planner(nodes)
    cc = _cc_with_input_dir(input_dir)
    report = validate_capability_mismatch(
        planner_output=planner,
        specs=[_spec("n_acquire", runtime_class="bounded_acquire")],
        skipped_node_ids=[],
        compile_context=cc,
    )
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.kind == "io_shape_mismatch"
    assert issue.locator_shape == "directory"
    assert issue.selected_capability_id == "filesystem_read"


def test_r8b_validator_does_not_flag_io_shape_when_locator_is_file(tmp_path: Path):
    input_file = tmp_path / "in.csv"
    input_file.write_text("a\n", encoding="utf-8")

    nodes = [
        _node(
            draft_node_id="n_acquire",
            capability_id="filesystem_read",
            semantic_type="acquire",
        )
    ]
    planner = _planner(nodes)
    # cc with a FILE locator — no io_shape issue.
    cc = _cc_with_input_dir(input_file)
    report = validate_capability_mismatch(
        planner_output=planner,
        specs=[_spec("n_acquire", runtime_class="bounded_acquire")],
        skipped_node_ids=[],
        compile_context=cc,
    )
    assert not report.issues


# ---------------------------------------------------------------------------
# R9-R10: build-side repair surface
# ---------------------------------------------------------------------------


def test_r9_enforce_spec_gates_returns_repair_attempts(tmp_path: Path):
    """When the gate finds a fixable issue, it returns the repair trace
    so the caller can persist + log."""
    from app.services.build.build_execution_service import _enforce_spec_gates

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "a.csv").write_text("a\n", encoding="utf-8")

    nodes = [
        _node(
            draft_node_id="n_acquire",
            capability_id="filesystem_read",
            semantic_type="acquire",
        ),
        _node(
            draft_node_id="n_compute",
            capability_id="validate_schema",  # wrong: spec wants transform
            semantic_type="transform",
        ),
    ]
    planner = _planner(nodes)
    bundle = NodeExecutionSpecBundle(
        specs=[
            _spec("n_acquire", runtime_class="bounded_acquire"),
            _spec("n_compute", runtime_class="bounded_transform"),
        ],
        skipped_node_ids=[],
        edge_contract_report=EdgeContractReport(checked_edges=0),
    )
    cc = _cc_with_input_dir(input_dir)
    outcome = _enforce_spec_gates(
        bundle=bundle, planner_output=planner, compile_context=cc
    )
    assert outcome["planner_output_modified"] is True
    # Two issues, both repaired (n_acquire has io_shape because IC is
    # a directory; n_compute has class mismatch).
    assert len(outcome["repair_attempts"]) == 2
    by_node = {a["node_id"]: a for a in outcome["repair_attempts"]}
    assert by_node["n_acquire"]["new_capability"] == "coding_agent"
    assert by_node["n_acquire"]["issue_kind"] == "io_shape_mismatch"
    assert by_node["n_compute"]["new_capability"] == "coding_agent"
    assert by_node["n_compute"]["issue_kind"] == "specialised_class_mismatch"


def test_r10_enforce_spec_gates_blocks_when_no_repair_possible():
    """If the candidate pool has no compatible capability AND no
    generalist, repair fails and the gate blocks the build."""
    from app.services.build.build_execution_service import _enforce_spec_gates
    from app.services.workflow_planner import capability_repair as repair_mod

    nodes = [
        _node(
            draft_node_id="n",
            capability_id="filesystem_read",
            semantic_type="transform",
            candidate_ids=["filesystem_read"],
        )
    ]
    planner = _planner(nodes)
    bundle = NodeExecutionSpecBundle(
        specs=[_spec("n", runtime_class="bounded_transform")],
        skipped_node_ids=[],
        edge_contract_report=EdgeContractReport(checked_edges=0),
    )
    # Force the repair module to NOT inject coding_agent — simulate
    # a slice where the generalist isn't available.
    import app.services.workflow_planner.capability_repair as cap_repair

    original = cap_repair._DEFAULT_GENERALIST
    cap_repair._DEFAULT_GENERALIST = "nonexistent_generalist"
    try:
        with pytest.raises(RuntimeError, match="capability_mismatch"):
            _enforce_spec_gates(bundle=bundle, planner_output=planner)
    finally:
        cap_repair._DEFAULT_GENERALIST = original


# ---------------------------------------------------------------------------
# R11-R15: spec reconciliation — when rebind crosses
# "in-memory cap → file-writing cap", spec must also adjust so the
# resulting task_prompt no longer encodes the old in-memory mental
# model.
# ---------------------------------------------------------------------------


def test_r11_rebind_filesystem_read_to_coding_agent_appends_persistence_rule():
    """The classic case: planner picked filesystem_read for a transform
    (which is class-mismatched), repair rebinds to coding_agent. Because
    filesystem_read does not require disk persistence and coding_agent
    DOES, the spec needs a [persistence] BusinessRule + sketch line so
    the task_prompt makes the file-write obligation impossible to miss."""
    nodes = [
        _node(
            draft_node_id="n_compute",
            capability_id="filesystem_read",
            semantic_type="transform",
        )
    ]
    planner = _planner(nodes)
    spec = _spec("n_compute", runtime_class="bounded_transform")
    # Confirm the test fixture starts with no persistence rules and
    # no implementation_sketch lines.
    assert all(r.category != "persistence" for r in spec.business_rules)
    assert spec.implementation_sketch == []
    issues = [
        CapabilityMismatchIssue(
            node_id="n_compute",
            selected_capability_id="filesystem_read",
            expected_runtime_class="bounded_transform",
            allowed_runtime_classes=["bounded_acquire"],
            kind="specialised_class_mismatch",
            detail="t",
        )
    ]
    report = repair_capability_picks(
        planner_output=planner,
        specs=[spec],
        issues=issues,
    )
    assert report.made_any_change
    attempt = report.attempts[0]
    assert attempt.new_capability == "coding_agent"
    # Reconciliation MUST have fired.
    assert attempt.spec_reconciliation is not None
    rec = attempt.spec_reconciliation
    assert rec.old_capability == "filesystem_read"
    assert rec.new_capability == "coding_agent"
    assert rec.added_business_rule_id == "capability_repair_persistence_n_compute"
    # Spec mutation visible: persistence rule appended.
    persistence_rules = [
        r for r in spec.business_rules if r.category == "persistence"
    ]
    assert len(persistence_rules) == 1
    assert "MUST persist primary_output" in persistence_rules[0].rule
    # Sketch line appended.
    assert len(spec.implementation_sketch) == 1
    assert "locator_hint" in spec.implementation_sketch[0]
    # Audit record on the planner node also stamps spec_reconciled.
    record = planner.node_drafts[0].planner_metadata["capability_repair_record"]
    assert record["spec_reconciled"] is True


def test_r12_rebind_validate_schema_to_coding_agent_appends_persistence_rule():
    """validate_schema is also an in-memory cap (emits validation result
    via state). Rebind to coding_agent must trigger reconciliation."""
    nodes = [
        _node(
            draft_node_id="n_validate",
            capability_id="validate_schema",
            semantic_type="transform",
        )
    ]
    planner = _planner(nodes)
    spec = _spec("n_validate", runtime_class="bounded_transform")
    issues = [
        CapabilityMismatchIssue(
            node_id="n_validate",
            selected_capability_id="validate_schema",
            expected_runtime_class="bounded_transform",
            allowed_runtime_classes=["bounded_verify"],
            kind="specialised_class_mismatch",
            detail="t",
        )
    ]
    report = repair_capability_picks(
        planner_output=planner,
        specs=[spec],
        issues=issues,
    )
    rec = report.attempts[0].spec_reconciliation
    assert rec is not None
    assert rec.old_capability == "validate_schema"
    assert rec.new_capability == "coding_agent"
    assert any(r.category == "persistence" for r in spec.business_rules)


def test_r13_rebind_bumps_max_iterations_to_at_least_two():
    """bounded_acquire / transform specs default to max_iterations=1.
    After rebinding to coding_agent the agent has a stricter file-write
    contract; one shot leaves no room to learn from policy_violation
    feedback. Reconciliation bumps the budget to >=2."""
    nodes = [
        _node(
            draft_node_id="n",
            capability_id="filesystem_read",
            semantic_type="acquire",
        )
    ]
    planner = _planner(nodes)
    spec = _spec("n", runtime_class="bounded_acquire")
    assert spec.execution_constraints.max_iterations == 1  # default
    issues = [
        CapabilityMismatchIssue(
            node_id="n",
            selected_capability_id="filesystem_read",
            expected_runtime_class="bounded_acquire",
            allowed_runtime_classes=[],
            kind="io_shape_mismatch",
            detail="d",
            locator="/tmp/dir",
            locator_shape="directory",
        )
    ]
    report = repair_capability_picks(
        planner_output=planner,
        specs=[spec],
        issues=issues,
    )
    rec = report.attempts[0].spec_reconciliation
    assert rec is not None
    assert rec.bumped_max_iterations_from == 1
    assert rec.bumped_max_iterations_to == 2
    # Spec really got mutated.
    assert spec.execution_constraints.max_iterations == 2


def test_r14_no_reconciliation_when_both_caps_already_write_files():
    """Hypothetical: planner picked filesystem_write but the spec wants
    a different capability that ALSO writes files. Reconciliation
    should NOT fire — the spec was authored under a file-writing
    mental model already."""
    # Inject a fake "file-writing" cap into the allowed set so the
    # test doesn't depend on real capability tables. We patch the
    # module-level set directly.
    import app.services.workflow_planner.capability_repair as mod

    original = mod._CAPS_THAT_MUST_WRITE_FILE
    mod._CAPS_THAT_MUST_WRITE_FILE = frozenset(
        list(original) + ["fake_other_writer"]
    )
    try:
        nodes = [
            _node(
                draft_node_id="n",
                capability_id="fake_other_writer",
                semantic_type="transform",
                candidate_ids=["fake_other_writer", "coding_agent"],
            )
        ]
        planner = _planner(nodes)
        spec = _spec("n", runtime_class="bounded_transform")
        # Pre-condition: spec already has no persistence rule.
        original_rule_count = len(spec.business_rules)
        original_sketch_count = len(spec.implementation_sketch)
        issues = [
            CapabilityMismatchIssue(
                node_id="n",
                selected_capability_id="fake_other_writer",
                expected_runtime_class="bounded_transform",
                allowed_runtime_classes=[],
                kind="missing_capability_selection",
                detail="...",
            )
        ]
        report = repair_capability_picks(
            planner_output=planner,
            specs=[spec],
            issues=issues,
        )
        # Rebind happened (fake_other_writer → coding_agent).
        attempt = report.attempts[0]
        assert attempt.new_capability == "coding_agent"
        # But reconciliation should NOT have fired, because
        # fake_other_writer was already a file-writer.
        assert attempt.spec_reconciliation is None
        assert len(spec.business_rules) == original_rule_count
        assert len(spec.implementation_sketch) == original_sketch_count
    finally:
        mod._CAPS_THAT_MUST_WRITE_FILE = original


def test_r15_enforce_spec_gates_emits_spec_reconciliations_in_outcome():
    """When _enforce_spec_gates runs and triggers reconciliation, the
    outcome dict surfaces spec_reconciliations alongside repair_attempts
    so build_execution_service can persist both into the planner_metadata
    audit trail."""
    from app.services.build.build_execution_service import _enforce_spec_gates

    nodes = [
        _node(
            draft_node_id="n_compute",
            capability_id="validate_schema",
            semantic_type="transform",
        ),
    ]
    planner = _planner(nodes)
    bundle = NodeExecutionSpecBundle(
        specs=[_spec("n_compute", runtime_class="bounded_transform")],
        skipped_node_ids=[],
        edge_contract_report=EdgeContractReport(checked_edges=0),
    )
    outcome = _enforce_spec_gates(bundle=bundle, planner_output=planner)
    assert outcome["planner_output_modified"] is True
    assert len(outcome["repair_attempts"]) == 1
    assert outcome["repair_attempts"][0]["spec_reconciled"] is True
    # spec_reconciliations is the audit-trail-friendly serialization.
    recs = outcome["spec_reconciliations"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["node_id"] == "n_compute"
    assert rec["old_capability"] == "validate_schema"
    assert rec["new_capability"] == "coding_agent"
    assert rec["bumped_max_iterations_from"] == 1
    assert rec["bumped_max_iterations_to"] == 2
    assert "MUST persist primary_output" in (rec["added_business_rule_text"] or "")
