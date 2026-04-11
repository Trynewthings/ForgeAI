"""Phase 1A front-chain guardrail tests.

Coverage matrix (tracked against R1–R10 and R-substrate):
    T1  — problem formulation stays separate from closure semantics            (R4)
    T2  — input closure does not absorb output or admissibility                (R4)
    T3  — output closure does not absorb input or capability                   (R4)
    T4  — partition validator rejects forbidden cross-layer fields             (R4)
    T5  — FrontChainState is the only write target in the new path             (R1/R8)
    T6  — new path does not write Requirement JSON fields                      (R1/R8)
    T7  — state requires session/requirement binding unless preview=True       (R5)
    T8  — exactly one LLM call per interpret invocation                        (R9)
    T9  — new front_chain package has no legacy-service imports (AST check)    (R10)
    T10 — closure gap merger puts formulation blocking first                   (R4)
    T11 — closure gap merger orders input before output                        (R4)
    T12 — closure gap merger places review gaps after blocking                 (R4)
    T13 — policy engine emits at most three questions                          (R-policy)
    T14 — policy engine prefers blocking over review                           (R-policy)
    T15 — policy engine prefers formulation over closure                       (R-policy)
    T16 — policy engine returns empty when no gaps                             (R-policy)
    T17 — interpreter accepts optional semantic substrate hint                 (R-substrate)
    T18 — interpreter does not fall back to substrate on LLM failure           (R-substrate)
    T19 — partition validator ignores substrate origin                         (R-substrate)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from app.db.models import FrontChainState, Project, Requirement, RequirementSession
from app.schemas.front_chain import (
    FrontChainClarificationQuestion,
    FrontChainInterpretationRequest,
    FrontChainInterpreterInputClosureCandidate,
    FrontChainInterpreterLLMOutput,
    FrontChainInterpreterOutputClosureCandidate,
    FrontChainInterpreterProblemCandidate,
    FrontChainLLMTraceSummary,
    MergedGapItem,
    MergedGapSet,
)
from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemAssumption,
    ProblemDecision,
    ProblemFormulationContract,
)
from app.schemas.semantic_core import SemanticCore
from app.services.front_chain.clarification_policy_engine import ClarificationPolicyEngine
from app.services.front_chain.closure_gap_merger import ClosureGapMerger
from app.services.front_chain.contract_partition_validator import (
    ContractPartitionValidator,
    PartitionValidationIssue,
)
from app.services.front_chain.front_chain_interpreter_llm_service import (
    INTERPRETER_VERSION,
    FrontChainInterpreterLLMService,
)
from app.services.front_chain.front_chain_state_service import FrontChainStateService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _create_project_via_api(client: TestClient) -> dict:
    resp = client.post("/api/v1/projects", json={"name": "FrontChain Phase 1A", "description": "test"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_session_via_api(client: TestClient, project_id: str) -> dict:
    resp = client.post("/api/v1/requirements/sessions", json={"project_id": project_id})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_requirement_row(client: TestClient, project_id: str) -> dict:
    resp = client.post(
        "/api/v1/requirements",
        json={"project_id": project_id, "title": "FrontChain Phase 1A requirement"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _run_full_pipeline(raw_intent: str) -> tuple[
    ProblemFormulationContract,
    InputClosureContract,
    OutputClosureContract,
    MergedGapSet,
    list[FrontChainClarificationQuestion],
    FrontChainLLMTraceSummary,
]:
    interpreter = FrontChainInterpreterLLMService()
    result = interpreter.interpret(raw_intent)
    partition = ContractPartitionValidator().validate_and_partition(result.llm_output)
    merged = ClosureGapMerger().merge(
        partition.problem_formulation,
        partition.input_closure,
        partition.output_closure,
    )
    questions = ClarificationPolicyEngine().select_top_questions(merged)
    return (
        partition.problem_formulation,
        partition.input_closure,
        partition.output_closure,
        merged,
        questions,
        result.trace_summary,
    )


# ---------------------------------------------------------------------------
# T1 — Problem formulation stays separate from closure semantics
# ---------------------------------------------------------------------------


def test_problem_formulation_stays_separate_from_closure_semantics() -> None:
    """Problem formulation must not carry closure_status or deliverable_form."""
    contract_fields = set(ProblemFormulationContract.model_fields.keys())
    forbidden = {
        "closure_status",
        "deliverable_form",
        "delivery_target",
        "output_modality",
        "resolved_source_locator",
        "acquisition_path_kind",
        "admissibility_status",
        "primary_execution_regime",
        "candidate_sources",
    }
    leaked = contract_fields & forbidden
    assert leaked == set(), (
        f"ProblemFormulationContract leaked closure/admissibility fields: {leaked}"
    )


# ---------------------------------------------------------------------------
# T2 — Input closure does not absorb output/admissibility semantics
# ---------------------------------------------------------------------------


def test_input_closure_does_not_absorb_output_or_admissibility() -> None:
    contract_fields = set(InputClosureContract.model_fields.keys())
    forbidden = {
        "deliverable_form",
        "delivery_target",
        "output_modality",
        "admissibility_status",
        "primary_execution_regime",
        "capability_matches",
        "required_subgoals",
        "graph_feasibility",
        "selected_blueprint_type",
    }
    leaked = contract_fields & forbidden
    assert leaked == set(), (
        f"InputClosureContract leaked output/admissibility fields: {leaked}"
    )


# ---------------------------------------------------------------------------
# T3 — Output closure does not absorb input/capability semantics
# ---------------------------------------------------------------------------


def test_output_closure_does_not_absorb_input_or_capability() -> None:
    contract_fields = set(OutputClosureContract.model_fields.keys())
    forbidden = {
        "resolved_source_locator",
        "resolved_source_kind",
        "resolved_source_modality",
        "candidate_sources",
        "acquisition_path_kind",
        "capability_matches",
        "required_subgoals",
        "graph_feasibility",
        "admissibility_status",
    }
    leaked = contract_fields & forbidden
    assert leaked == set(), (
        f"OutputClosureContract leaked input/capability fields: {leaked}"
    )


# ---------------------------------------------------------------------------
# T4 — Partition validator rejects forbidden cross-layer fields
# ---------------------------------------------------------------------------


def test_partition_validator_strips_forbidden_cross_layer_fields(monkeypatch) -> None:
    """If an LLM emits a closure_status on problem formulation, the validator
    must strip it and record a ``forbidden_field`` issue."""

    llm_output = FrontChainInterpreterLLMOutput(
        problem_formulation_candidate=FrontChainInterpreterProblemCandidate(
            interpreted_problem_kind="test",
            target_object="repository",
            problem_status="solvable",
            planning_intent_label="Test intent",
        ),
        input_closure_candidate=FrontChainInterpreterInputClosureCandidate(
            target_object="repository",
            closure_status="input_closed",
        ),
        output_closure_candidate=FrontChainInterpreterOutputClosureCandidate(
            deliverable_form="report",
            closure_status="output_closed",
        ),
    )

    # Inject a forbidden field via the raw model_dump path — we construct a
    # custom validator input that carries a forbidden "closure_status" on the
    # problem candidate payload to prove the validator strips it.
    validator = ContractPartitionValidator()

    # Monkeypatch the candidate's model_dump to inject a forbidden key.
    original_dump = FrontChainInterpreterProblemCandidate.model_dump

    def _dump_with_forbidden(self, *args, **kwargs):
        payload = original_dump(self, *args, **kwargs)
        payload["closure_status"] = "input_closed"  # leaked field
        payload["deliverable_form"] = "report"  # another leaked field
        return payload

    monkeypatch.setattr(FrontChainInterpreterProblemCandidate, "model_dump", _dump_with_forbidden)
    result = validator.validate_and_partition(llm_output)

    forbidden_issues = [i for i in result.issues if i.kind == "forbidden_field"]
    forbidden_axes = {i.axis for i in forbidden_issues if i.layer == "problem_formulation"}
    assert "closure_status" in forbidden_axes
    assert "deliverable_form" in forbidden_axes

    # And the problem formulation contract must NOT carry those fields (it
    # can't anyway because the schema doesn't allow them, but this confirms
    # the strip happened upstream).
    problem_dump = result.problem_formulation.model_dump()
    assert "closure_status" not in problem_dump
    assert "deliverable_form" not in problem_dump


# ---------------------------------------------------------------------------
# T5 — FrontChainState is the only write target
# ---------------------------------------------------------------------------


def test_front_chain_state_is_only_write_target_when_running_pipeline(client: TestClient) -> None:
    project = _create_project_via_api(client)
    session = _create_session_via_api(client, project["id"])

    response = client.post(
        "/api/v1/front-chain/interpret",
        json={
            "project_id": project["id"],
            "raw_intent": "Summarize the README at https://github.com/example/repo into a markdown report.",
            "session_id": session["id"],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    state_id = body["state_id"]
    assert state_id

    # The state row exists
    get_response = client.get(f"/api/v1/front-chain/states/{state_id}")
    assert get_response.status_code == 200
    row = get_response.json()
    assert row["project_id"] == project["id"]
    assert row["session_id"] == session["id"]
    assert row["requirement_id"] is None


# ---------------------------------------------------------------------------
# T6 — New path does not write Requirement JSON fields
# ---------------------------------------------------------------------------


def test_new_path_does_not_write_requirement_json_fields(client: TestClient) -> None:
    project = _create_project_via_api(client)
    requirement = _create_requirement_row(client, project["id"])

    # Capture the pre-interpret requirement row so we can compare JSON columns.
    pre = client.get(f"/api/v1/requirements/{requirement['id']}").json()
    pre_snapshot = {
        "structured_spec_json": pre.get("structured_spec_json"),
        "compiler_result_json": pre.get("compiler_result_json"),
        "clarification_state_json": pre.get("clarification_state_json"),
        "compiler_notes_json": pre.get("compiler_notes_json"),
        "compiler_open_questions_json": pre.get("compiler_open_questions_json"),
    }

    interpret_response = client.post(
        "/api/v1/front-chain/interpret",
        json={
            "project_id": project["id"],
            "raw_intent": "Analyze the repo at https://github.com/example/demo and write a short assessment.",
            "requirement_id": requirement["id"],
        },
    )
    assert interpret_response.status_code == 201, interpret_response.text

    post = client.get(f"/api/v1/requirements/{requirement['id']}").json()
    post_snapshot = {
        "structured_spec_json": post.get("structured_spec_json"),
        "compiler_result_json": post.get("compiler_result_json"),
        "clarification_state_json": post.get("clarification_state_json"),
        "compiler_notes_json": post.get("compiler_notes_json"),
        "compiler_open_questions_json": post.get("compiler_open_questions_json"),
    }

    assert pre_snapshot == post_snapshot, (
        "Front-chain v2 interpret call must NOT write any legacy Requirement JSON column"
    )


# ---------------------------------------------------------------------------
# T7 — State requires session/requirement binding unless preview=True
# ---------------------------------------------------------------------------


def test_interpret_without_binding_rejects_with_422(client: TestClient) -> None:
    project = _create_project_via_api(client)

    response = client.post(
        "/api/v1/front-chain/interpret",
        json={
            "project_id": project["id"],
            "raw_intent": "Do the thing",
            # no session_id, no requirement_id, no preview
        },
    )
    assert response.status_code == 422, response.text
    assert "preview" in response.text.lower() or "binding" in response.text.lower()


def test_interpret_preview_mode_accepts_orphan_call(client: TestClient) -> None:
    project = _create_project_via_api(client)
    response = client.post(
        "/api/v1/front-chain/interpret",
        json={
            "project_id": project["id"],
            "raw_intent": "Summarize the data file at /tmp/sample.csv into a short markdown brief.",
            "preview": True,
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["preview_mode"] is True
    assert body["session_id"] is None
    assert body["requirement_id"] is None


# ---------------------------------------------------------------------------
# T8 — Exactly one LLM call per interpret
# ---------------------------------------------------------------------------


def test_new_path_makes_exactly_one_llm_call_per_interpret() -> None:
    interpreter = FrontChainInterpreterLLMService()
    result = interpreter.interpret(
        "Fetch the status page at https://example.com/status.json and produce a summary report."
    )
    assert result.trace_summary.call_count == 1, (
        f"Expected exactly 1 LLM call, got {result.trace_summary.call_count}"
    )
    # Running multiple interprets back-to-back must still be 1-call each.
    result2 = interpreter.interpret("Analyze the CSV at /tmp/foo.csv and write a report.")
    assert result2.trace_summary.call_count == 1


# ---------------------------------------------------------------------------
# T9 — New front_chain package has no legacy-service imports (AST)
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


def _walk_front_chain_module_files() -> list[Path]:
    root = Path(__file__).resolve().parent.parent / "app" / "services" / "front_chain"
    assert root.exists(), f"front_chain package not found at {root}"
    return [p for p in root.rglob("*.py") if p.is_file()]


def test_new_front_chain_package_does_not_import_old_services() -> None:
    violations: list[str] = []
    for module_file in _walk_front_chain_module_files():
        source = module_file.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module_name = alias.name
                    if module_name in _FORBIDDEN_LEGACY_MODULES or any(
                        module_name.startswith(prefix) for prefix in _FORBIDDEN_LEGACY_PREFIXES
                    ):
                        violations.append(f"{module_file.name}: import {module_name}")
            elif isinstance(node, ast.ImportFrom):
                module_name = node.module or ""
                if module_name in _FORBIDDEN_LEGACY_MODULES or any(
                    module_name.startswith(prefix) for prefix in _FORBIDDEN_LEGACY_PREFIXES
                ):
                    violations.append(f"{module_file.name}: from {module_name} import ...")
    assert violations == [], (
        "New front_chain package must not import legacy front-chain modules. "
        f"Violations: {violations}"
    )


# ---------------------------------------------------------------------------
# T10/T11/T12 — Gap merger priority and review-after-blocking
# ---------------------------------------------------------------------------


def _make_formulation(blocking: list[str] = None, review: list[str] = None) -> ProblemFormulationContract:
    return ProblemFormulationContract(
        interpreted_problem_kind="test",
        target_object="repository",
        problem_status="underdetermined",
        blocking_axes=blocking or [],
        review_axes=review or [],
    )


def _make_input_closure(blocking: list[str] = None, review: list[str] = None) -> InputClosureContract:
    return InputClosureContract(
        target_object="repository",
        blocking_axes=blocking or [],
        review_axes=review or [],
    )


def _make_output_closure(blocking: list[str] = None, review: list[str] = None) -> OutputClosureContract:
    return OutputClosureContract(
        blocking_axes=blocking or [],
        review_axes=review or [],
    )


def test_closure_gap_merger_priority_formulation_first() -> None:
    formulation = _make_formulation(blocking=["target_object"])
    input_closure = _make_input_closure(blocking=["source_locator"])
    output_closure = _make_output_closure(blocking=["deliverable_form"])

    merged = ClosureGapMerger().merge(formulation, input_closure, output_closure)

    assert len(merged.blocking) == 3
    assert merged.blocking[0].layer == "problem_formulation"
    assert merged.blocking[0].axis == "target_object"


def test_closure_gap_merger_priority_input_before_output() -> None:
    formulation = _make_formulation()
    input_closure = _make_input_closure(blocking=["source_locator"])
    output_closure = _make_output_closure(blocking=["deliverable_form"])

    merged = ClosureGapMerger().merge(formulation, input_closure, output_closure)

    assert [g.layer for g in merged.blocking] == ["input_closure", "output_closure"]


def test_closure_gap_merger_review_gaps_after_blocking() -> None:
    formulation = _make_formulation(blocking=["target_object"], review=["domain_fit"])
    input_closure = _make_input_closure(review=["auth_mode"])
    output_closure = _make_output_closure()

    merged = ClosureGapMerger().merge(formulation, input_closure, output_closure)

    assert merged.blocking and merged.review
    assert all(g.severity == "blocking" for g in merged.blocking)
    assert all(g.severity == "review" for g in merged.review)
    # prioritized_decisions: blocking axes come first, then review axes
    assert merged.prioritized_decisions.index("target_object") < merged.prioritized_decisions.index("domain_fit")


# ---------------------------------------------------------------------------
# T13/T14/T15/T16 — Clarification policy engine
# ---------------------------------------------------------------------------


def test_policy_engine_emits_at_most_three_questions() -> None:
    gaps = MergedGapSet(
        blocking=[
            MergedGapItem(axis="target_object", layer="problem_formulation", severity="blocking", priority=0),
            MergedGapItem(axis="source_locator", layer="input_closure", severity="blocking", priority=1),
            MergedGapItem(axis="deliverable_form", layer="output_closure", severity="blocking", priority=2),
            MergedGapItem(axis="source_modality", layer="input_closure", severity="blocking", priority=1),
            MergedGapItem(axis="output_modality", layer="output_closure", severity="blocking", priority=2),
        ],
        review=[],
        prioritized_decisions=[
            "target_object",
            "source_locator",
            "source_modality",
            "deliverable_form",
            "output_modality",
        ],
    )
    questions = ClarificationPolicyEngine(max_questions=3).select_top_questions(gaps)
    assert len(questions) == 3


def test_policy_engine_prefers_blocking_over_review() -> None:
    gaps = MergedGapSet(
        blocking=[
            MergedGapItem(axis="source_locator", layer="input_closure", severity="blocking", priority=1),
        ],
        review=[
            MergedGapItem(axis="domain_fit", layer="problem_formulation", severity="review", priority=100),
        ],
        prioritized_decisions=["source_locator", "domain_fit"],
    )
    questions = ClarificationPolicyEngine().select_top_questions(gaps)
    assert questions[0].severity == "blocking"
    assert questions[0].axis == "source_locator"


def test_policy_engine_prefers_formulation_over_closure() -> None:
    gaps = MergedGapSet(
        blocking=[
            MergedGapItem(axis="target_object", layer="problem_formulation", severity="blocking", priority=0),
            MergedGapItem(axis="source_locator", layer="input_closure", severity="blocking", priority=1),
        ],
        review=[],
        prioritized_decisions=["target_object", "source_locator"],
    )
    questions = ClarificationPolicyEngine().select_top_questions(gaps)
    assert questions[0].layer == "problem_formulation"
    assert questions[0].axis == "target_object"


def test_policy_engine_returns_empty_when_no_gaps() -> None:
    empty = MergedGapSet(blocking=[], review=[], prioritized_decisions=[])
    questions = ClarificationPolicyEngine().select_top_questions(empty)
    assert questions == []


# ---------------------------------------------------------------------------
# T17/T18/T19 — Semantic substrate hint (read-only substrate, not authority)
# ---------------------------------------------------------------------------


def _build_semantic_substrate_hint() -> SemanticCore:
    from app.schemas.semantic_core import (
        DerivedIntentResolution,
        DomainSemanticPackSet,
        GlobalSemanticCore,
    )

    return SemanticCore(
        global_core=GlobalSemanticCore(
            target_object="repository",
            deliverable_form="report",
            analysis_frame="architecture",
            decision_posture="descriptive",
            workflow_shape="bounded_workflow",
            source_modality="workspace_repo",
        ),
        domain_packs=DomainSemanticPackSet(),
        derived_intent=DerivedIntentResolution(
            deliverable_contract="architecture_report",
            reason="prior conversation context",
        ),
        evidence_requirements=[],
    )


def test_interpreter_accepts_optional_semantic_substrate_hint() -> None:
    interpreter = FrontChainInterpreterLLMService()
    substrate = _build_semantic_substrate_hint()
    result = interpreter.interpret(
        "Produce the architecture report for the repository.",
        semantic_substrate=substrate,
    )
    # Substrate is accepted — we don't assert that it shaped the output, only
    # that the call completes and trace still shows exactly one LLM call.
    assert result.trace_summary.call_count == 1


def test_interpreter_does_not_fall_back_to_substrate_on_llm_failure() -> None:
    """If the real LLM call fails, the interpreter must return an empty
    envelope rather than silently copying substrate fields into the contract
    candidates (R-substrate rule)."""

    from app.services.provider_runtime import ProviderCallError

    class _BrokenAdapter:
        provider_key = "broken"

        def chat(self, messages):
            raise ProviderCallError("simulated failure")

    def _broken_factory():
        return _BrokenAdapter(), None

    interpreter = FrontChainInterpreterLLMService(adapter_factory=_broken_factory)

    # Force real mode for this test only.
    from app.core.config import settings

    original_mode = settings.llm_mode
    settings.llm_mode = "real"
    try:
        result = interpreter.interpret(
            "Produce the architecture report for the repository.",
            semantic_substrate=_build_semantic_substrate_hint(),
        )
    finally:
        settings.llm_mode = original_mode

    # The returned envelope must NOT carry substrate-derived fields.
    llm_output = result.llm_output
    assert llm_output.problem_formulation_candidate.target_object is None
    assert llm_output.problem_formulation_candidate.planning_intent_label is None
    assert llm_output.input_closure_candidate.resolved_source_kind is None
    assert llm_output.output_closure_candidate.deliverable_form is None
    # Trace should reflect an error finish_reason
    assert result.trace_summary.finish_reason == "error"
    # Call count still 1: we tried, it failed, we did not retry.
    assert result.trace_summary.call_count == 1


def test_partition_validator_ignores_substrate_origin() -> None:
    """The partition validator must treat any forbidden cross-layer field the
    same way regardless of whether the LLM got it from a substrate hint or
    made it up — there is no "trusted substrate" path."""
    llm_output = FrontChainInterpreterLLMOutput(
        problem_formulation_candidate=FrontChainInterpreterProblemCandidate(
            interpreted_problem_kind="test",
            target_object="repository",
            problem_status="solvable",
            planning_intent_label="Test intent",
        ),
        input_closure_candidate=FrontChainInterpreterInputClosureCandidate(
            target_object="repository",
            closure_status="input_closed",
        ),
        output_closure_candidate=FrontChainInterpreterOutputClosureCandidate(
            deliverable_form="report",
            closure_status="output_closed",
        ),
    )
    result = ContractPartitionValidator().validate_and_partition(llm_output)
    # No forbidden fields are present (the schema prevents them) so the
    # validator should complete without issues. The point of this test is
    # that the validator does not special-case substrate-enriched input.
    assert all(i.kind != "forbidden_field" for i in result.issues)
    assert result.problem_formulation.target_object == "repository"
    assert result.input_closure.closure_status == "input_closed"


# ---------------------------------------------------------------------------
# Bonus: DB CHECK constraint enforcement
# ---------------------------------------------------------------------------


def test_front_chain_state_db_check_constraint_is_present(client: TestClient) -> None:
    """Sanity check that the CHECK constraint exists on the table (defence in
    depth against the API-layer guard being bypassed)."""
    from app.core.database import get_db

    override = client.app.dependency_overrides.get(get_db)
    assert override is not None
    db_iter = override()
    db = next(db_iter)
    try:
        engine = db.get_bind()
        inspector = inspect(engine)
        check_constraints = inspector.get_check_constraints("front_chain_states")
        names = {c.get("name") or "" for c in check_constraints}
        assert any("binding_required" in name for name in names), (
            f"CHECK constraint missing; got {names}"
        )
    finally:
        try:
            next(db_iter)
        except StopIteration:
            pass
