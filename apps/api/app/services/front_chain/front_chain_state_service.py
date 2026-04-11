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
    ClarificationInterpretationResult,
    FrontChainAnswerItem,
    FrontChainAnswerRequest,
    FrontChainClarificationQuestion,
    FrontChainEvaluationRequest,
    FrontChainEvaluationResponse,
    FrontChainInterpretationRequest,
    FrontChainInterpretationResponse,
    FrontChainLayerName,
    FrontChainLLMTraceSummary,
    FrontChainStateResponse,
    MergedGapSet,
)
from app.schemas.problem_formulation import (
    ClarificationResolutionRecord,
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.schemas.solvability import (
    SolvabilityContract,
)
from app.schemas.workflow_native import (
    ExecutionAdmissibilityContract,
)
from app.services.front_chain.clarification_interpreter_llm_service import (
    CLARIFICATION_INTERPRETER_VERSION,
    ClarificationInterpreterLLMService,
)
from app.services.front_chain.clarification_policy_engine import (
    POLICY_VERSION,
    ClarificationPolicyEngine,
)
from app.services.front_chain.closure_gap_merger import (
    MERGER_VERSION,
    ClosureGapMerger,
)
from app.services.front_chain.closure_reevaluation_service import (
    REEVALUATION_VERSION,
    ClosureReevaluationService,
)
from app.services.front_chain.contract_partition_validator import (
    VALIDATOR_VERSION,
    ContractPartitionValidator,
)
from app.services.front_chain.contract_update_engine import (
    UPDATE_ENGINE_VERSION,
    ContractUpdateEngine,
)
from app.services.front_chain.execution_admissibility_service import (
    ADMISSIBILITY_VERSION,
    ExecutionAdmissibilityService,
)
from app.services.front_chain.front_chain_interpreter_llm_service import (
    INTERPRETER_VERSION,
    FrontChainInterpreterLLMService,
)
from app.services.front_chain.solvability_llm_service import (
    SOLVABILITY_LLM_VERSION,
    SolvabilityLLMService,
)
from app.services.front_chain.solvability_validator import (
    VALIDATOR_VERSION as SOLVABILITY_VALIDATOR_VERSION,
    SolvabilityValidator,
)

EVALUATION_VERSION = "front_chain_evaluation_v1"

logger = logging.getLogger(__name__)


class FrontChainStateNotFoundError(Exception):
    """Raised when an apply_answers call references a non-existent parent state."""

    def __init__(self, state_id: str) -> None:
        super().__init__(f"front_chain_state {state_id!r} not found")
        self.state_id = state_id


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
        clarification_interpreter: Optional[ClarificationInterpreterLLMService] = None,
        update_engine: Optional[ContractUpdateEngine] = None,
        reevaluation_service: Optional[ClosureReevaluationService] = None,
        solvability_service: Optional[SolvabilityLLMService] = None,
        solvability_validator: Optional[SolvabilityValidator] = None,
        admissibility_service: Optional[ExecutionAdmissibilityService] = None,
    ) -> None:
        self._db = db
        self._interpreter = interpreter or FrontChainInterpreterLLMService()
        self._validator = validator or ContractPartitionValidator()
        self._merger = merger or ClosureGapMerger()
        self._policy_engine = policy_engine or ClarificationPolicyEngine(max_questions=3)
        self._clarification_interpreter = (
            clarification_interpreter or ClarificationInterpreterLLMService()
        )
        self._update_engine = update_engine or ContractUpdateEngine()
        self._reevaluation_service = reevaluation_service or ClosureReevaluationService(
            merger=self._merger,
            policy_engine=self._policy_engine,
        )
        # Phase 3
        self._solvability_service = solvability_service or SolvabilityLLMService()
        self._solvability_validator = solvability_validator or SolvabilityValidator()
        self._admissibility_service = admissibility_service or ExecutionAdmissibilityService()

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
            parent_state_id=None,
            turn_number=1,
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
            parent_state_id=row.parent_state_id,
            turn_number=row.turn_number,
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
            applied_resolutions=[],
            changed_contracts=[],
        )

    def apply_answers(
        self,
        *,
        parent_state_id: str,
        request: FrontChainAnswerRequest,
    ) -> FrontChainInterpretationResponse:
        """Phase 1B: consume the user's answers and produce a new state turn.

        Pipeline (1 LLM call total):
            1. Load parent FrontChainState
            2. clarification_interpreter.interpret_answers(answers, parent.top_questions)
            3. contract_update_engine.apply_resolutions(parent.contracts, resolutions)
            4. closure_reevaluation_service.reevaluate(patched contracts)
            5. Persist as a new state row with parent_state_id chain
            6. Return new state's full interpretation response
        """
        parent_row = (
            self._db.query(FrontChainState).filter(FrontChainState.id == parent_state_id).first()
        )
        if parent_row is None:
            raise FrontChainStateNotFoundError(parent_state_id)

        parent_problem = ProblemFormulationContract.model_validate(parent_row.problem_formulation_json)
        parent_input = InputClosureContract.model_validate(parent_row.input_closure_json)
        parent_output = OutputClosureContract.model_validate(parent_row.output_closure_json)
        parent_questions = [
            FrontChainClarificationQuestion.model_validate(q)
            for q in (parent_row.top_questions_json or [])
        ]

        interpretation: ClarificationInterpretationResult = (
            self._clarification_interpreter.interpret_answers(
                answers=list(request.answers),
                outstanding_questions=parent_questions,
            )
        )

        update_result = self._update_engine.apply_resolutions(
            problem_formulation=parent_problem,
            input_closure=parent_input,
            output_closure=parent_output,
            resolutions=list(interpretation.resolutions),
        )

        reeval = self._reevaluation_service.reevaluate(
            problem_formulation=update_result.problem_formulation,
            input_closure=update_result.input_closure,
            output_closure=update_result.output_closure,
        )

        # When the update was a no-op (no resolution actually mutated a
        # contract), we still persist a new row so the audit chain stays
        # complete. The trace summary preserves the LLM call count from the
        # interpreter (1 in real / mock paths, 0 in the no-op path).
        new_row = self._persist(
            project_id=parent_row.project_id,
            session_id=parent_row.session_id,
            requirement_id=parent_row.requirement_id,
            preview_mode=parent_row.preview_mode,
            raw_intent=parent_row.raw_intent,
            parent_state_id=parent_row.id,
            turn_number=parent_row.turn_number + 1,
            problem_formulation=update_result.problem_formulation,
            input_closure=update_result.input_closure,
            output_closure=update_result.output_closure,
            merged_gaps=reeval.merged_gaps,
            top_questions=reeval.top_questions,
            trace_summary=interpretation.llm_trace,
        )

        return FrontChainInterpretationResponse(
            state_id=new_row.id,
            project_id=new_row.project_id,
            session_id=new_row.session_id,
            requirement_id=new_row.requirement_id,
            preview_mode=new_row.preview_mode,
            parent_state_id=new_row.parent_state_id,
            turn_number=new_row.turn_number,
            raw_intent=new_row.raw_intent,
            problem_formulation=update_result.problem_formulation,
            input_closure=update_result.input_closure,
            output_closure=update_result.output_closure,
            merged_gaps=reeval.merged_gaps,
            top_questions=reeval.top_questions,
            llm_trace_summary=interpretation.llm_trace,
            interpreter_version=INTERPRETER_VERSION,
            validator_version=VALIDATOR_VERSION,
            created_at=new_row.created_at,
            applied_resolutions=list(update_result.applied_resolutions),
            changed_contracts=list(update_result.changed_layers),
        )

    def evaluate(
        self,
        *,
        parent_state_id: str,
        request: Optional[FrontChainEvaluationRequest] = None,
    ) -> FrontChainEvaluationResponse:
        """Phase 3: derive solvability + admissibility from the closure contracts.

        Pipeline (1 LLM call total):
            1. Load parent FrontChainState
            2. SolvabilityLLMService.propose(...)                 [LLM × 1]
            3. SolvabilityValidator.validate_and_produce(...)     [deterministic]
            4. ExecutionAdmissibilityService.decide(...)          [deterministic, 0 LLM]
            5. Persist as a NEW chained turn row with:
                 * closure contracts byte-equal preserved
                 * solvability_json + admissibility_json populated
            6. Return FrontChainEvaluationResponse
        """
        parent_row = (
            self._db.query(FrontChainState).filter(FrontChainState.id == parent_state_id).first()
        )
        if parent_row is None:
            raise FrontChainStateNotFoundError(parent_state_id)

        parent_problem = ProblemFormulationContract.model_validate(parent_row.problem_formulation_json)
        parent_input = InputClosureContract.model_validate(parent_row.input_closure_json)
        parent_output = OutputClosureContract.model_validate(parent_row.output_closure_json)
        parent_merged = MergedGapSet.model_validate(parent_row.merged_gaps_json)
        parent_top_questions = [
            FrontChainClarificationQuestion.model_validate(q)
            for q in (parent_row.top_questions_json or [])
        ]
        parent_trace = FrontChainLLMTraceSummary.model_validate(parent_row.llm_trace_json)

        # Step 1: solvability LLM (single call)
        proposal = self._solvability_service.propose(
            problem_formulation=parent_problem,
            input_closure=parent_input,
            output_closure=parent_output,
            capability_envelope=None,
        )

        # Step 2: deterministic validator
        validation = self._solvability_validator.validate_and_produce(
            candidate=proposal.candidate,
            problem_formulation=parent_problem,
            input_closure=parent_input,
            output_closure=parent_output,
            known_capability_ids=None,
            raw_candidate_payload=None,
        )

        # Step 3: deterministic admissibility policy
        decision = self._admissibility_service.decide(
            problem_formulation=parent_problem,
            input_closure=parent_input,
            output_closure=parent_output,
            solvability=validation.contract,
            policy_overrides=(request.policy_overrides if request else None),
        )

        # Step 4: persist a new chained turn row with closure contracts
        # preserved byte-equal. Phase 3 does NOT re-run interpret or
        # reevaluation; the gap/top_questions payloads from the parent
        # carry forward unchanged.
        new_row = self._persist_evaluation(
            parent_row=parent_row,
            solvability=validation.contract,
            admissibility=decision.contract,
            solvability_trace=proposal.trace_summary,
            parent_trace=parent_trace,
            parent_merged=parent_merged,
            parent_top_questions=parent_top_questions,
            parent_problem=parent_problem,
            parent_input=parent_input,
            parent_output=parent_output,
        )

        return FrontChainEvaluationResponse(
            state_id=new_row.id,
            project_id=new_row.project_id,
            session_id=new_row.session_id,
            requirement_id=new_row.requirement_id,
            preview_mode=new_row.preview_mode,
            parent_state_id=new_row.parent_state_id,
            turn_number=new_row.turn_number,
            raw_intent=new_row.raw_intent,
            problem_formulation=parent_problem,
            input_closure=parent_input,
            output_closure=parent_output,
            merged_gaps=parent_merged,
            top_questions=parent_top_questions,
            llm_trace_summary=parent_trace,
            solvability=validation.contract,
            execution_admissibility=decision.contract,
            solvability_llm_trace=proposal.trace_summary,
            evaluation_version=EVALUATION_VERSION,
            interpreter_version=INTERPRETER_VERSION,
            validator_version=VALIDATOR_VERSION,
            created_at=new_row.created_at,
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

    def _persist_evaluation(
        self,
        *,
        parent_row: FrontChainState,
        solvability: SolvabilityContract,
        admissibility: ExecutionAdmissibilityContract,
        solvability_trace: FrontChainLLMTraceSummary,
        parent_trace: FrontChainLLMTraceSummary,
        parent_merged: MergedGapSet,
        parent_top_questions: list[FrontChainClarificationQuestion],
        parent_problem: ProblemFormulationContract,
        parent_input: InputClosureContract,
        parent_output: OutputClosureContract,
    ) -> FrontChainState:
        """Persist a new chained turn carrying Phase 3 evaluation fields.

        The closure contracts on the new row are byte-equal to the parent's
        closure contracts (Phase 3 never mutates them). Only the Phase 3
        columns (solvability_json, admissibility_json, solvability_llm_trace_json,
        evaluation_version) are populated anew.
        """
        row = FrontChainState(
            project_id=parent_row.project_id,
            session_id=parent_row.session_id,
            requirement_id=parent_row.requirement_id,
            preview_mode=parent_row.preview_mode,
            parent_state_id=parent_row.id,
            turn_number=parent_row.turn_number + 1,
            raw_intent=parent_row.raw_intent,
            problem_formulation_json=parent_problem.model_dump(mode="json"),
            input_closure_json=parent_input.model_dump(mode="json"),
            output_closure_json=parent_output.model_dump(mode="json"),
            merged_gaps_json=parent_merged.model_dump(mode="json"),
            top_questions_json=[q.model_dump(mode="json") for q in parent_top_questions],
            # Preserve the original interpret trace on the new row so callers
            # can trace how we got here; Phase 3 also stores its own trace
            # separately in solvability_llm_trace_json.
            llm_trace_json=parent_trace.model_dump(mode="json"),
            solvability_json=solvability.model_dump(mode="json"),
            admissibility_json=admissibility.model_dump(mode="json"),
            solvability_llm_trace_json=solvability_trace.model_dump(mode="json"),
            evaluation_version=EVALUATION_VERSION,
            interpreter_version=INTERPRETER_VERSION,
            validator_version=VALIDATOR_VERSION,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        logger.info(
            "front_chain_state.evaluation_persisted",
            extra={
                "state_id": row.id,
                "parent_state_id": row.parent_state_id,
                "turn_number": row.turn_number,
                "solvability_status": solvability.solvability_status,
                "admissibility_status": admissibility.admissibility_status,
                "primary_regime": admissibility.primary_execution_regime,
                "solvability_llm_version": SOLVABILITY_LLM_VERSION,
                "solvability_validator_version": SOLVABILITY_VALIDATOR_VERSION,
                "admissibility_version": ADMISSIBILITY_VERSION,
                "evaluation_version": EVALUATION_VERSION,
            },
        )
        return row

    def _persist(
        self,
        *,
        project_id: str,
        session_id: Optional[str],
        requirement_id: Optional[str],
        preview_mode: bool,
        raw_intent: str,
        parent_state_id: Optional[str],
        turn_number: int,
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
            parent_state_id=parent_state_id,
            turn_number=turn_number,
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
                "parent_state_id": row.parent_state_id,
                "turn_number": row.turn_number,
                "merger_version": MERGER_VERSION,
                "policy_version": POLICY_VERSION,
                "update_engine_version": UPDATE_ENGINE_VERSION,
                "reevaluation_version": REEVALUATION_VERSION,
                "clarification_interpreter_version": CLARIFICATION_INTERPRETER_VERSION,
            },
        )
        return row

    @staticmethod
    def _row_to_response(row: FrontChainState) -> FrontChainStateResponse:
        solvability = (
            SolvabilityContract.model_validate(row.solvability_json)
            if row.solvability_json is not None
            else None
        )
        admissibility = (
            ExecutionAdmissibilityContract.model_validate(row.admissibility_json)
            if row.admissibility_json is not None
            else None
        )
        solvability_trace = (
            FrontChainLLMTraceSummary.model_validate(row.solvability_llm_trace_json)
            if row.solvability_llm_trace_json is not None
            else None
        )
        return FrontChainStateResponse(
            id=row.id,
            project_id=row.project_id,
            session_id=row.session_id,
            requirement_id=row.requirement_id,
            preview_mode=row.preview_mode,
            parent_state_id=row.parent_state_id,
            turn_number=row.turn_number,
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
            solvability=solvability,
            execution_admissibility=admissibility,
            solvability_llm_trace=solvability_trace,
            evaluation_version=row.evaluation_version,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
