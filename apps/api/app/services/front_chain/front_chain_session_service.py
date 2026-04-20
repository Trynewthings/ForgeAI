"""Phase 2: FrontChainSessionService — session-level orchestrator.

This service is a thin auto-chaining adapter around
``FrontChainStateService``. A "session turn" submits either a raw intent
(first turn) or a set of answers (later turns); the service then runs the
four front-chain stages in order, short-circuiting whenever a stage
indicates progress should stop:

    interpret OR answer
       │
       │  (if contracts closed AND no blocking gaps)
       ▼
    evaluate
       │
       │  (if solvability != "not_solvable")
       ▼
    compile

The session's status is recomputed after every turn:
    * ``open``       — latest state has unresolved blocking gaps OR no compile yet
    * ``compilable`` — latest state carries a compile_context
    * ``closed``     — explicit user close action

Hard invariant: at most 2 LLM calls per turn (interpret/answer + evaluate).
Compile is deterministic.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import FrontChainSession, FrontChainState
from app.schemas.front_chain import (
    FrontChainAnswerRequest,
    FrontChainInterpretationRequest,
    FrontChainSessionResponse,
    FrontChainSessionSummary,
    FrontChainSessionTurnRequest,
    FrontChainSessionTurnResponse,
    FrontChainTurnStage,
)
from app.services.front_chain.front_chain_state_service import (
    FrontChainStateNotEvaluatedError,
    FrontChainStateNotFoundError,
    FrontChainStateService,
)

logger = logging.getLogger(__name__)


class FrontChainSessionNotFoundError(Exception):
    """Raised when a caller references a non-existent session."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"front_chain_session {session_id!r} not found")
        self.session_id = session_id


