"""Phase 2 — FrontChainSessionService integration tests.

Coverage matrix:

    S1   create session -> status=open, empty turns
    S2   first turn with fully-closed intent -> auto-chains interpret+evaluate+compile
    S3   first turn with blocking intent -> stops at interpret, status=open
    S4   answer resolves the gap -> auto-chains to compile
    S5   get session returns turns in ascending order + latest_compile_context
    S6   list_for_project returns sessions ordered desc with summary
    S7   close session -> status=closed, further turns rejected
    S8   404 on unknown session
    S9   422 on kind=raw_intent submitted to session that already has a turn
    S10  422 on kind=answers submitted to session with no prior turn
    S11  stages_run accurately reports what executed
"""
from __future__ import annotations

from fastapi.testclient import TestClient


def _create_project(client: TestClient) -> dict:
    resp = client.post("/api/v1/projects", json={"name": "Phase 2 Session Test", "description": "t"})
    assert resp.status_code == 201
    return resp.json()


def _create_session(client: TestClient, project_id: str) -> dict:
    resp = client.post("/api/v1/front-chain/sessions", json={"project_id": project_id})
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# S1 — create empty session
# ---------------------------------------------------------------------------


def test_create_session_returns_open_status_with_no_turns(client: TestClient) -> None:
    project = _create_project(client)
    session = _create_session(client, project["id"])
    assert session["status"] == "open"
    assert session["turns"] == []
    assert session["latest_compile_context"] is None
    assert session["project_id"] == project["id"]


# ---------------------------------------------------------------------------
# S2 — first turn, fully closed intent auto-chains through compile
# ---------------------------------------------------------------------------


def test_turn_with_fully_closed_intent_auto_chains_to_compile(client: TestClient) -> None:
    project = _create_project(client)
    session = _create_session(client, project["id"])

    resp = client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={
            "kind": "raw_intent",
            "raw_intent": (
                "Analyze the repository at https://github.com/example/foo "
                "and produce a markdown report"
            ),
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "compilable"
    assert body["stages_run"] == ["interpret", "evaluate", "compile"]
    assert body["compile_context"] is not None
    assert body["compile_context"]["compile_trace"]["authority_source"] == "front_chain_v2"
    assert body["latest_state"]["compile_context"] is not None


# ---------------------------------------------------------------------------
# S3 — first turn blocking intent stops at interpret
# ---------------------------------------------------------------------------


def test_turn_with_blocking_intent_stops_at_interpret(client: TestClient) -> None:
    project = _create_project(client)
    session = _create_session(client, project["id"])

    resp = client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={
            # No URL -> input blocking; no deliverable keyword -> output blocking.
            "kind": "raw_intent",
            "raw_intent": "Do something useful",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "open"
    assert body["stages_run"] == ["interpret"]
    assert body["compile_context"] is None
    assert len(body["latest_state"]["top_questions"]) > 0


# ---------------------------------------------------------------------------
# S4 — answer drains blocking axes and chains through compile
# ---------------------------------------------------------------------------


def test_answer_turn_resolves_gap_and_auto_compiles(client: TestClient) -> None:
    project = _create_project(client)
    session = _create_session(client, project["id"])

    # Turn 1: blocking on deliverable_form
    resp1 = client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={
            "kind": "raw_intent",
            "raw_intent": "Analyze the repository at https://github.com/example/foo",
        },
    )
    assert resp1.status_code == 201
    body1 = resp1.json()
    assert body1["status"] == "open"
    deliverable_q = next(
        q for q in body1["latest_state"]["top_questions"] if q["axis"] == "deliverable_form"
    )

    # Turn 2: answer deliverable_form -> should drain gap and auto-chain.
    resp2 = client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={
            "kind": "answers",
            "answers": [
                {
                    "question_id": deliverable_q["question_id"],
                    "answer_text": "report",
                }
            ],
        },
    )
    assert resp2.status_code == 201, resp2.text
    body2 = resp2.json()
    assert body2["status"] == "compilable"
    assert body2["stages_run"] == ["answer", "evaluate", "compile"]
    assert body2["compile_context"] is not None


# ---------------------------------------------------------------------------
# S5 — get session returns ordered turns + latest_compile_context
# ---------------------------------------------------------------------------


