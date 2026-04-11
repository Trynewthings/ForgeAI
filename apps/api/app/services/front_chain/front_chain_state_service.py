"""FrontChainState ORM service (Phase 1A).

The sole persistence layer for the new front-chain authority island. This
service:
    * Orchestrates interpreter -> validator -> merger -> policy engine
    * Enforces the session_id / requirement_id / preview_mode binding
      invariant at the application layer (DB CHECK is the belt-and-braces)
    * Writes to ``front_chain_states`` only — never to Requirement,
      RequirementSession, or any legacy JSON column.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models import FrontChainState
from app.schemas.front_chain import (
    FrontChainClarificationQuestion,
    FrontChainInterpretationRequest,
    FrontChainInterpretationResponse,
    FrontChainLLMTraceSummary,
    FrontChainStateResponse,
    MergedGapSet,
)
from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.services.front_chain.clarification_policy_engine import (
    POLICY_VERSION,
    ClarificationPolicyEngine,
)
from app.services.front_chain.closure_gap_merger import (
    MERGER_VERSION,
    ClosureGapMerger,
)
from app.services.front_chain.contract_partition_validator import (
    VALIDATOR_VERSION,
    ContractPartitionValidator,
)
from app.services.front_chain.front_chain_interpreter_llm_service import (
    INTERPRETER_VERSION,
    FrontChainInterpreterLLMService,
)

logger = logging.getLogger(__name__)


class FrontChainStateService:
    """Phase 1A orchestrator.

    Example::

        service = FrontChainStateService(db)
        response = service.interpret(request)  # full understand pass + persist
    """

    def __init__(
        self,
        db: Session,
        *,
        interpreter: Optional[FrontChainInterpreterLLMService] = None,
        validator: Optional[ContractPartitionValidator] = None,
        merger: Optional[ClosureGapMerger] = None,
        policy_engine: Optional[ClarificationPolicyEngine] = None,
    ) -> None:
        self._db = db
        self._interpreter = interpreter or FrontChainInterpreterLLMService()
        self._validator = validator or ContractPartitionValidator()
        self._merger = merger or ClosureGapMerger()
        self._policy_engine = policy_engine or ClarificationPolicyEngine(max_questions=3)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def interpret(
        self, request: FrontChainInterpretationRequest
    ) -> FrontChainInterpretationResponse:
        request.require_binding_or_preview()

        interpretation = self._interpreter.interpret(
            request.raw_intent,
            semantic_substrate=request.semantic_substrate_hint,
        )

        partition = self._validator.validate_and_partition(interpretation.llm_output)
        merged_gaps = self._merger.merge(
            partition.problem_formulation,
            partition.input_closure,
            partition.output_closure,
        )
        top_questions = self._policy_engine.select_top_questions(merged_gaps)

        row = self._persist(
            project_id=request.project_id,
            session_id=request.session_id,
            requirement_id=request.requirement_id,
            preview_mode=request.preview,
            raw_intent=request.raw_intent,
            problem_formulation=partition.problem_formulation,
            input_closure=partition.input_closure,
            output_closure=partition.output_closure,
            merged_gaps=merged_gaps,
            top_questions=top_questions,
            trace_summary=interpretation.trace_summary,
        )

        return FrontChainInterpretationResponse(
            state_id=row.id,
            project_id=row.project_id,
            session_id=row.session_id,
            requirement_id=row.requirement_id,
            preview_mode=row.preview_mode,
            raw_intent=row.raw_intent,
            problem_formulation=partition.problem_formulation,
            input_closure=partition.input_closure,
            output_closure=partition.output_closure,
            merged_gaps=merged_gaps,
            top_questions=top_questions,
            llm_trace_summary=interpretation.trace_summary,
            interpreter_version=INTERPRETER_VERSION,
            validator_version=VALIDATOR_VERSION,
            created_at=row.created_at,
        )

    def get_by_id(self, state_id: str) -> Optional[FrontChainStateResponse]:
        row = self._db.query(FrontChainState).filter(FrontChainState.id == state_id).first()
        if row is None:
            return None
        return self._row_to_response(row)

    def list_for_session(self, session_id: str) -> list[FrontChainStateResponse]:
        rows = (
            self._db.query(FrontChainState)
            .filter(FrontChainState.session_id == session_id)
            .order_by(FrontChainState.created_at.desc())
            .all()
        )
        return [self._row_to_response(row) for row in rows]

    def list_for_requirement(self, requirement_id: str) -> list[FrontChainStateResponse]:
        rows = (
            self._db.query(FrontChainState)
            .filter(FrontChainState.requirement_id == requirement_id)
            .order_by(FrontChainState.created_at.desc())
            .all()
        )
        return [self._row_to_response(row) for row in rows]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _persist(
        self,
        *,
        project_id: str,
        session_id: Optional[str],
        requirement_id: Optional[str],
        preview_mode: bool,
        raw_intent: str,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        merged_gaps: MergedGapSet,
        top_questions: list[FrontChainClarificationQuestion],
        trace_summary: FrontChainLLMTraceSummary,
    ) -> FrontChainState:
        row = FrontChainState(
            project_id=project_id,
            session_id=session_id,
            requirement_id=requirement_id,
            preview_mode=preview_mode,
            raw_intent=raw_intent,
            problem_formulation_json=problem_formulation.model_dump(mode="json"),
            input_closure_json=input_closure.model_dump(mode="json"),
            output_closure_json=output_closure.model_dump(mode="json"),
            merged_gaps_json=merged_gaps.model_dump(mode="json"),
            top_questions_json=[q.model_dump(mode="json") for q in top_questions],
            llm_trace_json=trace_summary.model_dump(mode="json"),
            interpreter_version=INTERPRETER_VERSION,
            validator_version=VALIDATOR_VERSION,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        logger.info(
            "front_chain_state.persisted",
            extra={
                "state_id": row.id,
                "project_id": row.project_id,
                "session_id": row.session_id,
                "requirement_id": row.requirement_id,
                "preview_mode": row.preview_mode,
                "merger_version": MERGER_VERSION,
                "policy_version": POLICY_VERSION,
            },
        )
        return row

    @staticmethod
    def _row_to_response(row: FrontChainState) -> FrontChainStateResponse:
        return FrontChainStateResponse(
            id=row.id,
            project_id=row.project_id,
            session_id=row.session_id,
            requirement_id=row.requirement_id,
            preview_mode=row.preview_mode,
            raw_intent=row.raw_intent,
            problem_formulation=ProblemFormulationContract.model_validate(row.problem_formulation_json),
            input_closure=InputClosureContract.model_validate(row.input_closure_json),
            output_closure=OutputClosureContract.model_validate(row.output_closure_json),
            merged_gaps=MergedGapSet.model_validate(row.merged_gaps_json),
            top_questions=[
                FrontChainClarificationQuestion.model_validate(q) for q in (row.top_questions_json or [])
            ],
            llm_trace_summary=FrontChainLLMTraceSummary.model_validate(row.llm_trace_json),
            interpreter_version=row.interpreter_version,
            validator_version=row.validator_version,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
