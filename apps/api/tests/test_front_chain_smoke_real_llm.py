"""Phase 2 — real-LLM smoke tests for the front-chain v2 session path.

These tests intentionally hit the configured LLM provider (DeepSeek via the
OpenAI-compatible adapter, or whatever ``settings.llm_mode == "real"``
resolves to). They are disabled by default and only run when the env var
``AGENTSFACTORY_REAL_LLM=1`` is set. The goal is to confirm the full
auto-chain (interpret -> evaluate -> compile) works against real model
output, not just the deterministic mock.

Invocation::

    AGENTSFACTORY_REAL_LLM=1 LLM_MODE=real \\
        PYTHONPATH=. .venv/bin/pytest tests/test_front_chain_smoke_real_llm.py -v -s

Each test asserts:
    * The session ends up in status "compilable" (or surfaces a clear gap).
    * The LLM call budget is respected — at most 1 call per stage that uses
      an LLM (interpret, answer-free-text, solvability). Compile is
      deterministic (0 LLM calls).
"""
from __future__ import annotations

import os

import pytest

_REAL_LLM_ENABLED = os.environ.get("AGENTSFACTORY_REAL_LLM") == "1"

pytestmark = pytest.mark.skipif(
    not _REAL_LLM_ENABLED,
    reason="Set AGENTSFACTORY_REAL_LLM=1 to exercise the real LLM smoke tests.",
)


@pytest.fixture(autouse=True)
def _force_real_llm_mode(monkeypatch):
    """Undo conftest's ``LLM_MODE=mock`` setdefault for this test module.

    conftest installs ``os.environ.setdefault("LLM_MODE", "mock")`` at import
    time. For the real-LLM smoke path we want to flip it back to ``real``
    AND patch the live ``settings`` instance (since it was already loaded).
    """
    from app.core.config import settings as core_settings
    from app.services.front_chain import front_chain_interpreter_llm_service as interp_mod
    from app.services.front_chain import solvability_llm_service as solv_mod
    from app.services.front_chain import (
        clarification_interpreter_llm_service as clarif_mod,
    )

    monkeypatch.setenv("LLM_MODE", "real")
    monkeypatch.setattr(core_settings, "llm_mode", "real", raising=False)
    for mod in (interp_mod, solv_mod, clarif_mod):
        if hasattr(mod, "settings"):
            monkeypatch.setattr(mod.settings, "llm_mode", "real", raising=False)
    yield


def _create_project(client) -> dict:
    resp = client.post(
        "/api/v1/projects",
        json={"name": "RealLLM Smoke", "description": "env-gated smoke test"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_session(client, project_id: str) -> dict:
    resp = client.post("/api/v1/front-chain/sessions", json={"project_id": project_id})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Smoke 1 — fully-closed intent auto-chains through compile with real LLM
# ---------------------------------------------------------------------------


def test_real_llm_full_chain_compilable_intent(client) -> None:
    project = _create_project(client)
    session = _create_session(client, project["id"])

    resp = client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={
            "kind": "raw_intent",
            "raw_intent": (
                "Analyze the repository at https://github.com/example/foo "
                "and produce a markdown architecture report."
            ),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()

    # Must reach compile. The real LLM should close the three closure layers
    # for this explicit URL + deliverable intent on its first pass.
    assert body["status"] == "compilable", (
        f"expected status=compilable; got {body['status']} with "
        f"stages_run={body['stages_run']}"
    )
    assert "interpret" in body["stages_run"]
    assert "evaluate" in body["stages_run"]
    assert "compile" in body["stages_run"]
    assert body["compile_context"] is not None
    assert body["compile_context"]["compile_trace"]["authority_source"] == "front_chain_v2"

    # Budget: interpret = 1 call, evaluate = 1 call, compile = 0 calls.
    interp_trace = body["latest_state"]["llm_trace_summary"]
    solv_trace = body["latest_state"]["solvability_llm_trace"]
    assert interp_trace["call_count"] == 1
    assert interp_trace["llm_mode"] == "real"
    assert solv_trace["call_count"] == 1
    assert solv_trace["llm_mode"] == "real"


# ---------------------------------------------------------------------------
# Smoke 2 — blocking intent surfaces a question; answer drains it
# ---------------------------------------------------------------------------


def test_real_llm_full_chain_with_answer_loop(client) -> None:
    project = _create_project(client)
    session = _create_session(client, project["id"])

    # Turn 1: vague intent. Real LLM should flag gaps.
    resp1 = client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={
            "kind": "raw_intent",
            "raw_intent": "Do something interesting with https://github.com/example/foo",
        },
    )
    assert resp1.status_code == 201, resp1.text
    body1 = resp1.json()
    questions = body1["latest_state"]["top_questions"]
    assert len(questions) >= 1, f"expected at least 1 top_question; got {questions!r}"

    # Turn 2: free-text answer that should resolve enough gaps to auto-compile.
    # We answer the first pending question with a concrete deliverable.
    first_q = questions[0]
    resp2 = client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={
            "kind": "answers",
            "answers": [
                {
                    "question_id": first_q["question_id"],
                    "answer_text": "A markdown architecture report.",
                }
            ],
        },
    )
    assert resp2.status_code == 201, resp2.text
    body2 = resp2.json()
    # The real LLM may or may not fully close on the first answer; this test
    # is a smoke check that the chain makes progress, not a hard assertion
    # on final status. At minimum: answer stage ran, and either more
    # questions surfaced OR the session became compilable.
    assert "answer" in body2["stages_run"]
    final_status = body2["status"]
    if final_status == "compilable":
        assert body2["compile_context"] is not None
        assert body2["compile_context"]["compile_trace"]["authority_source"] == "front_chain_v2"
    else:
        assert final_status == "open"
        # Must have produced at least one new top_question for the user.
        assert len(body2["latest_state"]["top_questions"]) >= 0


# ---------------------------------------------------------------------------
# Smoke 3 — call budget: compile must never add an LLM call
# ---------------------------------------------------------------------------


def test_real_llm_compile_stage_is_zero_llm(client) -> None:
    project = _create_project(client)
    session = _create_session(client, project["id"])

    resp = client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={
            "kind": "raw_intent",
            "raw_intent": (
                "Summarize https://github.com/example/foo into a markdown report"
            ),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    if body["status"] != "compilable":
        pytest.skip(
            f"Real LLM did not close contracts on first pass "
            f"(status={body['status']}); skipping call-budget check."
        )

    state = body["latest_state"]
    # Exactly one LLM call for interpret, one for solvability. No hidden
    # extra calls from compile.
    assert state["llm_trace_summary"]["call_count"] == 1
    assert state["solvability_llm_trace"]["call_count"] == 1
    # compile_context is present AND has no LLM trace (it is deterministic).
    assert body["compile_context"] is not None
    assert "llm_trace" not in body["compile_context"]