def test_get_session_returns_ordered_turns_and_latest_compile_context(client: TestClient) -> None:
    project = _create_project(client)
    session = _create_session(client, project["id"])
    client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={
            "kind": "raw_intent",
            "raw_intent": (
                "Analyze the repository at https://github.com/example/foo "
                "and produce a markdown report"
            ),
        },
    )

    get_resp = client.get(f"/api/v1/front-chain/sessions/{session['id']}")
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["status"] == "compilable"
    # Turns should be in ascending turn_number order.
    turn_numbers = [t["turn_number"] for t in body["turns"]]
    assert turn_numbers == sorted(turn_numbers)
    assert len(turn_numbers) >= 3  # interpret + evaluate + compile each make a state row
    assert body["latest_compile_context"] is not None


# ---------------------------------------------------------------------------
# S6 — list_for_project ordering and summary fields
# ---------------------------------------------------------------------------


def test_list_sessions_for_project_returns_summaries_desc(client: TestClient) -> None:
    project = _create_project(client)
    s1 = _create_session(client, project["id"])
    s2 = _create_session(client, project["id"])
    client.post(
        f"/api/v1/front-chain/sessions/{s2['id']}/turn",
        json={
            "kind": "raw_intent",
            "raw_intent": "Analyze the repo at https://github.com/example/foo into a report",
        },
    )

    resp = client.get(f"/api/v1/front-chain/projects/{project['id']}/sessions")
    assert resp.status_code == 200
    summaries = resp.json()
    assert len(summaries) == 2
    ids = [s["id"] for s in summaries]
    # s2 was updated most recently -> appears first.
    assert ids[0] == s2["id"]
    # Summaries carry turn_count and latest_raw_intent.
    s2_summary = next(s for s in summaries if s["id"] == s2["id"])
    assert s2_summary["turn_count"] >= 1
    assert "github.com/example/foo" in (s2_summary["latest_raw_intent"] or "")


# ---------------------------------------------------------------------------
# S7 — close session rejects further turns
# ---------------------------------------------------------------------------


def test_close_session_rejects_further_turns(client: TestClient) -> None:
    project = _create_project(client)
    session = _create_session(client, project["id"])
    close_resp = client.post(f"/api/v1/front-chain/sessions/{session['id']}/close")
    assert close_resp.status_code == 200
    assert close_resp.json()["status"] == "closed"

    resp = client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={"kind": "raw_intent", "raw_intent": "anything"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# S8 — 404 on unknown session
# ---------------------------------------------------------------------------


def test_404_on_unknown_session(client: TestClient) -> None:
    # get
    assert client.get("/api/v1/front-chain/sessions/does-not-exist").status_code == 404
    # turn
    assert (
        client.post(
            "/api/v1/front-chain/sessions/does-not-exist/turn",
            json={"kind": "raw_intent", "raw_intent": "x"},
        ).status_code
        == 404
    )
    # close
    assert client.post("/api/v1/front-chain/sessions/does-not-exist/close").status_code == 404


# ---------------------------------------------------------------------------
# S9 — raw_intent on a session that already has a turn -> 422
# ---------------------------------------------------------------------------


def test_raw_intent_on_non_fresh_session_is_422(client: TestClient) -> None:
    project = _create_project(client)
    session = _create_session(client, project["id"])
    client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={"kind": "raw_intent", "raw_intent": "any intent"},
    )
    resp = client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={"kind": "raw_intent", "raw_intent": "another intent"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# S10 — answers on a fresh session (no prior turn) -> 422
# ---------------------------------------------------------------------------


def test_answers_on_fresh_session_is_422(client: TestClient) -> None:
    project = _create_project(client)
    session = _create_session(client, project["id"])
    resp = client.post(
        f"/api/v1/front-chain/sessions/{session['id']}/turn",
        json={
            "kind": "answers",
            "answers": [{"question_id": "anything", "answer_text": "report"}],
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# S11 — stages_run accuracy
# ---------------------------------------------------------------------------


def test_stages_run_accurately_reflects_short_circuit(client: TestClient) -> None:
    project = _create_project(client)

    # Case A: blocking intent -> only interpret runs
    session_a = _create_session(client, project["id"])
    resp_a = client.post(
        f"/api/v1/front-chain/sessions/{session_a['id']}/turn",
        json={"kind": "raw_intent", "raw_intent": "Do stuff"},
    )
    assert resp_a.json()["stages_run"] == ["interpret"]

    # Case B: fully closed -> interpret + evaluate + compile
    session_b = _create_session(client, project["id"])
    resp_b = client.post(
        f"/api/v1/front-chain/sessions/{session_b['id']}/turn",
        json={
            "kind": "raw_intent",
            "raw_intent": "Analyze the repo at https://github.com/example/foo into a report",
        },
    )
    assert resp_b.json()["stages_run"] == ["interpret", "evaluate", "compile"]
