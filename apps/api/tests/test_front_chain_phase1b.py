"""Phase 1B front-chain answer-loop tests.

Coverage matrix (Phase 1B R-list):

    R1B-1  Local patch only — unchanged contracts byte-equal preserved
    R1B-2  clarification_interpreter only emits resolutions, never contracts
    R1B-3  contract_update_engine is deterministic (no LLM imports + idempotent)
    R1B-4  closure_reevaluation_service does NOT call the interpreter
    R1B-5  Still does NOT touch solvability / admissibility / compile / session

Tests in this file:
    B1   single-axis answer patches only that layer (R1B-1)
    B2   multi-axis answer patches multiple layers
    B3   running update engine twice yields byte-equal output (R1B-3)
    B4   answering a question doesn't drift the unchanged contracts
    B5   parent_state_id chain is set correctly
    B6   turn_number increments correctly
    B7   answer endpoint requires existing parent state (404 on bad id)
    B8   exactly 1 LLM call per answer (R9 carry-over)
    B9   AST: clarification_interpreter doesn't import contract update or state services
    B10  AST: contract_update_engine doesn't import provider_runtime or any *_llm_service
    B11  AST: closure_reevaluation_service doesn't import the front-chain interpreter
    B12  rejected_answers populated for off-list answers
    B13  policy engine reflects new state (axis disappears after answer)
    B14  closure_status recomputed when blocking axes drained
    B15  no-op answer still creates a new chained state row (audit invariant)
    B16  forbidden module list still empty for Phase 1B files (R10 carry-over)
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from app.db.models import FrontChainState
from app.schemas.front_chain import (
    FrontChainAnswerItem,
    FrontChainClarificationQuestion,
    MergedGapItem,
    MergedGapSet,
)
from app.schemas.problem_formulation import (
    ClarificationResolutionRecord,
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.services.front_chain.clarification_interpreter_llm_service import (
    ClarificationInterpreterLLMService,
)
from app.services.front_chain.clarification_policy_engine import ClarificationPolicyEngine
from app.services.front_chain.closure_gap_merger import ClosureGapMerger
from app.services.front_chain.closure_reevaluation_service import ClosureReevaluationService
from app.services.front_chain.contract_partition_validator import ContractPartitionValidator
from app.services.front_chain.contract_update_engine import ContractUpdateEngine
from app.services.front_chain.front_chain_interpreter_llm_service import (
    FrontChainInterpreterLLMService,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_project_via_api(client: TestClient) -> dict:
    resp = client.post("/api/v1/projects", json={"name": "FrontChain Phase 1B", "description": "test"})
    assert resp.status_code == 201
    return resp.json()


def _create_session_via_api(client: TestClient, project_id: str) -> dict:
    resp = client.post("/api/v1/front-chain/sessions", json={"project_id": project_id})
    assert resp.status_code == 201
    return resp.json()


def _interpret(client: TestClient, *, raw_intent: str) -> dict:
    project = _create_project_via_api(client)
    session = _create_session_via_api(client, project["id"])
    response = client.post(
        "/api/v1/front-chain/interpret",
        json={
            "project_id": project["id"],
            "raw_intent": raw_intent,
            "session_id": session["id"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _build_initial_pipeline(raw_intent: str) -> tuple[
    ProblemFormulationContract,
    InputClosureContract,
    OutputClosureContract,
    list[FrontChainClarificationQuestion],
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
        questions,
    )


def _question_for_axis(
    questions: list[FrontChainClarificationQuestion], axis: str
) -> Optional[FrontChainClarificationQuestion]:
    return next((q for q in questions if q.axis == axis), None)


# ---------------------------------------------------------------------------
# B1 — Single-axis answer patches only that layer (R1B-1)
# ---------------------------------------------------------------------------


def test_single_axis_answer_patches_only_input_closure_layer() -> None:
    pf, ic, oc, questions = _build_initial_pipeline("I want to build something useful")
    src_q = _question_for_axis(questions, "source_locator")
    assert src_q is not None, "expected source_locator question on the vague intent"

    interpreter = ClarificationInterpreterLLMService()
    interp = interpreter.interpret_answers(
        answers=[FrontChainAnswerItem(question_id=src_q.question_id, answer_text="https://github.com/foo/bar")],
        outstanding_questions=questions,
    )
    assert any(r.axis == "source_locator" for r in interp.resolutions)

    update = ContractUpdateEngine().apply_resolutions(
        problem_formulation=pf,
        input_closure=ic,
        output_closure=oc,
        resolutions=interp.resolutions,
    )

    # Only input_closure should change.
    assert update.changed_layers == ["input_closure"]
    # Byte-equal preservation for the other two layers.
    assert update.problem_formulation.model_dump() == pf.model_dump()
    assert update.output_closure.model_dump() == oc.model_dump()
    # Input closure should now have the locator and no source_locator block.
    assert update.input_closure.resolved_source_locator == "https://github.com/foo/bar"
    assert "source_locator" not in update.input_closure.blocking_axes
    assert update.input_closure.closure_status == "input_closed"


# ---------------------------------------------------------------------------
# B2 — Multi-axis answer patches multiple layers
# ---------------------------------------------------------------------------


def test_multi_axis_answer_patches_three_layers() -> None:
    pf, ic, oc, questions = _build_initial_pipeline("I want to build something useful")

    target_q = _question_for_axis(questions, "target_object")
    src_q = _question_for_axis(questions, "source_locator")
    deliv_q = _question_for_axis(questions, "deliverable_form")
    assert target_q and src_q and deliv_q

    interpreter = ClarificationInterpreterLLMService()
    interp = interpreter.interpret_answers(
        answers=[
            FrontChainAnswerItem(question_id=target_q.question_id, answer_text="repository"),
            FrontChainAnswerItem(question_id=src_q.question_id, answer_text="https://github.com/example/repo"),
            FrontChainAnswerItem(question_id=deliv_q.question_id, answer_text="markdown report"),
        ],
        outstanding_questions=questions,
    )
    update = ContractUpdateEngine().apply_resolutions(
        problem_formulation=pf,
        input_closure=ic,
        output_closure=oc,
        resolutions=interp.resolutions,
    )

    assert set(update.changed_layers) == {"problem_formulation", "input_closure", "output_closure"}
    assert update.problem_formulation.target_object == "repository"
    assert update.input_closure.resolved_source_locator == "https://github.com/example/repo"
    assert update.output_closure.deliverable_form == "report"


# ---------------------------------------------------------------------------
# B3 — Update engine is deterministic / idempotent
# ---------------------------------------------------------------------------


def test_update_engine_is_deterministic_and_idempotent() -> None:
    pf, ic, oc, questions = _build_initial_pipeline("I want to build something useful")
    src_q = _question_for_axis(questions, "source_locator")
    interp = ClarificationInterpreterLLMService().interpret_answers(
        answers=[FrontChainAnswerItem(question_id=src_q.question_id, answer_text="https://github.com/foo/bar")],
        outstanding_questions=questions,
    )

    engine = ContractUpdateEngine()
    first = engine.apply_resolutions(
        problem_formulation=pf, input_closure=ic, output_closure=oc, resolutions=interp.resolutions
    )
    second = engine.apply_resolutions(
        problem_formulation=pf, input_closure=ic, output_closure=oc, resolutions=interp.resolutions
    )
    assert first.problem_formulation.model_dump() == second.problem_formulation.model_dump()
    assert first.input_closure.model_dump() == second.input_closure.model_dump()
    assert first.output_closure.model_dump() == second.output_closure.model_dump()


# ---------------------------------------------------------------------------
# B4 — Answering doesn't drift unchanged contracts across multiple turns
# ---------------------------------------------------------------------------


def test_unchanged_contracts_remain_byte_equal_across_two_turns() -> None:
    pf, ic, oc, questions = _build_initial_pipeline("I want to build something useful")
    pf_orig = pf.model_dump()
    oc_orig = oc.model_dump()

    # Turn 1: answer source_locator
    src_q = _question_for_axis(questions, "source_locator")
    interp1 = ClarificationInterpreterLLMService().interpret_answers(
        answers=[FrontChainAnswerItem(question_id=src_q.question_id, answer_text="https://github.com/x/y")],
        outstanding_questions=questions,
    )
    update1 = ContractUpdateEngine().apply_resolutions(
        problem_formulation=pf, input_closure=ic, output_closure=oc, resolutions=interp1.resolutions
    )

    # PF and OC must be untouched.
    assert update1.problem_formulation.model_dump() == pf_orig
    assert update1.output_closure.model_dump() == oc_orig

    # Turn 2: reevaluation produces new questions; user now answers deliverable_form
    reeval = ClosureReevaluationService().reevaluate(
        problem_formulation=update1.problem_formulation,
        input_closure=update1.input_closure,
        output_closure=update1.output_closure,
    )
    deliv_q = _question_for_axis(reeval.top_questions, "deliverable_form")
    assert deliv_q is not None

    interp2 = ClarificationInterpreterLLMService().interpret_answers(
        answers=[FrontChainAnswerItem(question_id=deliv_q.question_id, answer_text="markdown report")],
        outstanding_questions=reeval.top_questions,
    )
    update2 = ContractUpdateEngine().apply_resolutions(
        problem_formulation=update1.problem_formulation,
        input_closure=update1.input_closure,
        output_closure=update1.output_closure,
        resolutions=interp2.resolutions,
    )

    # PF should *still* be byte-equal to the original (we never resolved it).
    assert update2.problem_formulation.model_dump() == pf_orig
    # Input closure from turn 1 must still be byte-equal in turn 2 (we only
    # touched output_closure).
    assert update2.input_closure.model_dump() == update1.input_closure.model_dump()


# ---------------------------------------------------------------------------
# B5/B6 — Parent chain and turn_number progression via API
# ---------------------------------------------------------------------------


def test_answer_endpoint_creates_chained_state(client: TestClient) -> None:
    initial = _interpret(client, raw_intent="I want to build something useful")
    src_q = next((q for q in initial["top_questions"] if q["axis"] == "source_locator"), None)
    assert src_q is not None
    assert initial["turn_number"] == 1
    assert initial["parent_state_id"] is None

    answer_response = client.post(
        f"/api/v1/front-chain/states/{initial['state_id']}/answer",
        json={
            "answers": [
                {"question_id": src_q["question_id"], "answer_text": "https://github.com/example/foo"}
            ]
        },
    )
    assert answer_response.status_code == 201, answer_response.text
    state_v2 = answer_response.json()
    assert state_v2["parent_state_id"] == initial["state_id"]
    assert state_v2["turn_number"] == 2
    assert state_v2["state_id"] != initial["state_id"]
    assert state_v2["input_closure"]["resolved_source_locator"] == "https://github.com/example/foo"


def test_answer_endpoint_404_for_unknown_state(client: TestClient) -> None:
    response = client.post(
        "/api/v1/front-chain/states/00000000-0000-0000-0000-000000000000/answer",
        json={"answers": [{"question_id": "q_x", "answer_text": "test"}]},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# B8 — Exactly 1 LLM call per answer
# ---------------------------------------------------------------------------


def test_exactly_one_llm_call_per_answer() -> None:
    _, _, _, questions = _build_initial_pipeline("I want to build something useful")
    src_q = _question_for_axis(questions, "source_locator")
    interp = ClarificationInterpreterLLMService().interpret_answers(
        answers=[FrontChainAnswerItem(question_id=src_q.question_id, answer_text="https://github.com/x/y")],
        outstanding_questions=questions,
    )
    assert interp.llm_trace.call_count == 1, (
        f"Expected exactly 1 LLM call, got {interp.llm_trace.call_count}"
    )


# ---------------------------------------------------------------------------
# B9/B10/B11 — AST guardrails for new services
# ---------------------------------------------------------------------------


_PHASE1B_BASE = Path(__file__).resolve().parent.parent / "app" / "services" / "front_chain"


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


def test_clarification_interpreter_does_not_import_state_or_update_services() -> None:
    imports = _imported_modules(_PHASE1B_BASE / "clarification_interpreter_llm_service.py")
    forbidden = {
        "app.services.front_chain.front_chain_state_service",
        "app.services.front_chain.contract_update_engine",
        "app.services.front_chain.contract_partition_validator",
        "app.services.front_chain.closure_gap_merger",
        "app.services.front_chain.closure_reevaluation_service",
    }
    assert (imports & forbidden) == set(), (
        f"clarification_interpreter must not import: {imports & forbidden}"
    )


def test_contract_update_engine_does_not_import_llm_or_provider_runtime() -> None:
    imports = _imported_modules(_PHASE1B_BASE / "contract_update_engine.py")
    forbidden_substrings = ("provider_runtime", "_llm_service")
    leaks = [name for name in imports if any(s in name for s in forbidden_substrings)]
    assert leaks == [], f"contract_update_engine has LLM coupling: {leaks}"


def test_closure_reevaluation_service_does_not_import_interpreter() -> None:
    imports = _imported_modules(_PHASE1B_BASE / "closure_reevaluation_service.py")
    forbidden = {
        "app.services.front_chain.front_chain_interpreter_llm_service",
        "app.services.front_chain.clarification_interpreter_llm_service",
        "app.services.front_chain.contract_partition_validator",
    }
    assert (imports & forbidden) == set(), (
        f"closure_reevaluation_service has forbidden imports: {imports & forbidden}"
    )


# ---------------------------------------------------------------------------
# B12 — Rejected answers for off-list question_ids
# ---------------------------------------------------------------------------


def test_rejected_answers_for_off_list_question_ids() -> None:
    _, _, _, questions = _build_initial_pipeline("I want to build something useful")
    interp = ClarificationInterpreterLLMService().interpret_answers(
        answers=[FrontChainAnswerItem(question_id="q_off_list", answer_text="something")],
        outstanding_questions=questions,
    )
    assert "q_off_list" in interp.rejected_answers
    assert interp.resolutions == []


# ---------------------------------------------------------------------------
# B13 — Policy engine drops resolved axes after reevaluation
# ---------------------------------------------------------------------------


def test_resolved_axis_disappears_from_top_questions() -> None:
    pf, ic, oc, questions = _build_initial_pipeline("I want to build something useful")
    src_q = _question_for_axis(questions, "source_locator")

    interp = ClarificationInterpreterLLMService().interpret_answers(
        answers=[FrontChainAnswerItem(question_id=src_q.question_id, answer_text="https://github.com/x/y")],
        outstanding_questions=questions,
    )
    update = ContractUpdateEngine().apply_resolutions(
        problem_formulation=pf, input_closure=ic, output_closure=oc, resolutions=interp.resolutions
    )
    reeval = ClosureReevaluationService().reevaluate(
        problem_formulation=update.problem_formulation,
        input_closure=update.input_closure,
        output_closure=update.output_closure,
    )

    new_axes = {q.axis for q in reeval.top_questions}
    assert "source_locator" not in new_axes
    # The other two unresolved axes should still be in the top questions.
    assert "target_object" in new_axes or "deliverable_form" in new_axes


# ---------------------------------------------------------------------------
# B14 — closure_status recomputed when blocking drained
# ---------------------------------------------------------------------------


def test_closure_status_promoted_when_blocking_drained() -> None:
    pf, ic, oc, questions = _build_initial_pipeline("I want to build something useful")
    src_q = _question_for_axis(questions, "source_locator")
    deliv_q = _question_for_axis(questions, "deliverable_form")

    interp = ClarificationInterpreterLLMService().interpret_answers(
        answers=[
            FrontChainAnswerItem(question_id=src_q.question_id, answer_text="/tmp/local/file.csv"),
            FrontChainAnswerItem(question_id=deliv_q.question_id, answer_text="markdown report"),
        ],
        outstanding_questions=questions,
    )
    update = ContractUpdateEngine().apply_resolutions(
        problem_formulation=pf, input_closure=ic, output_closure=oc, resolutions=interp.resolutions
    )

    assert update.input_closure.closure_status == "input_closed"
    assert update.output_closure.closure_status == "output_closed"


# ---------------------------------------------------------------------------
# B15 — No-op answer still creates a new chained row
# ---------------------------------------------------------------------------


def test_no_op_answer_still_creates_chained_state(client: TestClient) -> None:
    initial = _interpret(client, raw_intent="I want to build something useful")
    # Submit an answer with an unknown question_id → should be rejected,
    # producing a no-op turn but a fresh chained state row.
    response = client.post(
        f"/api/v1/front-chain/states/{initial['state_id']}/answer",
        json={"answers": [{"question_id": "off_list_xyz", "answer_text": "blah"}]},
    )
    assert response.status_code == 201, response.text
    new_state = response.json()
    assert new_state["parent_state_id"] == initial["state_id"]
    assert new_state["turn_number"] == 2
    # Contracts should be byte-equal to the parent (no fields touched).
    assert new_state["problem_formulation"] == initial["problem_formulation"]
    assert new_state["input_closure"] == initial["input_closure"]
    assert new_state["output_closure"] == initial["output_closure"]
    assert new_state["changed_contracts"] == []


# ---------------------------------------------------------------------------
# B16 — Phase 1B files still satisfy the original Phase 1A AST guardrail
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


def test_phase_1b_files_have_no_legacy_imports() -> None:
    targets = [
        _PHASE1B_BASE / "clarification_interpreter_llm_service.py",
        _PHASE1B_BASE / "contract_update_engine.py",
        _PHASE1B_BASE / "closure_reevaluation_service.py",
    ]
    violations: list[str] = []
    for path in targets:
        for module_name in _imported_modules(path):
            if module_name in _FORBIDDEN_LEGACY_MODULES or module_name.startswith(
                "app.services.requirement.compiler"
            ):
                violations.append(f"{path.name}: {module_name}")
    assert violations == [], (
        f"Phase 1B files must not import legacy modules. Violations: {violations}"
    )
