"""Phase 3 front-chain evaluation tests.

Coverage matrix (Phase 3 R-list):

    E1   solvability_llm produces proof-style candidate (no graph_role)
    E2   validator strips graph_role attempts (R4 layer boundary)
    E3   validator strips forbidden planner fields (edges / node_ordering / ...)
    E4   validator drops unknown capability_ids
    E5   validator derives graph_feasibility descriptively
    E6   admissibility: full_workflow when all satisfied + closed
    E7   admissibility: plan_only when unmatched capability
    E8   admissibility: human_gated when partial capability
    E9   admissibility: human_gated when subgoal requires human review
    E10  admissibility: review_needed when closure still blocking
    E11  R3 — solvable does not imply admissible (distinct axes)
    E12  evaluate does NOT mutate closure contracts (byte-equal)
    E13  evaluate creates a new chained turn row (parent/turn++)
    E14  exactly 1 LLM call per evaluate
    E15  AST — solvability_llm_service has no forbidden imports
    E16  AST — solvability_validator is deterministic (no LLM coupling)
    E17  AST — execution_admissibility_service is zero-LLM
    E18  evaluate response carries solvability + admissibility + trace
    E19  evaluate 404 on unknown parent state
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.schemas.front_chain import (
    FrontChainClarificationQuestion,
    MergedGapSet,
)
from app.schemas.problem_formulation import (
    InputClosureContract,
    InputClosureRequirement,
    InputSourceCandidate,
    OutputClosureContract,
    PlanningIntentContract,
    ProblemFormulationContract,
)
from app.schemas.solvability import (
    CapabilityMatch,
    SolvabilityLLMCandidate,
    SolvabilityLLMSubgoal,
)
from app.services.front_chain.execution_admissibility_service import (
    ExecutionAdmissibilityService,
)
from app.services.front_chain.solvability_llm_service import SolvabilityLLMService
from app.services.front_chain.solvability_validator import (
    SolvabilityValidator,
)


# ---------------------------------------------------------------------------
# Helpers — fixture builders
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


def _full_capability_envelope() -> list[dict]:
    return [
        {"capability_id": "repo_checkout", "supported_step_kinds": ["acquire"]},
        {"capability_id": "repo_inventory", "supported_step_kinds": ["analyze", "transform"]},
        {"capability_id": "report_writer", "supported_step_kinds": ["synthesize", "summarize"]},
        {"capability_id": "fs_writer", "supported_step_kinds": ["deliver", "persist"]},
    ]


def _blocking_pf() -> ProblemFormulationContract:
    return ProblemFormulationContract(
        interpreted_problem_kind="generic_task",
        target_object=None,
        problem_status="underdetermined",
        current_system_fit="partially_in_scope",
        blocking_axes=["target_object"],
        blocking_reasons=["Missing target_object"],
    )


# ---------------------------------------------------------------------------
# E1 — solvability_llm produces proof-style candidate (no graph_role)
# ---------------------------------------------------------------------------


def test_solvability_llm_candidate_has_no_graph_role_field() -> None:
    candidate_fields = set(SolvabilityLLMSubgoal.model_fields.keys())
    assert "graph_role" not in candidate_fields, (
        "SolvabilityLLMSubgoal must not carry graph_role (R4 — solvability is "
        "proof-oriented, planner owns graph topology)"
    )


def test_solvability_llm_service_emits_proof_subgoals() -> None:
    service = SolvabilityLLMService()
    result = service.propose(
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        capability_envelope=_full_capability_envelope(),
    )
    assert result.trace_summary.call_count == 1
    assert len(result.candidate.required_subgoals) > 0
    # Mock interpreter produces acquire→analyze→synthesize→deliver chain.
    kinds = {sg.subgoal_kind for sg in result.candidate.required_subgoals}
    assert "acquire" in kinds


# ---------------------------------------------------------------------------
# E2 — validator strips graph_role attempts
# ---------------------------------------------------------------------------


def test_validator_defaults_graph_role_on_subgoals() -> None:
    """Even if downstream planners care about graph_role, the solvability
    validator must default it to 'transform' — it never lifts the LLM's
    hint into planner authority."""
    candidate = SolvabilityLLMCandidate(
        required_subgoals=[
            SolvabilityLLMSubgoal(
                subgoal_id="sg_1",
                label="Acquire",
                description="Fetch the source",
                subgoal_kind="acquire",
                required_capability_classes=["acquire"],
            ),
        ],
        capability_matches=[
            CapabilityMatch(
                subgoal_id="sg_1",
                required_capability_classes=["acquire"],
                matched_capability_ids=["repo_checkout"],
                match_status="matched",
            ),
        ],
    )
    result = SolvabilityValidator().validate_and_produce(
        candidate=candidate,
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        known_capability_ids=["repo_checkout"],
    )
    assert len(result.contract.required_subgoals) == 1
    assert result.contract.required_subgoals[0].graph_role == "transform"


# ---------------------------------------------------------------------------
# E3 — validator strips forbidden planner fields on raw payload
# ---------------------------------------------------------------------------


def test_validator_strips_forbidden_planner_fields_from_raw_payload() -> None:
    candidate = SolvabilityLLMCandidate(
        required_subgoals=[
            SolvabilityLLMSubgoal(
                subgoal_id="sg_1",
                label="Acquire",
                description="x",
                subgoal_kind="acquire",
                required_capability_classes=["acquire"],
            )
        ],
    )
    # Simulate a raw LLM payload that leaked planner-structure fields.
    raw_payload = {
        "required_subgoals": [],
        "edges": [{"from": "sg_1", "to": "sg_2"}],
        "node_drafts": [{"id": "n1"}],
        "compatibility_step_refs": ["step_a"],
        "execution_order": ["sg_1", "sg_2"],
    }
    result = SolvabilityValidator().validate_and_produce(
        candidate=candidate,
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        raw_candidate_payload=raw_payload,
    )
    forbidden_kinds = {i.kind for i in result.issues if i.kind == "forbidden_field"}
    assert forbidden_kinds == {"forbidden_field"}
    stripped_axes = {i.axis for i in result.issues if i.kind == "forbidden_field"}
    assert "edges" in stripped_axes
    assert "compatibility_step_refs" in stripped_axes
    assert "execution_order" in stripped_axes
    assert "edges" not in raw_payload
    assert "compatibility_step_refs" not in raw_payload


# ---------------------------------------------------------------------------
# E4 — validator drops unknown capability ids
# ---------------------------------------------------------------------------


def test_validator_drops_unknown_capability_ids() -> None:
    candidate = SolvabilityLLMCandidate(
        required_subgoals=[
            SolvabilityLLMSubgoal(
                subgoal_id="sg_1",
                label="x",
                description="y",
                subgoal_kind="acquire",
                required_capability_classes=["acquire"],
            )
        ],
        capability_matches=[
            CapabilityMatch(
                subgoal_id="sg_1",
                required_capability_classes=["acquire"],
                matched_capability_ids=["repo_checkout", "hallucinated_cap"],
                match_status="matched",
            ),
        ],
    )
    result = SolvabilityValidator().validate_and_produce(
        candidate=candidate,
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        known_capability_ids=["repo_checkout"],
    )
    match = result.contract.capability_matches[0]
    assert match.matched_capability_ids == ["repo_checkout"]
    assert any(i.kind == "unknown_capability_id" for i in result.issues)


# ---------------------------------------------------------------------------
# E5 — graph_feasibility is descriptive only
# ---------------------------------------------------------------------------


def test_validator_derives_graph_feasibility_descriptively() -> None:
    candidate = SolvabilityLLMCandidate(
        required_subgoals=[
            SolvabilityLLMSubgoal(
                subgoal_id="sg_1",
                label="x",
                description="y",
                subgoal_kind="acquire",
                required_capability_classes=["acquire"],
            )
        ],
        capability_matches=[
            CapabilityMatch(
                subgoal_id="sg_1",
                required_capability_classes=["acquire"],
                matched_capability_ids=["repo_checkout"],
                match_status="matched",
            ),
        ],
        candidate_shape_hint="linear",
    )
    result = SolvabilityValidator().validate_and_produce(
        candidate=candidate,
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        known_capability_ids=["repo_checkout"],
    )
    gf = result.contract.graph_feasibility
    assert gf.representable is True
    assert gf.runtime_supported is True
    assert gf.policy_supported is True
    assert gf.candidate_shape == "linear"
    assert gf.blocking_reasons == []


# ---------------------------------------------------------------------------
# E6 — admissibility: full_workflow when all satisfied + closed
# ---------------------------------------------------------------------------


def _run_solvability_then_admissibility(envelope: list[dict]):
    proposal = SolvabilityLLMService().propose(
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        capability_envelope=envelope,
    )
    validation = SolvabilityValidator().validate_and_produce(
        candidate=proposal.candidate,
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        known_capability_ids=[c["capability_id"] for c in envelope],
    )
    decision = ExecutionAdmissibilityService().decide(
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        solvability=validation.contract,
    )
    return validation.contract, decision.contract


def test_admissibility_full_workflow_when_everything_matches() -> None:
    solvability, admissibility = _run_solvability_then_admissibility(_full_capability_envelope())
    assert solvability.solvability_status == "solvable"
    assert admissibility.admissibility_status == "supported"
    assert admissibility.primary_execution_regime == "workflow_build"
    assert admissibility.max_automation_level == "full_workflow"
    assert admissibility.build_admission_decision == "admit"


# ---------------------------------------------------------------------------
# E7 — admissibility: unsupported / plan_only when unmatched
# ---------------------------------------------------------------------------


def test_admissibility_unsupported_when_capability_unmatched() -> None:
    # Remove analyze capability → synthesize/deliver still covered but
    # analyze won't match → subgoal becomes unmatched.
    envelope = [
        {"capability_id": "repo_checkout", "supported_step_kinds": ["acquire"]},
        # No analyze / transform capability
        {"capability_id": "report_writer", "supported_step_kinds": ["synthesize"]},
        {"capability_id": "fs_writer", "supported_step_kinds": ["deliver", "persist"]},
    ]
    solvability, admissibility = _run_solvability_then_admissibility(envelope)
    assert solvability.solvability_status == "not_solvable"
    assert admissibility.admissibility_status == "unsupported"
    assert admissibility.max_automation_level == "plan_only"
    assert admissibility.build_admission_decision == "reject"


# ---------------------------------------------------------------------------
# E8 — admissibility: human_gated when partial coverage
# ---------------------------------------------------------------------------


def test_admissibility_human_gated_when_partial_match() -> None:
    # Mock proposer emits subgoal with 2 required classes but envelope only
    # covers 1 → partial match.
    proposal_candidate = SolvabilityLLMCandidate(
        required_subgoals=[
            SolvabilityLLMSubgoal(
                subgoal_id="sg_1",
                label="Analyze repo",
                description="x",
                subgoal_kind="analyze",
                required_capability_classes=["analyze", "transform"],
            )
        ],
        capability_matches=[
            CapabilityMatch(
                subgoal_id="sg_1",
                required_capability_classes=["analyze", "transform"],
                matched_capability_ids=["cap_analyze"],  # only 1 of 2
                match_status="partial",
            ),
        ],
    )
    validation = SolvabilityValidator().validate_and_produce(
        candidate=proposal_candidate,
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        known_capability_ids=["cap_analyze"],
    )
    decision = ExecutionAdmissibilityService().decide(
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        solvability=validation.contract,
    )
    assert validation.contract.solvability_status == "solvable_with_review"
    assert decision.contract.admissibility_status == "review_needed"
    assert decision.contract.primary_execution_regime == "human_gated"
    assert decision.contract.max_automation_level == "human_gated"


# ---------------------------------------------------------------------------
# E9 — admissibility: human_gated when subgoal requires review
# ---------------------------------------------------------------------------


def test_admissibility_human_gated_when_subgoal_requires_review() -> None:
    candidate = SolvabilityLLMCandidate(
        required_subgoals=[
            SolvabilityLLMSubgoal(
                subgoal_id="sg_1",
                label="Delete files",
                description="destructive",
                subgoal_kind="deliver",
                required_capability_classes=["deliver"],
                human_review_required=True,
            )
        ],
        capability_matches=[
            CapabilityMatch(
                subgoal_id="sg_1",
                required_capability_classes=["deliver"],
                matched_capability_ids=["fs_writer"],
                match_status="matched",
            )
        ],
    )
    validation = SolvabilityValidator().validate_and_produce(
        candidate=candidate,
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        known_capability_ids=["fs_writer"],
    )
    decision = ExecutionAdmissibilityService().decide(
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        solvability=validation.contract,
    )
    # solvability: matched but review_required → solvable_with_review
    assert validation.contract.solvability_status == "solvable_with_review"
    # admissibility: human_gated regime
    assert decision.contract.primary_execution_regime == "human_gated"
    assert decision.contract.requires_human_review is True


# ---------------------------------------------------------------------------
# E10 — admissibility: review_needed / plan_only when closure still blocking
# ---------------------------------------------------------------------------


def test_admissibility_review_needed_when_closure_blocking() -> None:
    candidate = SolvabilityLLMCandidate()  # empty
    validation = SolvabilityValidator().validate_and_produce(
        candidate=candidate,
        problem_formulation=_blocking_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
    )
    decision = ExecutionAdmissibilityService().decide(
        problem_formulation=_blocking_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        solvability=validation.contract,
    )
    assert validation.contract.solvability_status == "solvable_with_clarification"
    assert decision.contract.admissibility_status == "review_needed"
    assert decision.contract.max_automation_level == "plan_only"


# ---------------------------------------------------------------------------
# E11 — R3: solvable != admissible (distinct axes)
# ---------------------------------------------------------------------------


def test_solvable_does_not_imply_admissible() -> None:
    """A subgoal that is fully matched but requires human review ends up
    'solvable_with_review' — the problem is technically workable — yet
    admissibility reports 'review_needed' with primary_execution_regime
    'human_gated'. The two statuses are distinct axes."""
    candidate = SolvabilityLLMCandidate(
        required_subgoals=[
            SolvabilityLLMSubgoal(
                subgoal_id="sg_1",
                label="Delete",
                description="destructive",
                subgoal_kind="deliver",
                required_capability_classes=["deliver"],
                human_review_required=True,
            )
        ],
        capability_matches=[
            CapabilityMatch(
                subgoal_id="sg_1",
                required_capability_classes=["deliver"],
                matched_capability_ids=["fs_writer"],
                match_status="matched",
            )
        ],
    )
    validation = SolvabilityValidator().validate_and_produce(
        candidate=candidate,
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        known_capability_ids=["fs_writer"],
    )
    decision = ExecutionAdmissibilityService().decide(
        problem_formulation=_closed_pf(),
        input_closure=_closed_ic(),
        output_closure=_closed_oc(),
        solvability=validation.contract,
    )
    # "solvable_with_review" is a solvability value
    assert validation.contract.solvability_status == "solvable_with_review"
    # Admissibility is NOT "supported" — it's "review_needed"
    assert decision.contract.admissibility_status != "supported"
    # And the regime is NOT full workflow
    assert decision.contract.max_automation_level != "full_workflow"


# ---------------------------------------------------------------------------
# E12/E13/E14/E18 — Full end-to-end via API endpoint
# ---------------------------------------------------------------------------


def _create_project(client: TestClient) -> dict:
    resp = client.post("/api/v1/projects", json={"name": "Phase 3 Test", "description": "t"})
    assert resp.status_code == 201
    return resp.json()


def _create_session(client: TestClient, project_id: str) -> dict:
    resp = client.post("/api/v1/requirements/sessions", json={"project_id": project_id})
    assert resp.status_code == 201
    return resp.json()


def _interpret(client: TestClient, raw_intent: str) -> dict:
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


def test_evaluate_preserves_closure_contracts_byte_equal(client: TestClient) -> None:
    parent = _interpret(
        client,
        "Analyze the repository at https://github.com/example/foo and produce a markdown report.",
    )
    parent_pf = parent["problem_formulation"]
    parent_ic = parent["input_closure"]
    parent_oc = parent["output_closure"]

    resp = client.post(f"/api/v1/front-chain/states/{parent['state_id']}/evaluate", json={})
    assert resp.status_code == 201, resp.text
    evaluated = resp.json()

    assert evaluated["problem_formulation"] == parent_pf
    assert evaluated["input_closure"] == parent_ic
    assert evaluated["output_closure"] == parent_oc


def test_evaluate_creates_chained_turn_row(client: TestClient) -> None:
    parent = _interpret(
        client,
        "Analyze the repository at https://github.com/example/foo and produce a markdown report.",
    )
    resp = client.post(f"/api/v1/front-chain/states/{parent['state_id']}/evaluate", json={})
    evaluated = resp.json()
    assert evaluated["parent_state_id"] == parent["state_id"]
    assert evaluated["turn_number"] == parent["turn_number"] + 1
    assert evaluated["state_id"] != parent["state_id"]


def test_evaluate_exactly_one_llm_call(client: TestClient) -> None:
    parent = _interpret(
        client,
        "Analyze the repository at https://github.com/example/foo and produce a markdown report.",
    )
    resp = client.post(f"/api/v1/front-chain/states/{parent['state_id']}/evaluate", json={})
    evaluated = resp.json()
    assert evaluated["solvability_llm_trace"]["call_count"] == 1


def test_evaluate_response_carries_solvability_and_admissibility(client: TestClient) -> None:
    parent = _interpret(
        client,
        "Analyze the repository at https://github.com/example/foo and produce a markdown report.",
    )
    resp = client.post(f"/api/v1/front-chain/states/{parent['state_id']}/evaluate", json={})
    evaluated = resp.json()
    assert "solvability" in evaluated
    assert "execution_admissibility" in evaluated
    assert "solvability_llm_trace" in evaluated
    assert evaluated["solvability"]["solvability_status"] in {
        "not_solvable",
        "solvable_with_clarification",
        "solvable_with_review",
        "solvable",
    }
    assert evaluated["execution_admissibility"]["admissibility_status"] in {
        "supported",
        "unsupported",
        "review_needed",
    }


# ---------------------------------------------------------------------------
# E19 — evaluate 404 on unknown parent state
# ---------------------------------------------------------------------------


def test_evaluate_404_for_unknown_state(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/front-chain/states/00000000-0000-0000-0000-000000000000/evaluate",
        json={},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# E15/E16/E17 — AST guardrails
# ---------------------------------------------------------------------------


_PHASE3_BASE = Path(__file__).resolve().parent.parent / "app" / "services" / "front_chain"

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


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    return names


def test_solvability_llm_service_does_not_import_legacy_modules() -> None:
    imports = _imported_modules(_PHASE3_BASE / "solvability_llm_service.py")
    violations = imports & _FORBIDDEN_LEGACY_MODULES
    assert violations == set(), f"Forbidden imports in solvability_llm_service: {violations}"
    assert not any(m.startswith("app.services.requirement.compiler") for m in imports)


def test_solvability_validator_is_deterministic_no_llm_coupling() -> None:
    imports = _imported_modules(_PHASE3_BASE / "solvability_validator.py")
    forbidden_substrings = ("provider_runtime", "_llm_service")
    leaks = [name for name in imports if any(s in name for s in forbidden_substrings)]
    assert leaks == [], f"solvability_validator has LLM coupling: {leaks}"


def test_execution_admissibility_service_is_zero_llm() -> None:
    imports = _imported_modules(_PHASE3_BASE / "execution_admissibility_service.py")
    forbidden_substrings = ("provider_runtime", "_llm_service")
    leaks = [name for name in imports if any(s in name for s in forbidden_substrings)]
    assert leaks == [], f"execution_admissibility_service has LLM coupling: {leaks}"
    # Also no legacy module imports
    violations = imports & _FORBIDDEN_LEGACY_MODULES
    assert violations == set()