class FrontChainSessionStateMismatchError(Exception):
    """Raised when the submitted turn shape is inconsistent with session state.

    * ``kind="raw_intent"`` submitted to a session that already has a turn.
    * ``kind="answers"`` submitted to a fresh session with no prior state.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class FrontChainSessionService:
    def __init__(
        self,
        db: Session,
        *,
        state_service: Optional[FrontChainStateService] = None,
    ) -> None:
        self._db = db
        self._state_service = state_service or FrontChainStateService(db)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, *, project_id: str) -> FrontChainSessionResponse:
        row = FrontChainSession(project_id=project_id, status="open")
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        logger.info(
            "front_chain_session.created",
            extra={"session_id": row.id, "project_id": project_id},
        )
        return self._row_to_response(row, turns=[])

    def get(self, *, session_id: str) -> FrontChainSessionResponse:
        row = self._load(session_id)
        # Fetch rows directly in ASC turn_number order so the UI can render
        # the conversation from oldest to newest.
        states = (
            self._db.query(FrontChainState)
            .filter(FrontChainState.session_id == session_id)
            .order_by(FrontChainState.turn_number.asc(), FrontChainState.created_at.asc())
            .all()
        )
        turns = [self._state_service._row_to_response(state) for state in states]
        return self._row_to_response(row, turns=turns)

    def list_for_project(self, *, project_id: str) -> list[FrontChainSessionSummary]:
        rows = (
            self._db.query(FrontChainSession)
            .filter(FrontChainSession.project_id == project_id)
            .order_by(FrontChainSession.updated_at.desc())
            .all()
        )
        summaries: list[FrontChainSessionSummary] = []
        for row in rows:
            # One small query per session to fetch just the count + the first
            # state's raw_intent (cheap in practice; sessions are few).
            first_state = (
                self._db.query(FrontChainState)
                .filter(FrontChainState.session_id == row.id)
                .order_by(FrontChainState.turn_number.asc())
                .first()
            )
            turn_count = (
                self._db.query(FrontChainState)
                .filter(FrontChainState.session_id == row.id)
                .count()
            )
            summaries.append(
                FrontChainSessionSummary(
                    id=row.id,
                    project_id=row.project_id,
                    status=row.status,  # type: ignore[arg-type]
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    turn_count=turn_count,
                    latest_raw_intent=first_state.raw_intent if first_state else None,
                )
            )
        return summaries

    def close(self, *, session_id: str) -> FrontChainSessionResponse:
        row = self._load(session_id)
        row.status = "closed"
        self._db.commit()
        self._db.refresh(row)
        states = (
            self._db.query(FrontChainState)
            .filter(FrontChainState.session_id == session_id)
            .order_by(FrontChainState.turn_number.asc(), FrontChainState.created_at.asc())
            .all()
        )
        turns = [self._state_service._row_to_response(state) for state in states]
        return self._row_to_response(row, turns=turns)

    def submit_turn(
        self,
        *,
        session_id: str,
        request: FrontChainSessionTurnRequest,
    ) -> FrontChainSessionTurnResponse:
        session = self._load(session_id)
        if session.status == "closed":
            raise FrontChainSessionStateMismatchError(
                f"session {session_id!r} is closed; cannot accept new turns"
            )

        latest_state = (
            self._db.query(FrontChainState)
            .filter(FrontChainState.session_id == session_id)
            .order_by(FrontChainState.turn_number.desc())
            .first()
        )

        stages_run: list[FrontChainTurnStage] = []

        # --- Stage 1: interpret OR answer --------------------------------
        if request.kind == "raw_intent":
            if latest_state is not None:
                raise FrontChainSessionStateMismatchError(
                    f"session {session_id!r} already has a first turn; "
                    "subsequent turns must use kind='answers'"
                )
            if not request.raw_intent:
                raise FrontChainSessionStateMismatchError(
                    "raw_intent is required when kind='raw_intent'"
                )
            interpret_resp = self._state_service.interpret(
                FrontChainInterpretationRequest(
                    project_id=session.project_id,
                    raw_intent=request.raw_intent,
                    session_id=session_id,
                )
            )
            current_state_id = interpret_resp.state_id
            stages_run.append("interpret")
        else:  # request.kind == "answers"
            if latest_state is None:
                raise FrontChainSessionStateMismatchError(
                    f"session {session_id!r} has no prior turn; "
                    "first turn must use kind='raw_intent'"
                )
            if not request.answers:
                raise FrontChainSessionStateMismatchError(
                    "answers must be a non-empty list when kind='answers'"
                )
            answer_resp = self._state_service.apply_answers(
                parent_state_id=latest_state.id,
                request=FrontChainAnswerRequest(answers=request.answers),
            )
            current_state_id = answer_resp.state_id
            stages_run.append("answer")

        # --- Decide: proceed to evaluate? ---------------------------------
        current_state_resp = self._state_service.get_by_id(current_state_id)
        assert current_state_resp is not None

        merged_gaps = current_state_resp.merged_gaps
        top_questions = current_state_resp.top_questions
        input_closure = current_state_resp.input_closure.closure_status
        output_closure = current_state_resp.output_closure.closure_status
        problem_blocking = bool(current_state_resp.problem_formulation.blocking_axes)

        # Proceed to evaluate once all three closure layers are free of
        # blocking gaps and the policy engine has nothing left to ask. We
        # intentionally do NOT gate on ``problem_formulation.problem_status``
        # — it can lag behind the axis-level state after an answer, and
        # blocking_axes + top_questions are the authoritative signals.
        contracts_closed = (
            not merged_gaps.has_blocking
            and not top_questions
            and not problem_blocking
            and input_closure == "input_closed"
            and output_closure == "output_closed"
        )

        if not contracts_closed:
            # Stop; status stays open. Return the current state.
            self._set_status(session, "open")
            return FrontChainSessionTurnResponse(
                session_id=session_id,
                status="open",
                stages_run=stages_run,
                latest_state=current_state_resp,
                compile_context=None,
            )

        # --- Stage 2: evaluate --------------------------------------------
        # Runs unconditionally once contracts are closed. Any "not_solvable"
        # verdict is surfaced via the resulting compile_context's
        # ``solvability.solvability_status`` — the UI decides how to render
        # that — rather than by short-circuiting the chain here. Compile is
        # deterministic packaging (R7), so there's no cost to running it.
        eval_resp = self._state_service.evaluate(parent_state_id=current_state_id)
        current_state_id = eval_resp.state_id
        stages_run.append("evaluate")

        # --- Stage 3: compile --------------------------------------------
        compile_resp = self._state_service.compile(parent_state_id=current_state_id)
        stages_run.append("compile")
        self._set_status(session, "compilable")

        current_state_resp = self._state_service.get_by_id(compile_resp.state_id)
        assert current_state_resp is not None

        return FrontChainSessionTurnResponse(
            session_id=session_id,
            status="compilable",
            stages_run=stages_run,
            latest_state=current_state_resp,
            compile_context=compile_resp.compile_context,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load(self, session_id: str) -> FrontChainSession:
        row = (
            self._db.query(FrontChainSession)
            .filter(FrontChainSession.id == session_id)
            .first()
        )
        if row is None:
            raise FrontChainSessionNotFoundError(session_id)
        return row

    def _set_status(self, session: FrontChainSession, status: str) -> None:
        from datetime import datetime, timezone

        # Always bump updated_at so list_for_project ordering reflects
        # most-recent activity even when status doesn't change.
        session.status = status
        session.updated_at = datetime.now(timezone.utc)
        self._db.commit()
        self._db.refresh(session)

    def _row_to_response(
        self,
        row: FrontChainSession,
        *,
        turns: list,
    ) -> FrontChainSessionResponse:
        latest_compile_context = None
        if turns:
            for state in reversed(turns):
                if state.compile_context is not None:
                    latest_compile_context = state.compile_context
                    break
        return FrontChainSessionResponse(
            id=row.id,
            project_id=row.project_id,
            status=row.status,  # type: ignore[arg-type]
            created_at=row.created_at,
            updated_at=row.updated_at,
            turns=turns,
            latest_compile_context=latest_compile_context,
        )


__all__ = [
    "FrontChainSessionService",
    "FrontChainSessionNotFoundError",
    "FrontChainSessionStateMismatchError",
]

# Re-export for convenience
_ = FrontChainStateNotFoundError
_ = FrontChainStateNotEvaluatedError
