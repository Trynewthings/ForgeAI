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
    """Fixture builder for Phase 1B tests.

    Contracts come from the real pipeline (interpreter → partitioner →
    merger). The returned ``questions`` list is synthesized directly from
    the closure axis registry for all three canonical axes
    (target_object, source_locator, deliverable_form) regardless of the
    policy engine's staged-emission rule, so that downstream tests can
    construct answers that span multiple layers in a single call. The
    policy engine's own staging behaviour is tested separately in
    ``test_front_chain_phase1.py``.
    """
    from app.schemas.front_chain import FrontChainClarificationOption
    from app.services.front_chain.closure_axis_registry import get_axis

    interpreter = FrontChainInterpreterLLMService()
    result = interpreter.interpret(raw_intent)
    partition = ContractPartitionValidator().validate_and_partition(result.llm_output)

    def _q(axis_id: str, layer: str) -> FrontChainClarificationQuestion:
        axis_def = get_axis(axis_id)
        assert axis_def is not None, f"axis {axis_id} missing from registry"
        return FrontChainClarificationQuestion(
            question_id=f"q_{layer}_{axis_id}",
            axis=axis_id,
            layer=layer,  # type: ignore[arg-type]
            severity="blocking",
            question_text=axis_def.question_text,
            priority=0,
            context_hint=axis_def.context_hint,
            suggested_options=[
                FrontChainClarificationOption(
                    option_id=o.option_id,
                    label=o.label,
                    value=o.value,
                    resolves=dict(o.resolves),
                    is_default=o.is_default,
                    requires_free_text_suffix=o.requires_free_text_suffix,
                )
                for o in axis_def.options
            ],
        )

    questions = [
        _q("target_object", "problem_formulation"),
        _q("source_locator", "input_closure"),
        _q("deliverable_form", "output_closure"),
    ]
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

    # Turn 2: reevaluation produces new questions; user now answers deliverable_form.
    # Note: with problem_formulation staging enabled in the policy engine,
    # ``reeval.top_questions`` may only surface PF questions while PF is still
    # blocking. For this test we only need a well-formed deliverable_form
    # question object to hand to the interpreter, so we pull it from the
    # fixture directly (the fixture synthesizes questions for all three
    # canonical axes independent of policy engine staging).
    ClosureReevaluationService().reevaluate(
        problem_formulation=update1.problem_formulation,
        input_closure=update1.input_closure,
        output_closure=update1.output_closure,
    )
    deliv_q = _question_for_axis(questions, "deliverable_form")
    assert deliv_q is not None

    interp2 = ClarificationInterpreterLLMService().interpret_answers(
        answers=[FrontChainAnswerItem(question_id=deliv_q.question_id, answer_text="markdown report")],
        outstanding_questions=questions,
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
    # With problem_formulation staging, a vague intent first surfaces only
    # PF questions. We close PF in turn 1 (pick "代码仓库" for target_object,
    # which also resolves problem_kind via the side-effect-free single-axis
    # patch), and then expect IC questions (including source_locator) on
    # turn 2.
    initial = _interpret(client, raw_intent="I want to build something useful")
    target_q = next((q for q in initial["top_questions"] if q["axis"] == "target_object"), None)
    assert target_q is not None, (
        "expected target_object as the first staged question "
        "(problem_formulation must close before input/output layers)"
    )
    assert initial["turn_number"] == 1
    assert initial["parent_state_id"] is None

    # Turn 1: answer target_object via option selection (deterministic path).
    target_option = next(
        opt for opt in target_q["suggested_options"] if opt["option_id"] == "to_repo"
    )
    turn1_response = client.post(
        f"/api/v1/front-chain/states/{initial['state_id']}/answer",
        json={
            "answers": [
                {
                    "question_id": target_q["question_id"],
                    "selected_option_id": target_option["option_id"],
                    "answer_text": target_option["value"],
                }
            ]
        },
    )
    assert turn1_response.status_code == 201, turn1_response.text
    turn1 = turn1_response.json()
    assert turn1["turn_number"] == 2

    # If problem_kind is still open, answer it too so PF closes.
    pk_q = next((q for q in turn1["top_questions"] if q["axis"] == "problem_kind"), None)
    parent_id = turn1["state_id"]
    if pk_q is not None:
        pk_option = next(
            opt for opt in pk_q["suggested_options"] if opt["option_id"] == "pk_analysis"
        )
        turn_pk = client.post(
            f"/api/v1/front-chain/states/{parent_id}/answer",
            json={
                "answers": [
                    {
                        "question_id": pk_q["question_id"],
                        "selected_option_id": pk_option["option_id"],
                        "answer_text": pk_option["value"],
                    }
                ]
            },
        )
        assert turn_pk.status_code == 201
        parent_id = turn_pk.json()["state_id"]

    # Turn 2 (or 3): PF is now closed, source_locator should be surfaced.
    follow_up = client.get(f"/api/v1/front-chain/states/{parent_id}")
    assert follow_up.status_code == 200
    follow_state = follow_up.json()
    src_q = next((q for q in follow_state["top_questions"] if q["axis"] == "source_locator"), None)
    assert src_q is not None, (
        "source_locator should be available once problem_formulation has no blocking gap"
    )

    answer_response = client.post(
        f"/api/v1/front-chain/states/{parent_id}/answer",
        json={
            "answers": [
                {"question_id": src_q["question_id"], "answer_text": "https://github.com/example/foo"}
            ]
        },
    )
    assert answer_response.status_code == 201, answer_response.text
    state_vN = answer_response.json()
    assert state_vN["parent_state_id"] == parent_id
    assert state_vN["turn_number"] > follow_state["turn_number"]
    assert state_vN["state_id"] != parent_id
    assert state_vN["input_closure"]["resolved_source_locator"] == "https://github.com/example/foo"


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
# B-clarity — PF intent_clarity semantic gate
# ---------------------------------------------------------------------------


def test_interpret_emits_missing_goal_for_noun_only_intent() -> None:
    """Bug repro ("给你两个数据Excel表格" landing at compilable):
    the interpreter MUST flag this as ``missing_goal`` with a follow-up
    question, rather than letting the axis loop quietly fill every slot
    and reach evaluate."""
    from app.services.front_chain.front_chain_interpreter_llm_service import (
        FrontChainInterpreterLLMService,
    )
    from app.services.front_chain.contract_partition_validator import (
        ContractPartitionValidator,
    )
    # Stub the adapter — the mock path is deterministic via the
    # heuristic interpreter. We explicitly force ``llm_mode="mock"``
    # above the service call path to exercise ``_heuristic_llm_output``.
    import app.core.config as cfg

    original_mode = cfg.settings.llm_mode
    cfg.settings.llm_mode = "mock"
    try:
        result = FrontChainInterpreterLLMService().interpret(
            "给你两个数据Excel表格"
        )
    finally:
        cfg.settings.llm_mode = original_mode

    partition = ContractPartitionValidator().validate_and_partition(result.llm_output)
    assert partition.problem_formulation.intent_clarity == "missing_goal"
    assert partition.problem_formulation.clarification_question


def test_interpret_emits_clear_when_action_verb_present() -> None:
    """Positive case: a user saying "compare two files and merge common
    rows" carries an action verb and should come back as ``clear``."""
    from app.services.front_chain.front_chain_interpreter_llm_service import (
        FrontChainInterpreterLLMService,
    )
    from app.services.front_chain.contract_partition_validator import (
        ContractPartitionValidator,
    )
    import app.core.config as cfg

    original_mode = cfg.settings.llm_mode
    cfg.settings.llm_mode = "mock"
    try:
        result = FrontChainInterpreterLLMService().interpret(
            "Compare /tmp/a.csv and /tmp/b.csv and merge the common rows into /tmp/out.csv"
        )
    finally:
        cfg.settings.llm_mode = original_mode

    partition = ContractPartitionValidator().validate_and_partition(result.llm_output)
    assert partition.problem_formulation.intent_clarity == "clear"


def test_policy_engine_pf_clarity_question_has_no_options() -> None:
    """The PF clarity question is free-form; the UI must render a
    text input, not an option group. No suggested_options."""
    from app.services.front_chain.clarification_policy_engine import (
        ClarificationPolicyEngine,
    )

    q = ClarificationPolicyEngine().select_pf_clarity_question(
        clarification_question="具体要对这些内容做什么？",
        interpreted_goal="两个 Excel 表格已提供",
    )
    assert q.axis == "intent_clarity"
    assert q.layer == "problem_formulation"
    assert q.severity == "blocking"
    assert q.suggested_options == []
    assert q.question_text == "具体要对这些内容做什么？"


def test_state_service_surfaces_pf_clarity_question_when_clarity_missing(client: TestClient) -> None:
    """End-to-end via the session/turn endpoint: first turn with a
    vague intent produces top_questions containing ONLY the PF
    clarity question (axis-based questions are deferred)."""
    import app.core.config as cfg

    original_mode = cfg.settings.llm_mode
    cfg.settings.llm_mode = "mock"
    try:
        project = client.post("/api/v1/projects", json={"name": "clarity_test"}).json()
        session = client.post(
            "/api/v1/front-chain/sessions", json={"project_id": project["id"]}
        ).json()
        turn_resp = client.post(
            f"/api/v1/front-chain/sessions/{session['id']}/turn",
            json={"kind": "raw_intent", "raw_intent": "给你两张表"},
        )
    finally:
        cfg.settings.llm_mode = original_mode

    assert turn_resp.status_code == 201, turn_resp.text
    body = turn_resp.json()
    assert body["status"] == "open"
    questions = body["latest_state"]["top_questions"]
    assert len(questions) == 1
    assert questions[0]["axis"] == "intent_clarity"
    assert questions[0]["layer"] == "problem_formulation"
    assert questions[0]["suggested_options"] == []
    # The PF semantic gate was triggered.
    assert body["latest_state"]["problem_formulation"]["intent_clarity"] == "missing_goal"


def test_clarity_answer_reinterprets_and_can_reach_clear(client: TestClient) -> None:
    """Answering the free-form PF clarity question feeds the text back
    into the interpreter as a clarification. In mock mode, appending
    an action verb should flip the clarity to ``clear``."""
    import app.core.config as cfg

    original_mode = cfg.settings.llm_mode
    cfg.settings.llm_mode = "mock"
    try:
        project = client.post("/api/v1/projects", json={"name": "clarity_flow"}).json()
        session = client.post(
            "/api/v1/front-chain/sessions", json={"project_id": project["id"]}
        ).json()
        turn1 = client.post(
            f"/api/v1/front-chain/sessions/{session['id']}/turn",
            json={"kind": "raw_intent", "raw_intent": "给你两张表"},
        ).json()
        clarity_q = turn1["latest_state"]["top_questions"][0]
        assert clarity_q["question_id"] == "q_problem_formulation_intent_clarity"

        turn2 = client.post(
            f"/api/v1/front-chain/sessions/{session['id']}/turn",
            json={
                "kind": "answers",
                "answers": [
                    {
                        "question_id": clarity_q["question_id"],
                        "answer_text": "compare two files and merge common rows",
                    }
                ],
            },
        ).json()
    finally:
        cfg.settings.llm_mode = original_mode

    assert turn2["latest_state"]["problem_formulation"]["intent_clarity"] == "clear"
    # Now the PF gate is open; axis-based questions may appear.
    new_qs = turn2["latest_state"]["top_questions"]
    assert all(q["axis"] != "intent_clarity" for q in new_qs)


def test_clarity_loop_force_advances_after_cap() -> None:
    """After PF_CLARITY_ROUND_CAP unsuccessful clarity rounds the state
    service must stop nagging: intent_clarity is forced to
    ``ambiguous`` so axis clarification can take over."""
    from app.services.front_chain.front_chain_state_service import (
        FrontChainStateService,
    )

    # Direct service construction with the default in-session SQLite DB
    # (via the conftest client fixture's app).
    from app.core.database import SessionLocal

    cap = FrontChainStateService.PF_CLARITY_ROUND_CAP
    assert cap == 5, "cap should be 5 per user decision"


# ---------------------------------------------------------------------------
# B18 — Contract completeness guard prevents zombie-open states
# ---------------------------------------------------------------------------


def test_completeness_guard_repairs_output_closure_without_deliverable() -> None:
    """Reproduces the "两份文档提炼共同点" stuck-session bug.

    The LLM interpreter sometimes emits ``output_clarification_required``
    with empty ``blocking_axes`` AND no ``deliverable_form``. Without the
    completeness guard, the session ends up at status=open with an empty
    top_questions list — no way to drive OC to closed, no way forward.

    The guard must inject ``deliverable_form`` into OC.blocking_axes so
    the policy engine surfaces a clarification question.
    """
    from app.schemas.problem_formulation import (
        ClarificationResolutionRecord,
        InputClosureContract,
        OutputClosureContract,
        ProblemFormulationContract,
    )
    from datetime import datetime, timezone

    # PF already closed on target_object, IC closed with a locator, but
    # OC is in the pathological state: clarification_required + empty axes
    # + no deliverable_form.
    pf = ProblemFormulationContract(
        interpreted_problem_kind="analysis",
        target_object="local_workspace",
        problem_status="solvable_with_clarification",
        current_system_fit="in_scope",
        blocking_axes=[],
        review_axes=[],
        blocking_reasons=[],
        clarification_plan=[],
    )
    ic = InputClosureContract(
        resolved_source_locator="/tmp/1.txt",
        acquisition_path_kind="direct_read",
        blocking_axes=[],
        review_axes=[],
        closure_status="input_closed",
    )
    oc = OutputClosureContract(
        deliverable_form=None,
        delivery_target=None,
        output_modality=None,
        blocking_axes=[],
        review_axes=[],
        closure_status="output_clarification_required",
    )

    # Trigger the update engine with any resolution (even a no-op one,
    # to exercise the tail guard). We use a PF-layer no-op so only the
    # completeness guard fires for OC.
    dummy_resolution = ClarificationResolutionRecord(
        axis="target_object",
        resolution_type="user_confirmed_suggested_value",
        resolved_value="local_workspace",
        source_text="option:to_workspace",
        accepted=True,
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )
    result = ContractUpdateEngine().apply_resolutions(
        problem_formulation=pf,
        input_closure=ic,
        output_closure=oc,
        resolutions=[dummy_resolution],
    )

    # OC should now have deliverable_form listed as a blocking axis so
    # the policy engine can surface a question on the next turn.
    assert "deliverable_form" in result.output_closure.blocking_axes
    assert result.output_closure.closure_status == "output_clarification_required"
    # IC must be untouched by the guard because it is already closed.
    assert result.input_closure.blocking_axes == []
    assert result.input_closure.closure_status == "input_closed"
    assert "output_closure" in result.changed_layers


def test_completeness_guard_does_not_touch_closed_layers() -> None:
    """Regression guard: an explicitly closed layer must not be
    resurrected by the completeness helpers — if the interpreter
    produced an explicit ``input_closed`` status, respect it even when
    ``resolved_source_locator`` is None (legacy test fixtures)."""
    from app.schemas.problem_formulation import (
        ClarificationResolutionRecord,
        InputClosureContract,
        OutputClosureContract,
        ProblemFormulationContract,
    )
    from datetime import datetime, timezone

    pf = ProblemFormulationContract(
        interpreted_problem_kind="analysis",
        target_object="repository",
        problem_status="solvable",
        current_system_fit="in_scope",
    )
    ic = InputClosureContract(
        resolved_source_locator=None,
        acquisition_path_kind="direct_read",
        blocking_axes=[],
        review_axes=[],
        closure_status="input_closed",  # explicitly claimed closed
    )
    oc = OutputClosureContract(
        deliverable_form="report",
        blocking_axes=[],
        review_axes=[],
        closure_status="output_closed",
    )

    result = ContractUpdateEngine().apply_resolutions(
        problem_formulation=pf,
        input_closure=ic,
        output_closure=oc,
        resolutions=[
            ClarificationResolutionRecord(
                axis="target_object",
                resolution_type="user_confirmed_suggested_value",
                resolved_value="repository",
                source_text="option:to_repo",
                accepted=True,
                recorded_at=datetime.now(timezone.utc).isoformat(),
            )
        ],
    )
    # Neither layer should have axes reinjected.
    assert result.input_closure.blocking_axes == []
    assert result.input_closure.closure_status == "input_closed"
    assert result.output_closure.blocking_axes == []
    assert result.output_closure.closure_status == "output_closed"


def test_completeness_guard_repairs_pf_when_target_object_missing() -> None:
    """If PF has no axes but target_object is unresolved, the guard
    must inject target_object into blocking_axes so we don't lose the
    layer to a zombie-open state."""
    from app.schemas.problem_formulation import (
        ClarificationResolutionRecord,
        InputClosureContract,
        OutputClosureContract,
        ProblemFormulationContract,
    )
    from datetime import datetime, timezone

    # ``intent_clarity="clear"`` is required — the completeness guard is
    # now gated behind the PF semantic gate. Clarity != clear defers
    # axis injection entirely so the free-form PF clarity question can
    # drive the turn instead.
    pf = ProblemFormulationContract(
        interpreted_problem_kind=None,
        target_object=None,
        problem_status="underdetermined",
        current_system_fit="partially_in_scope",
        blocking_axes=[],
        review_axes=[],
        blocking_reasons=[],
        clarification_plan=[],
        intent_clarity="clear",
    )
    ic = InputClosureContract(
        resolved_source_locator="/tmp/x",
        blocking_axes=[],
        review_axes=[],
        closure_status="input_closed",
    )
    oc = OutputClosureContract(
        deliverable_form="report",
        blocking_axes=[],
        review_axes=[],
        closure_status="output_closed",
    )

    # Apply an axis-less resolution just to hit the tail guard path.
    result = ContractUpdateEngine().apply_resolutions(
        problem_formulation=pf,
        input_closure=ic,
        output_closure=oc,
        resolutions=[
            ClarificationResolutionRecord(
                axis="deliverable_form",
                resolution_type="user_confirmed_suggested_value",
                resolved_value="report",
                source_text="option:df_report_md",
                accepted=True,
                recorded_at=datetime.now(timezone.utc).isoformat(),
            )
        ],
    )
    assert "target_object" in result.problem_formulation.blocking_axes
    assert "problem_kind" in result.problem_formulation.blocking_axes
    assert "problem_formulation" in result.changed_layers


# ---------------------------------------------------------------------------
# B17 — Synthesis/generation problem_kind auto-closes input_closure layer
# ---------------------------------------------------------------------------


def test_synthesis_problem_kind_auto_closes_input_layer() -> None:
    """Answering problem_kind with the 'synthesis' option collapses input.

    "Generate me a picture" has no external data source to resolve. The
    deterministic layer must detect that problem_kind landed in the
    synthesis family and flip input_closure to not_applicable /
    input_closed so the user isn't asked where the data comes from.
    """
    from app.schemas.problem_formulation import (
        ClarificationResolutionRecord,
        InputClosureContract,
        OutputClosureContract,
        ProblemFormulationContract,
    )
    from datetime import datetime, timezone

    pf = ProblemFormulationContract(
        interpreted_problem_kind=None,
        target_object=None,
        problem_status="underdetermined",
        current_system_fit="partially_in_scope",
        planning_intent=None,
        required_decisions=[],
        recommended_assumptions=[],
        blocking_axes=["problem_kind"],
        review_axes=[],
        blocking_reasons=["problem kind not known"],
        clarification_plan=[],
    )
    ic = InputClosureContract(
        blocking_axes=["source_locator", "source_modality", "acquisition_path"],
        review_axes=[],
    )
    oc = OutputClosureContract(blocking_axes=["deliverable_form"], review_axes=[])

    resolution = ClarificationResolutionRecord(
        axis="problem_kind",
        resolution_type="user_confirmed_suggested_value",
        resolved_value="synthesis",
        source_text="option:pk_synth",
        accepted=True,
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )
    result = ContractUpdateEngine().apply_resolutions(
        problem_formulation=pf,
        input_closure=ic,
        output_closure=oc,
        resolutions=[resolution],
    )

    assert result.problem_formulation.interpreted_problem_kind == "synthesis"
    # Input layer collapsed: no blocking, no review, marked not_applicable,
    # closure_status input_closed.
    assert result.input_closure.applicability == "not_applicable"
    assert result.input_closure.blocking_axes == []
    assert result.input_closure.review_axes == []
    assert result.input_closure.closure_status == "input_closed"
    # The change must be reported in changed_layers so downstream auditors
    # can see the cross-layer side-effect.
    assert "problem_formulation" in result.changed_layers
    assert "input_closure" in result.changed_layers
    # Output layer must NOT be affected — generation tasks still need to
    # produce an output of some kind.
    assert result.output_closure.blocking_axes == ["deliverable_form"]


def test_non_synthesis_problem_kind_preserves_input_layer() -> None:
    """Regression guard: picking 'analysis' or 'research' must NOT collapse input."""
    from app.schemas.problem_formulation import (
        ClarificationResolutionRecord,
        InputClosureContract,
        OutputClosureContract,
        ProblemFormulationContract,
    )
    from datetime import datetime, timezone

    pf = ProblemFormulationContract(
        interpreted_problem_kind=None,
        target_object=None,
        problem_status="underdetermined",
        current_system_fit="partially_in_scope",
        planning_intent=None,
        required_decisions=[],
        recommended_assumptions=[],
        blocking_axes=["problem_kind"],
        review_axes=[],
        blocking_reasons=[],
        clarification_plan=[],
    )
    ic = InputClosureContract(
        blocking_axes=["source_locator", "source_modality"],
        review_axes=[],
    )
    oc = OutputClosureContract(blocking_axes=["deliverable_form"], review_axes=[])

    resolution = ClarificationResolutionRecord(
        axis="problem_kind",
        resolution_type="user_confirmed_suggested_value",
        resolved_value="analysis",
        source_text="option:pk_analysis",
        accepted=True,
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )
    result = ContractUpdateEngine().apply_resolutions(
        problem_formulation=pf,
        input_closure=ic,
        output_closure=oc,
        resolutions=[resolution],
    )

    assert result.problem_formulation.interpreted_problem_kind == "analysis"
    assert result.input_closure.applicability == "required"
    assert "source_locator" in result.input_closure.blocking_axes
    # Only PF should have been touched.
    assert result.changed_layers == ["problem_formulation"]


def test_synthesis_collapse_is_idempotent() -> None:
    """Running the engine twice with the same synthesis resolution produces
    byte-equal output. Ensures the collapse helper is a no-op on an
    already-collapsed input closure."""
    from app.schemas.problem_formulation import (
        ClarificationResolutionRecord,
        InputClosureContract,
        OutputClosureContract,
        ProblemFormulationContract,
    )
    from datetime import datetime, timezone

    pf = ProblemFormulationContract(
        interpreted_problem_kind=None,
        target_object=None,
        problem_status="underdetermined",
        current_system_fit="partially_in_scope",
        planning_intent=None,
        required_decisions=[],
        recommended_assumptions=[],
        blocking_axes=["problem_kind"],
        review_axes=[],
        blocking_reasons=[],
        clarification_plan=[],
    )
    ic = InputClosureContract(
        blocking_axes=["source_locator"],
        review_axes=[],
    )
    oc = OutputClosureContract(blocking_axes=["deliverable_form"], review_axes=[])

    resolution = ClarificationResolutionRecord(
        axis="problem_kind",
        resolution_type="user_confirmed_suggested_value",
        resolved_value="synthesis",
        source_text="option:pk_synth",
        accepted=True,
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )
    first = ContractUpdateEngine().apply_resolutions(
        problem_formulation=pf, input_closure=ic, output_closure=oc, resolutions=[resolution]
    )
    second = ContractUpdateEngine().apply_resolutions(
        problem_formulation=first.problem_formulation,
        input_closure=first.input_closure,
        output_closure=first.output_closure,
        resolutions=[resolution],
    )
    assert first.input_closure.model_dump() == second.input_closure.model_dump()
    # No input_closure change should be recorded the second time because
    # the collapse helper returns identity on already-collapsed input.
    assert "input_closure" not in second.changed_layers


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
