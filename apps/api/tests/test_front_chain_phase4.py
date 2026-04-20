"""Phase 4 front-chain compile tests.

Coverage matrix:

    C1   compile_context fields come from v2 authority objects only
    C2   compile_context does not read old compiler_result_json
    C3   closure contracts byte-equal preserved through compile
    C4   capability_envelope derived from solvability.capability_matches
    C5   candidate_work_units derived from solvability.required_subgoals (hint)
    C6   v2 path does NOT populate selected_blueprint_type (R5)
    C7   compile creates new chained turn (parent_state_id / turn_number)
    C8   compile 404 on unknown state
    C9   compile 422 on unevaluated state (no solvability)
    C10  AST — compile_context_assembler does not import old compiler services
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.schemas.problem_formulation import (
    InputClosureContract,
    InputSourceCandidate,
    OutputClosureContract,
    PlanningIntentContract,
    ProblemFormulationContract,
)
from app.schemas.solvability import (
    CapabilityMatch,
    GraphFeasibility,
    SolvabilityContract,
    SolvabilitySubgoal,
)
from app.schemas.workflow_native import ExecutionAdmissibilityContract
from app.services.front_chain.compile_context_assembler import (
    ASSEMBLER_VERSION,
    CompileContextAssembler,
)


# ---------------------------------------------------------------------------
# Fixtures — reuse the same closed-contract builders from Phase 3
# ---------------------------------------------------------------------------


def _closed_pf() -> ProblemFormulationContract:
    return ProblemFormulationContract(
        interpreted_problem_kind="repository_task",
        target_object="repository",
        problem_status="solvable",
        current_system_fit="in_scope",
        planning_intent=PlanningIntentContract(
            intent_id="repo_intent",
            label="Repository analysis",
            target_object="repository",
            objective_family="analysis",
        ),
        blocking_axes=[],
        review_axes=[],
        blocking_reasons=[],
    )


def _closed_ic() -> InputClosureContract:
    return InputClosureContract(
        target_object="repository",
        planning_intent_id="repo_intent",
        candidate_sources=[
            InputSourceCandidate(
                candidate_id="src_1",
                locator_kind="url",
                locator_value="https://github.com/example/foo",
                content_kind="repository",
                access_boundary="remote",
                acquisition_kind="checkout",
                confidence=0.9,
                evidence=["detected URL"],
            )
        ],
        resolved_source_kind="repository",
        resolved_source_locator="https://github.com/example/foo",
        resolved_source_modality="remote_url",
        acquisition_path_kind="checkout_then_inventory",
        blocking_axes=[],
        closure_status="input_closed",
    )


def _closed_oc() -> OutputClosureContract:
    return OutputClosureContract(
        deliverable_form="report",
        delivery_target="architecture_report.md",
        output_modality="markdown",
        blocking_axes=[],
        closure_status="output_closed",
    )


def _solvability_with_matches() -> SolvabilityContract:
    return SolvabilityContract(
        problem_kind="repository_task",
        planning_intent_id="repo_intent",
        solvability_status="solvable",
        required_subgoals=[
            SolvabilitySubgoal(
                subgoal_id="sg_1",
                label="Acquire repository",
                description="Clone the target repo",
                subgoal_kind="acquire",
                required_capability_classes=["acquire"],
                input_evidence_kinds=["raw_input"],
                output_evidence_kinds=["source_tree"],
            ),
            SolvabilitySubgoal(
                subgoal_id="sg_2",
                label="Analyze code",
                description="Extract architecture patterns",
                subgoal_kind="analyze",
                required_capability_classes=["analyze", "transform"],
                input_evidence_kinds=["source_tree"],
                output_evidence_kinds=["analysis_result"],
                human_review_required=True,
            ),
            SolvabilitySubgoal(
                subgoal_id="sg_3",
                label="Generate report",
                description="Produce the architecture report",
                subgoal_kind="deliver",
                required_capability_classes=["synthesize"],
                input_evidence_kinds=["analysis_result"],
                output_evidence_kinds=["report"],
                graph_role="delivery",
            ),
        ],
        capability_matches=[
            CapabilityMatch(
                subgoal_id="sg_1",
                required_capability_classes=["acquire"],
                matched_capability_ids=["repo_checkout"],
                match_status="matched",
            ),
            CapabilityMatch(
                subgoal_id="sg_2",
                required_capability_classes=["analyze", "transform"],
                matched_capability_ids=["repo_inventory"],
                match_status="matched",
            ),
            CapabilityMatch(
                subgoal_id="sg_3",
                required_capability_classes=["synthesize"],
                matched_capability_ids=["report_writer"],
                match_status="matched",
            ),
        ],
        graph_feasibility=GraphFeasibility(
            candidate_shape="linear",
            representable=True,
            runtime_supported=True,
            policy_supported=True,
        ),
    )


def _admissibility_supported() -> ExecutionAdmissibilityContract:
    return ExecutionAdmissibilityContract(
        admissibility_status="supported",
        primary_execution_regime="workflow_build",
        max_automation_level="full_workflow",
        build_admission_decision="admit",
        confidence=85,
        summary="Fully solvable",
    )


def _admissibility_analysis_only() -> ExecutionAdmissibilityContract:
    return ExecutionAdmissibilityContract(
        admissibility_status="review_needed",
        primary_execution_regime="analysis_only",
        max_automation_level="analysis_only",
        build_admission_decision="defer_to_plan",
        confidence=40,
        requires_human_review=True,
        summary="Analysis only",
    )


# ---------------------------------------------------------------------------
# C1 — compile_context fields come from v2 authority objects only
# ---------------------------------------------------------------------------


def test_compile_context_from_v2_authority_objects() -> None:
    assembler = CompileContextAssembler()
    result = assembler.assemble(
        state_id="state_123",
        project_id="proj_1",
        requirement_id="req_1",
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        solvability=_solvability_with_matches(),
        execution_admissibility=_admissibility_supported(),
    )
    assert result.compile_context_version == "compile_context_v1"
    assert result.project_id == "proj_1"
    assert result.requirement_id == "req_1"
    assert result.problem_formulation == _closed_pf()
    assert result.input_closure == _closed_ic()
    assert result.output_closure == _closed_oc()
    assert result.solvability == _solvability_with_matches()
    assert result.execution_admissibility == _admissibility_supported()
    assert result.compile_trace["authority_source"] == "front_chain_v2"
    assert result.compile_trace["assembler_version"] == ASSEMBLER_VERSION
    assert result.compile_trace["state_id"] == "state_123"


# ---------------------------------------------------------------------------
# C2 — compile_context does not read old compiler_result_json
#       and v2 path leaves all deprecated fields unset
# ---------------------------------------------------------------------------


def test_v2_assembler_leaves_deprecated_fields_unset() -> None:
    """v2 assembler must not populate the soft-deprecated legacy fields.

    * ``semantic_core`` was hard-removed from the schema entirely.
    * ``selected_blueprint_type`` / ``graph_closure_status`` /
      ``selected_template_id`` are soft-deprecated and the v2 path
      intentionally leaves them as None so the planner doesn't branch on
      legacy authority signals.
    """
    assembler = CompileContextAssembler()
    result = assembler.assemble(
        state_id="s1",
        project_id="p1",
        requirement_id=None,
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        solvability=_solvability_with_matches(),
        execution_admissibility=_admissibility_supported(),
    )
    # Soft-deprecated fields: v2 path must not populate them.
    assert result.selected_blueprint_type is None
    assert result.graph_closure_status is None
    assert result.selected_template_id is None
    # Hard-removed: the field no longer exists on the model.
    assert "semantic_core" not in result.model_fields
    assert not hasattr(result, "semantic_core")


# ---------------------------------------------------------------------------
# C3 — closure contracts byte-equal preserved through compile
# ---------------------------------------------------------------------------


def test_contracts_byte_equal_preserved() -> None:
    pf = _closed_pf()
    ic = _closed_ic()
    oc = _closed_oc()
    solv = _solvability_with_matches()
    adm = _admissibility_supported()

    assembler = CompileContextAssembler()
    result = assembler.assemble(
        state_id="s1",
        project_id="p1",
        requirement_id=None,
        problem_formulation=pf,
        input_closure=ic,
        output_closure=oc,
        solvability=solv,
        execution_admissibility=adm,
    )
    assert result.problem_formulation.model_dump(mode="json") == pf.model_dump(mode="json")
    assert result.input_closure.model_dump(mode="json") == ic.model_dump(mode="json")
    assert result.output_closure.model_dump(mode="json") == oc.model_dump(mode="json")
    assert result.solvability.model_dump(mode="json") == solv.model_dump(mode="json")


# ---------------------------------------------------------------------------
# C4 — capability_envelope from solvability.capability_matches
# ---------------------------------------------------------------------------


def test_capability_envelope_from_capability_matches() -> None:
    assembler = CompileContextAssembler()
    result = assembler.assemble(
        state_id="s1",
        project_id="p1",
        requirement_id=None,
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        solvability=_solvability_with_matches(),
        execution_admissibility=_admissibility_supported(),
    )
    sigs = result.capability_envelope.capability_signatures
    cap_ids = {s.capability_id for s in sigs}
    assert cap_ids == {"repo_checkout", "repo_inventory", "report_writer"}

    # Verify port kinds are derived from subgoal evidence
    checkout_sig = next(s for s in sigs if s.capability_id == "repo_checkout")
    assert checkout_sig.logical_step_kinds == ["acquire"]
    assert checkout_sig.accepted_input_kinds == ["raw_input"]
    assert checkout_sig.produced_output_kinds == ["source_tree"]


# ---------------------------------------------------------------------------
# C5 — candidate_work_units from solvability.required_subgoals (R2: hint)
# ---------------------------------------------------------------------------


def test_candidate_work_units_from_required_subgoals() -> None:
    assembler = CompileContextAssembler()
    result = assembler.assemble(
        state_id="s1",
        project_id="p1",
        requirement_id=None,
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        solvability=_solvability_with_matches(),
        execution_admissibility=_admissibility_supported(),
    )
    units = result.candidate_work_units
    assert len(units) == 3

    # First unit: acquire
    assert units[0].work_unit_id == "hint_1"
    assert units[0].label == "Acquire repository"
    assert units[0].concern_kind == "acquire"
    assert units[0].supporting_subgoal_ids == ["sg_1"]
    assert units[0].parallelism_hint is None  # graph_role is "transform" (default)
    assert units[0].human_gate_hint is None

    # Second unit: analyze (human_review_required)
    assert units[1].work_unit_id == "hint_2"
    assert units[1].human_gate_hint == "required"

    # Third unit: deliver (graph_role=delivery)
    assert units[2].work_unit_id == "hint_3"
    assert units[2].concern_kind == "deliver"
    assert "delivery" in units[2].concern_tags


# ---------------------------------------------------------------------------
# C6 — v2 path does NOT populate selected_blueprint_type (R5)
# ---------------------------------------------------------------------------


def test_v2_does_not_derive_blueprint_type_from_admissibility() -> None:
    """R5: ``selected_blueprint_type`` must NOT be planner authority.

    The v2 assembler intentionally leaves it unset across every admissibility
    regime — workflow_build, analysis_only, human_gated, etc. The planner
    is responsible for deriving its own blueprint shape if needed.
    """
    assembler = CompileContextAssembler()
    for adm in (
        _admissibility_supported(),
        _admissibility_analysis_only(),
        ExecutionAdmissibilityContract(
            admissibility_status="review_needed",
            primary_execution_regime="human_gated",
            max_automation_level="human_gated",
            build_admission_decision="admit_with_review",
            summary="Human gated",
        ),
    ):
        result = assembler.assemble(
            state_id="s",
            project_id="p",
            requirement_id=None,
            problem_formulation=_closed_pf(),
            input_closure=_closed_ic(),
            output_closure=_closed_oc(),
            solvability=_solvability_with_matches(),
            execution_admissibility=adm,
        )
        assert result.selected_blueprint_type is None, (
            f"v2 path must leave selected_blueprint_type unset; got "
            f"{result.selected_blueprint_type} for regime "
            f"{adm.primary_execution_regime}"
        )


# ---------------------------------------------------------------------------
# C7-C9 — integration tests via API
# ---------------------------------------------------------------------------


def _create_project(client) -> dict:
    resp = client.post("/api/v1/projects", json={"name": "Phase 4 Test", "description": "t"})
    assert resp.status_code == 201
    return resp.json()


def _create_session(client, project_id: str) -> dict:
    resp = client.post("/api/v1/front-chain/sessions", json={"project_id": project_id})
    assert resp.status_code == 201
    return resp.json()


def _interpret(client, raw_intent: str) -> dict:
    project = _create_project(client)
    session = _create_session(client, project["id"])
    resp = client.post(
        "/api/v1/front-chain/interpret",
        json={
            "project_id": project["id"],
            "raw_intent": raw_intent,
            "session_id": session["id"],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_compile_creates_chained_turn(client) -> None:
    """C7: compile creates a new row with parent/turn chaining."""
    # Step 1: interpret
    parent = _interpret(
        client,
        "Analyze architecture of a GitHub repo at https://github.com/example/foo",
    )
    state_id_1 = parent["state_id"]

    # Step 2: evaluate
    eval_resp = client.post(f"/api/v1/front-chain/states/{state_id_1}/evaluate")
    assert eval_resp.status_code == 201
    state_id_2 = eval_resp.json()["state_id"]

    # Step 3: compile
    compile_resp = client.post(f"/api/v1/front-chain/states/{state_id_2}/compile")
    assert compile_resp.status_code == 201
    body = compile_resp.json()

    assert body["parent_state_id"] == state_id_2
    assert body["turn_number"] == 3
    assert body["compile_context"] is not None
    assert body["compile_version"] == "front_chain_compile_v1"
    ctx = body["compile_context"]
    assert ctx["compile_context_version"] == "compile_context_v1"
    assert ctx["compile_trace"]["authority_source"] == "front_chain_v2"
    assert len(ctx["candidate_work_units"]) > 0
    # v2 path leaves the soft-deprecated legacy fields unset (R5).
    assert ctx["selected_blueprint_type"] is None
    assert ctx["graph_closure_status"] is None
    assert ctx["selected_template_id"] is None
    # ``semantic_core`` was hard-removed from the schema entirely.
    assert "semantic_core" not in ctx

    # Verify the compiled state is retrievable
    get_resp = client.get(f"/api/v1/front-chain/states/{body['state_id']}")
    assert get_resp.status_code == 200
    fetched = get_resp.json()
    assert fetched["compile_context"] is not None
    assert fetched["compile_version"] == "front_chain_compile_v1"


def test_compile_404_unknown_state(client) -> None:
    """C8: compile returns 404 for unknown state."""
    resp = client.post("/api/v1/front-chain/states/nonexistent_id/compile")
    assert resp.status_code == 404


def test_compile_422_unevaluated_state(client) -> None:
    """C9: compile returns 422 if state lacks solvability/admissibility."""
    parent = _interpret(client, "Something quick")
    state_id = parent["state_id"]

    # Try compile without evaluate first
    compile_resp = client.post(f"/api/v1/front-chain/states/{state_id}/compile")
    assert compile_resp.status_code == 422
    assert "not been evaluated" in compile_resp.json()["detail"]


# ---------------------------------------------------------------------------
# C10 — AST: compile_context_assembler has no old-world imports
# ---------------------------------------------------------------------------

_FORBIDDEN_LEGACY_MODULES = frozenset(
    {
        "app.services.requirement.problem_formulation_service",
        "app.services.requirement.solvability_service",
        "app.services.requirement.requirement_compiler_service",
        "app.services.requirement.requirement_clarification_service",
        "app.services.requirement.problem_formulation_llm_service",
        "app.services.requirement_clarifier_service",
        "app.services.requirement_session_service",
    }
)

_FORBIDDEN_LEGACY_PREFIXES = ("app.services.requirement.compiler",)


def test_compile_context_assembler_has_no_old_world_imports() -> None:
    """C10: The assembler must not import old compiler / requirement services."""
    assembler_path = (
        Path(__file__).resolve().parent.parent
        / "app"
        / "services"
        / "front_chain"
        / "compile_context_assembler.py"
    )
    assert assembler_path.exists(), f"compile_context_assembler.py not found at {assembler_path}"
    source = assembler_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name
                if module_name in _FORBIDDEN_LEGACY_MODULES or any(
                    module_name.startswith(prefix) for prefix in _FORBIDDEN_LEGACY_PREFIXES
                ):
                    violations.append(f"import {module_name}")
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            if module_name in _FORBIDDEN_LEGACY_MODULES or any(
                module_name.startswith(prefix) for prefix in _FORBIDDEN_LEGACY_PREFIXES
            ):
                violations.append(f"from {module_name} import ...")
    assert violations == [], (
        "compile_context_assembler must not import old-world services (R10). "
        f"Violations: {violations}"
    )


# ---------------------------------------------------------------------------
# C6b — compile_constraints reflect admissibility policy
# ---------------------------------------------------------------------------


def test_compile_constraints_reflect_admissibility() -> None:
    assembler = CompileContextAssembler()

    # analysis_only → forbidden side effects
    result = assembler.assemble(
        state_id="s1",
        project_id="p1",
        requirement_id=None,
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        solvability=_solvability_with_matches(),
        execution_admissibility=_admissibility_analysis_only(),
    )
    constraints = result.compile_constraints
    assert "filesystem_write" in constraints.forbidden_side_effect_classes
    assert "act" in constraints.require_human_gate_before
    assert len(constraints.compile_notes) >= 2

    # full_workflow → no forbidden side effects
    result2 = assembler.assemble(
        state_id="s2",
        project_id="p1",
        requirement_id=None,
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        solvability=_solvability_with_matches(),
        execution_admissibility=_admissibility_supported(),
    )
    assert result2.compile_constraints.forbidden_side_effect_classes == []


# ---------------------------------------------------------------------------
# E2E — full chain: interpret -> (answer) -> evaluate -> compile
# ---------------------------------------------------------------------------


def test_full_chain_with_answer_loop(client) -> None:
    """E2E: interpret produces a blocking question, answer resolves it,
    evaluate derives solvability+admissibility, compile assembles context.

    Intent omits "report"/"summary" so the mock interpreter flags
    deliverable_form as blocking. Submitting an answer drains the gap.
    """
    # ---- Step 1: interpret (turn 1) ----
    parent = _interpret(
        client,
        "Analyze the repository at https://github.com/example/foo",
    )
    state_id_v1 = parent["state_id"]
    assert parent["turn_number"] == 1
    assert parent["parent_state_id"] is None

    # Verify deliverable_form is the open blocking axis
    questions = parent["top_questions"]
    axes = {q["axis"] for q in questions}
    assert "deliverable_form" in axes, (
        f"Expected deliverable_form in top_questions; got {axes}"
    )
    deliverable_q = next(q for q in questions if q["axis"] == "deliverable_form")

    # Output closure should not be closed yet
    assert parent["output_closure"]["closure_status"] != "output_closed"

    # ---- Step 2: answer (turn 2) ----
    answer_resp = client.post(
        f"/api/v1/front-chain/states/{state_id_v1}/answer",
        json={
            "answers": [
                {
                    "question_id": deliverable_q["question_id"],
                    "answer_text": "report",
                }
            ]
        },
    )
    assert answer_resp.status_code == 201, answer_resp.text
    state_v2 = answer_resp.json()
    state_id_v2 = state_v2["state_id"]
    assert state_v2["turn_number"] == 2
    assert state_v2["parent_state_id"] == state_id_v1

    # deliverable_form should be drained from new top_questions
    new_axes = {q["axis"] for q in state_v2["top_questions"]}
    assert "deliverable_form" not in new_axes, (
        f"deliverable_form should be resolved; remaining axes: {new_axes}"
    )

    # ---- Step 3: evaluate (turn 3) ----
    eval_resp = client.post(f"/api/v1/front-chain/states/{state_id_v2}/evaluate")
    assert eval_resp.status_code == 201
    state_v3 = eval_resp.json()
    state_id_v3 = state_v3["state_id"]
    assert state_v3["turn_number"] == 3
    assert state_v3["parent_state_id"] == state_id_v2

    # Closure contracts must be byte-equal vs the answered state (Phase 3 R)
    assert state_v3["problem_formulation"] == state_v2["problem_formulation"]
    assert state_v3["input_closure"] == state_v2["input_closure"]
    assert state_v3["output_closure"] == state_v2["output_closure"]

    # Evaluate must produce solvability + admissibility with exactly 1 LLM call
    assert state_v3["solvability"] is not None
    assert state_v3["execution_admissibility"] is not None
    assert state_v3["solvability_llm_trace"]["call_count"] == 1
    subgoal_kinds = {sg["subgoal_kind"] for sg in state_v3["solvability"]["required_subgoals"]}
    assert "acquire" in subgoal_kinds
    assert "analyze" in subgoal_kinds  # repository target
    assert "synthesize" in subgoal_kinds  # report deliverable

    # ---- Step 4: compile (turn 4) ----
    compile_resp = client.post(f"/api/v1/front-chain/states/{state_id_v3}/compile")
    assert compile_resp.status_code == 201
    state_v4 = compile_resp.json()
    assert state_v4["turn_number"] == 4
    assert state_v4["parent_state_id"] == state_id_v3

    # Compile must not mutate closure contracts
    assert state_v4["problem_formulation"] == state_v3["problem_formulation"]
    assert state_v4["input_closure"] == state_v3["input_closure"]
    assert state_v4["output_closure"] == state_v3["output_closure"]

    # Compile must not re-introduce blocking axes that the answer loop drained.
    # (This is the safety net against compile silently injecting planner/build
    # concerns back into the front chain.)
    pf = state_v4["problem_formulation"]
    ic = state_v4["input_closure"]
    oc = state_v4["output_closure"]
    assert "deliverable_form" not in (pf.get("blocking_axes") or [])
    assert "deliverable_form" not in (ic.get("blocking_axes") or [])
    assert "deliverable_form" not in (oc.get("blocking_axes") or [])
    # And output closure should remain closed (or at least not be re-opened)
    assert oc["closure_status"] != "output_clarification_required"

    # CompileContext shape sanity
    ctx = state_v4["compile_context"]
    assert ctx is not None
    assert ctx["compile_trace"]["authority_source"] == "front_chain_v2"
    assert ctx["compile_trace"]["state_id"] == state_id_v3
    # Work units count matches subgoals (R2: hint material)
    assert len(ctx["candidate_work_units"]) == len(
        state_v3["solvability"]["required_subgoals"]
    )


def test_full_chain_fully_closed_skips_answer(client) -> None:
    """E2E: when interpret already closes everything, answer step is skipped.

    Intent includes both URL (input closes) and "report" keyword (output closes)
    so the mock interpreter produces 0 top_questions.  Chain becomes:
    interpret (turn 1) -> evaluate (turn 2) -> compile (turn 3).
    """
    # ---- Step 1: interpret (turn 1) ----
    parent = _interpret(
        client,
        "Analyze the repository at https://github.com/example/foo and produce a markdown report",
    )
    state_id_v1 = parent["state_id"]
    assert parent["turn_number"] == 1

    # Mock interpreter should fully close all axes — 0 questions
    assert parent["top_questions"] == [], (
        f"Expected fully closed interpret; got questions: {parent['top_questions']}"
    )
    assert parent["problem_formulation"]["problem_status"] == "solvable"
    assert parent["input_closure"]["closure_status"] == "input_closed"
    assert parent["output_closure"]["closure_status"] == "output_closed"

    # ---- Step 2: evaluate (turn 2) — skip answer ----
    eval_resp = client.post(f"/api/v1/front-chain/states/{state_id_v1}/evaluate")
    assert eval_resp.status_code == 201
    state_v2 = eval_resp.json()
    state_id_v2 = state_v2["state_id"]
    assert state_v2["turn_number"] == 2
    assert state_v2["parent_state_id"] == state_id_v1

    # ---- Step 3: compile (turn 3) ----
    compile_resp = client.post(f"/api/v1/front-chain/states/{state_id_v2}/compile")
    assert compile_resp.status_code == 201
    state_v3 = compile_resp.json()
    assert state_v3["turn_number"] == 3
    assert state_v3["parent_state_id"] == state_id_v2

    # Compile must not re-open any closure — everything stays closed end-to-end
    assert state_v3["output_closure"]["closure_status"] == "output_closed"
    assert state_v3["input_closure"]["closure_status"] == "input_closed"
    assert state_v3["problem_formulation"]["problem_status"] == "solvable"

    ctx = state_v3["compile_context"]
    assert ctx is not None
    assert ctx["compile_trace"]["authority_source"] == "front_chain_v2"
