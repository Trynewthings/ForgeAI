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

from app.core.config import settings

from app.db.models import FrontChainState
from app.schemas.front_chain import (
    ClarificationInterpretationResult,
    FrontChainAnswerItem,
    FrontChainAnswerRequest,
    FrontChainClarificationQuestion,
    FrontChainCompileResponse,
    FrontChainEvaluationRequest,
    FrontChainEvaluationResponse,
    FrontChainInterpretationRequest,
    FrontChainInterpretationResponse,
    FrontChainLayerName,
    FrontChainLLMTraceSummary,
    FrontChainStateResponse,
    MergedGapSet,
)
from app.schemas.requirement_compiler import CompileContextResponse
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
from app.services.front_chain.compile_context_assembler import (
    ASSEMBLER_VERSION,
    CompileContextAssembler,
)
from app.services.front_chain.closure_axis_registry import get_axis
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
    PartitionValidationResult,
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
COMPILE_VERSION = "front_chain_compile_v1"

logger = logging.getLogger(__name__)


class FrontChainStateNotFoundError(Exception):
    """Raised when an apply_answers call references a non-existent parent state."""

    def __init__(self, state_id: str) -> None:
        super().__init__(f"front_chain_state {state_id!r} not found")
        self.state_id = state_id


class FrontChainStateNotEvaluatedError(Exception):
    """Raised when compile is called on a state that lacks solvability/admissibility."""

    def __init__(self, state_id: str) -> None:
        super().__init__(
            f"front_chain_state {state_id!r} has not been evaluated yet "
            "(missing solvability_json or admissibility_json)"
        )
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
        solvability_orchestrator: Optional["SolvabilityOrchestrator"] = None,
        admissibility_service: Optional[ExecutionAdmissibilityService] = None,
        compile_assembler: Optional[CompileContextAssembler] = None,
    ) -> None:
        from app.services.front_chain.solvability_orchestrator import (
            SolvabilityOrchestrator,
        )

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
        # Phase 3 — the three-phase solvability orchestrator replaces
        # the old (SolvabilityLLMService + SolvabilityValidator) pair
        # as the default path. The legacy pair remains available in
        # the codebase for comparison/migration but is not wired into
        # evaluate().
        self._solvability_orchestrator = solvability_orchestrator or SolvabilityOrchestrator()
        self._admissibility_service = admissibility_service or ExecutionAdmissibilityService()
        # Phase 4
        self._compile_assembler = compile_assembler or CompileContextAssembler()

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
        top_questions = self._select_top_questions_with_clarity_gate(
            pf=partition.problem_formulation,
            merged_gaps=merged_gaps,
        )

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

    # Hard cap on PF clarity rounds. When this many free-form
    # clarification exchanges have happened without the interpreter
    # flipping ``intent_clarity`` to ``clear``, we stop nagging and
    # force-advance to the axis stage with ``ambiguous`` — downstream
    # admissibility will downgrade to ``admit_with_review`` so humans
    # still see the risk.
    PF_CLARITY_ROUND_CAP = 5

    # The question_id used by the policy engine's PF clarity question;
    # see ``ClarificationPolicyEngine.select_pf_clarity_question``.
    PF_CLARITY_QUESTION_ID = "q_problem_formulation_intent_clarity"

    def apply_answers(
        self,
        *,
        parent_state_id: str,
        request: FrontChainAnswerRequest,
    ) -> FrontChainInterpretationResponse:
        """Phase 1B: consume the user's answers and produce a new state turn.

        Two branches:
          1. If the user is answering the PF clarity free-form question,
             we treat their reply as an intent clarification. The text
             is concatenated onto the session's original raw_intent and
             we re-interpret — giving the interpreter full context to
             flip ``intent_clarity`` toward ``clear``. This keeps the
             conversational loop honest: PF is semantic, not axis-based.
          2. Otherwise we follow the classic axis clarification path
             (option selection + clarification interpreter).
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

        # Branch 1: PF clarity loop.
        clarity_answer = next(
            (
                a
                for a in request.answers
                if a.question_id == self.PF_CLARITY_QUESTION_ID and a.answer_text
            ),
            None,
        )
        if clarity_answer is not None and parent_problem.intent_clarity != "clear":
            return self._apply_clarity_answer(
                parent_row=parent_row,
                parent_problem=parent_problem,
                clarification_text=clarity_answer.answer_text or "",
            )

        # Phase 1C: split option selections from free-text answers.
        # Option selections are resolved deterministically via the closure
        # axis registry (no LLM call). Free-text answers go through the
        # clarification interpreter LLM as a fallback.
        option_resolutions: list[ClarificationResolutionRecord] = []
        free_text_answers: list[FrontChainAnswerItem] = []

        question_index = {q.question_id: q for q in parent_questions}
        for answer in request.answers:
            if answer.is_option_selection():
                resolution = self._resolve_option_deterministically(
                    answer=answer,
                    question_index=question_index,
                )
                if resolution:
                    option_resolutions.extend(resolution)
            elif answer.answer_text and answer.answer_text.strip():
                free_text_answers.append(answer)

        # Free-text answers go through the LLM interpreter (degraded to
        # standard-axis-only resolution per Rule 4).
        interpretation: ClarificationInterpretationResult
        if free_text_answers:
            interpretation = self._clarification_interpreter.interpret_answers(
                answers=free_text_answers,
                outstanding_questions=parent_questions,
            )
        else:
            from app.schemas.front_chain import FrontChainLLMTraceSummary
            interpretation = ClarificationInterpretationResult(
                interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
                resolutions=[],
                rejected_answers=[],
                reasoning_summary="All answers resolved via deterministic option selection; no LLM call needed.",
                llm_trace=FrontChainLLMTraceSummary(
                    call_count=0,
                    interpreter_version=CLARIFICATION_INTERPRETER_VERSION,
                    llm_mode=getattr(settings, "llm_mode", "mock"),
                    finish_reason="deterministic_option",
                ),
            )

        # Merge option resolutions + LLM resolutions, then apply.
        all_resolutions = option_resolutions + list(interpretation.resolutions)

        update_result = self._update_engine.apply_resolutions(
            problem_formulation=parent_problem,
            input_closure=parent_input,
            output_closure=parent_output,
            resolutions=all_resolutions,
        )

        reeval = self._reevaluation_service.reevaluate(
            problem_formulation=update_result.problem_formulation,
            input_closure=update_result.input_closure,
            output_closure=update_result.output_closure,
        )
        # Re-apply the PF clarity gate after axis resolution so the
        # free-form clarity question re-surfaces if clarity regressed.
        # (Normal path rarely regresses since axis answers don't touch
        # clarity, but this keeps the two paths symmetric.)
        gated_top_questions = self._select_top_questions_with_clarity_gate(
            pf=update_result.problem_formulation,
            merged_gaps=reeval.merged_gaps,
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
            top_questions=gated_top_questions,
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
            top_questions=gated_top_questions,
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

        # Hydrate the capability envelope from the registry so the
        # solvability LLM and validator both see the actual execution-layer
        # surface (capability ids, supported step kinds, runtime adapters,
        # policy hints). Historically this was passed as None, which caused
        # the LLM to invent capability_classes that nothing in the registry
        # could match — so every request tripped not_solvable at the
        # capability layer. We project a minimal, serialisable subset:
        # enough for the LLM to reason about what exists without leaking
        # the full registry schema.
        envelope_snapshot, known_capability_ids = self._build_solvability_envelope(
            problem_formulation=parent_problem,
            input_closure=parent_input,
            output_closure=parent_output,
        )

        # Steps 1+2: three-phase solvability orchestrator.
        # Phase A (enumerator) → Phase B (deterministic binder +
        # optional LLM rerank on narrowed pools only) → Phase C
        # (deterministic reducer). See ``SolvabilityOrchestrator``
        # for the full rule table; this seam is the only place the
        # evaluate flow touches Phase A/B/C.
        orchestration = self._solvability_orchestrator.orchestrate(
            problem_formulation=parent_problem,
            input_closure=parent_input,
            output_closure=parent_output,
            capability_envelope=envelope_snapshot,
            known_capability_ids=known_capability_ids,
        )
        solvability_contract = orchestration.contract
        solvability_trace = orchestration.trace_summary

        # Step 3: deterministic admissibility policy
        decision = self._admissibility_service.decide(
            problem_formulation=parent_problem,
            input_closure=parent_input,
            output_closure=parent_output,
            solvability=solvability_contract,
            policy_overrides=(request.policy_overrides if request else None),
        )

        # Step 4: persist a new chained turn row with closure contracts
        # preserved byte-equal. Phase 3 does NOT re-run interpret or
        # reevaluation; the gap/top_questions payloads from the parent
        # carry forward unchanged.
        new_row = self._persist_evaluation(
            parent_row=parent_row,
            solvability=solvability_contract,
            admissibility=decision.contract,
            solvability_trace=solvability_trace,
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
            solvability=solvability_contract,
            execution_admissibility=decision.contract,
            solvability_llm_trace=solvability_trace,
            evaluation_version=EVALUATION_VERSION,
            interpreter_version=INTERPRETER_VERSION,
            validator_version=VALIDATOR_VERSION,
            created_at=new_row.created_at,
        )

    def compile(
        self,
        *,
        parent_state_id: str,
    ) -> FrontChainCompileResponse:
        """Phase 4: assemble CompileContextResponse from evaluated state.

        Pipeline (0 LLM calls — pure deterministic packaging, R7):
            1. Load parent FrontChainState (must have solvability + admissibility)
            2. CompileContextAssembler.assemble(...)      [deterministic]
            3. Persist as a NEW chained turn row with:
                 * closure contracts byte-equal preserved
                 * compile_context_json populated
            4. Return FrontChainCompileResponse
        """
        parent_row = (
            self._db.query(FrontChainState).filter(FrontChainState.id == parent_state_id).first()
        )
        if parent_row is None:
            raise FrontChainStateNotFoundError(parent_state_id)

        if parent_row.solvability_json is None or parent_row.admissibility_json is None:
            raise FrontChainStateNotEvaluatedError(parent_state_id)

        parent_problem = ProblemFormulationContract.model_validate(parent_row.problem_formulation_json)
        parent_input = InputClosureContract.model_validate(parent_row.input_closure_json)
        parent_output = OutputClosureContract.model_validate(parent_row.output_closure_json)
        parent_merged = MergedGapSet.model_validate(parent_row.merged_gaps_json)
        parent_top_questions = [
            FrontChainClarificationQuestion.model_validate(q)
            for q in (parent_row.top_questions_json or [])
        ]
        parent_trace = FrontChainLLMTraceSummary.model_validate(parent_row.llm_trace_json)
        solvability = SolvabilityContract.model_validate(parent_row.solvability_json)
        admissibility = ExecutionAdmissibilityContract.model_validate(parent_row.admissibility_json)
        solvability_trace = FrontChainLLMTraceSummary.model_validate(parent_row.solvability_llm_trace_json)

        compile_context = self._compile_assembler.assemble(
            state_id=parent_row.id,
            project_id=parent_row.project_id,
            requirement_id=parent_row.requirement_id,
            problem_formulation=parent_problem,
            input_closure=parent_input,
            output_closure=parent_output,
            solvability=solvability,
            execution_admissibility=admissibility,
        )

        new_row = self._persist_compile(
            parent_row=parent_row,
            compile_context=compile_context,
            parent_trace=parent_trace,
            parent_merged=parent_merged,
            parent_top_questions=parent_top_questions,
            parent_problem=parent_problem,
            parent_input=parent_input,
            parent_output=parent_output,
            solvability=solvability,
            admissibility=admissibility,
            solvability_trace=solvability_trace,
        )

        return FrontChainCompileResponse(
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
            solvability=solvability,
            execution_admissibility=admissibility,
            solvability_llm_trace=solvability_trace,
            evaluation_version=parent_row.evaluation_version or EVALUATION_VERSION,
            compile_context=compile_context,
            compile_version=COMPILE_VERSION,
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

    def _select_top_questions_with_clarity_gate(
        self,
        *,
        pf: ProblemFormulationContract,
        merged_gaps: MergedGapSet,
    ) -> list[FrontChainClarificationQuestion]:
        """When PF intent_clarity != "clear", surface the free-form PF
        clarity question ALONE. Axis-based top_questions are deferred
        until the semantic gate opens."""
        if pf.intent_clarity != "clear":
            return [
                self._policy_engine.select_pf_clarity_question(
                    clarification_question=pf.clarification_question
                    or "你希望我具体做什么？",
                    interpreted_goal=pf.interpreted_goal,
                )
            ]
        return self._policy_engine.select_top_questions(merged_gaps)

    def _count_clarity_rounds(self, parent_row: FrontChainState) -> int:
        """Walk the parent chain counting states whose top_questions
        carried the PF clarity question. Used to enforce the
        ``PF_CLARITY_ROUND_CAP``."""
        count = 0
        current: Optional[FrontChainState] = parent_row
        visited: set[str] = set()
        while current is not None and current.id not in visited:
            visited.add(current.id)
            for q in current.top_questions_json or []:
                if isinstance(q, dict) and q.get("question_id") == self.PF_CLARITY_QUESTION_ID:
                    count += 1
                    break
            parent_id = current.parent_state_id
            if not parent_id:
                break
            current = (
                self._db.query(FrontChainState)
                .filter(FrontChainState.id == parent_id)
                .first()
            )
        return count

    def _apply_clarity_answer(
        self,
        *,
        parent_row: FrontChainState,
        parent_problem: ProblemFormulationContract,
        clarification_text: str,
    ) -> FrontChainInterpretationResponse:
        """Re-interpret the session's intent with the user's clarification
        appended. Enforces the 5-round cap; after that, force-advances
        with ``intent_clarity="ambiguous"`` so the axis loop can take
        over (downstream admissibility downgrades to ``admit_with_review``).
        """
        combined_intent = self._combine_intent(
            original=parent_row.raw_intent or "",
            follow_up=clarification_text.strip(),
        )
        interpretation = self._interpreter.interpret(combined_intent)
        partition = self._validator.validate_and_partition(interpretation.llm_output)

        # 5-round cap: if we've already gone through the cap and the
        # interpreter STILL didn't flip clarity to "clear", downgrade to
        # "ambiguous" and proceed to the axis loop.
        rounds_before_this_turn = self._count_clarity_rounds(parent_row)
        if (
            rounds_before_this_turn + 1 >= self.PF_CLARITY_ROUND_CAP
            and partition.problem_formulation.intent_clarity != "clear"
        ):
            partition = PartitionValidationResult(
                problem_formulation=partition.problem_formulation.model_copy(
                    update={
                        "intent_clarity": "ambiguous",
                        "clarification_question": None,
                    }
                ),
                input_closure=partition.input_closure,
                output_closure=partition.output_closure,
                issues=partition.issues,
            )

        merged_gaps = self._merger.merge(
            partition.problem_formulation,
            partition.input_closure,
            partition.output_closure,
        )
        top_questions = self._select_top_questions_with_clarity_gate(
            pf=partition.problem_formulation,
            merged_gaps=merged_gaps,
        )

        new_row = self._persist(
            project_id=parent_row.project_id,
            session_id=parent_row.session_id,
            requirement_id=parent_row.requirement_id,
            preview_mode=parent_row.preview_mode,
            raw_intent=combined_intent,
            parent_state_id=parent_row.id,
            turn_number=parent_row.turn_number + 1,
            problem_formulation=partition.problem_formulation,
            input_closure=partition.input_closure,
            output_closure=partition.output_closure,
            merged_gaps=merged_gaps,
            top_questions=top_questions,
            trace_summary=interpretation.trace_summary,
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
            problem_formulation=partition.problem_formulation,
            input_closure=partition.input_closure,
            output_closure=partition.output_closure,
            merged_gaps=merged_gaps,
            top_questions=top_questions,
            llm_trace_summary=interpretation.trace_summary,
            interpreter_version=INTERPRETER_VERSION,
            validator_version=VALIDATOR_VERSION,
            created_at=new_row.created_at,
            applied_resolutions=[],
            changed_contracts=[],
        )

    @staticmethod
    def _combine_intent(*, original: str, follow_up: str) -> str:
        """Merge the session's original intent with a free-form clarity
        reply into one ``raw_intent`` the interpreter can consume. The
        user's latest words go last so the LLM treats them as the
        current binding."""
        original = (original or "").strip()
        follow_up = (follow_up or "").strip()
        if not follow_up:
            return original
        if not original:
            return follow_up
        return f"{original}\n\n[User clarification]: {follow_up}"

    def _build_solvability_envelope(
        self,
        *,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
    ) -> tuple[list[dict], list[str]]:
        """Project the capability registry into the shape solvability expects.

        Returns a tuple of ``(envelope_list, known_capability_ids)``:
            * ``envelope_list`` — list of per-capability dicts with just
              the fields the solvability LLM and validator need. We keep
              the projection narrow so prompt size stays bounded and the
              validator doesn't have to worry about the full registry
              schema.
            * ``known_capability_ids`` — the set of capability_ids the
              validator should accept when filtering LLM-proposed matches.

        The registry is consulted at ``blueprint_planning`` context because
        front-chain solvability is pre-build: capabilities that are only
        allowed at ``runtime`` or ``authoring`` shouldn't be advertised as
        solvers. If the registry can't be loaded for any reason (import
        error in sandboxed tests, missing project context), we fall back
        to an empty envelope — existing behaviour before this change.
        """
        try:
            from app.services.capabilities.registry import CapabilityRegistryService
        except Exception:  # pragma: no cover - defensive import guard
            return [], []
        try:
            registry = CapabilityRegistryService()
            definitions = registry.list_capabilities(context="blueprint_planning")
        except Exception:  # pragma: no cover - defensive
            return [], []

        _ = (problem_formulation, input_closure, output_closure)  # reserved for future narrowing
        envelope: list[dict] = []
        ids: list[str] = []
        for cap in definitions:
            envelope.append(
                {
                    "capability_id": cap.capability_id,
                    "name": cap.name,
                    # Solvability mock matches on this key (see
                    # solvability_llm_service._mock_propose); also the
                    # shared vocabulary between LLM and validator.
                    "supported_step_kinds": list(cap.supported_step_kinds),
                    # Kept short — full descriptions would bloat the prompt.
                    "semantic_purpose": cap.semantic_purpose or "",
                    "capability_type": cap.capability_type,
                    "execution_kind": cap.execution_kind,
                    "runtime_adapter": cap.runtime_adapter,
                    "requires_confirmation": cap.requires_confirmation,
                }
            )
            ids.append(cap.capability_id)
        return envelope, ids

    def _resolve_option_deterministically(
        self,
        *,
        answer: FrontChainAnswerItem,
        question_index: dict[str, FrontChainClarificationQuestion],
    ) -> list[ClarificationResolutionRecord]:
        """Phase 1C: convert a selected option into deterministic resolutions.

        Reads the option's ``resolves`` mapping from the closure axis registry
        and produces one ``ClarificationResolutionRecord`` per axis in the map.
        No LLM call is made (Rule 3).
        """
        from datetime import datetime, timezone

        question = question_index.get(answer.question_id)
        if question is None:
            return []

        # Find the option definition.
        option_def = None
        for opt in question.suggested_options:
            if opt.option_id == answer.selected_option_id:
                option_def = opt
                break
        if option_def is None:
            # Fallback: look up in the global registry.
            axis_def = get_axis(question.axis)
            if axis_def is not None:
                option_def_raw = axis_def.get_option(answer.selected_option_id or "")
                if option_def_raw is not None:
                    from app.schemas.front_chain import FrontChainClarificationOption
                    option_def = FrontChainClarificationOption(
                        option_id=option_def_raw.option_id,
                        label=option_def_raw.label,
                        value=option_def_raw.value,
                        resolves=dict(option_def_raw.resolves),
                        is_default=option_def_raw.is_default,
                        requires_free_text_suffix=option_def_raw.requires_free_text_suffix,
                    )
        if option_def is None:
            return []

        recorded_at = datetime.now(timezone.utc).isoformat()
        resolutions: list[ClarificationResolutionRecord] = []
        resolves = option_def.resolves
        if not resolves:
            # Option with no resolves map but has a value → resolve the
            # question's own axis with the option value.
            resolves = {question.axis: option_def.value}

        for axis, value in resolves.items():
            # If the option requires a free-text suffix (e.g., "Other"),
            # append the user's answer_text to the value.
            final_value = value
            if option_def.requires_free_text_suffix and answer.answer_text:
                final_value = answer.answer_text.strip() if not value else f"{value}: {answer.answer_text.strip()}"
            resolutions.append(
                ClarificationResolutionRecord(
                    axis=axis,
                    resolution_type="user_confirmed_suggested_value",
                    resolved_value=final_value,
                    source_text=f"option:{option_def.option_id}",
                    accepted=True,
                    recorded_at=recorded_at,
                )
            )
        return resolutions

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

    def _persist_compile(
        self,
        *,
        parent_row: FrontChainState,
        compile_context: CompileContextResponse,
        parent_trace: FrontChainLLMTraceSummary,
        parent_merged: MergedGapSet,
        parent_top_questions: list[FrontChainClarificationQuestion],
        parent_problem: ProblemFormulationContract,
        parent_input: InputClosureContract,
        parent_output: OutputClosureContract,
        solvability: SolvabilityContract,
        admissibility: ExecutionAdmissibilityContract,
        solvability_trace: FrontChainLLMTraceSummary,
    ) -> FrontChainState:
        """Persist a new chained turn carrying Phase 4 compile context.

        All prior authority objects (closure contracts, solvability,
        admissibility) are preserved byte-equal. Only compile_context_json
        and compile_version are new.
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
            llm_trace_json=parent_trace.model_dump(mode="json"),
            solvability_json=solvability.model_dump(mode="json"),
            admissibility_json=admissibility.model_dump(mode="json"),
            solvability_llm_trace_json=solvability_trace.model_dump(mode="json"),
            evaluation_version=parent_row.evaluation_version or EVALUATION_VERSION,
            compile_context_json=compile_context.model_dump(mode="json"),
            compile_version=COMPILE_VERSION,
            interpreter_version=INTERPRETER_VERSION,
            validator_version=VALIDATOR_VERSION,
        )
        self._db.add(row)
        self._db.commit()
        self._db.refresh(row)
        logger.info(
            "front_chain_state.compile_persisted",
            extra={
                "state_id": row.id,
                "parent_state_id": row.parent_state_id,
                "turn_number": row.turn_number,
                # v2 path no longer carries selected_blueprint_type /
                # graph_closure_status (R5 + redundancy with solvability).
                "solvability_status": compile_context.solvability.solvability_status,
                "admissibility_regime": (
                    compile_context.execution_admissibility.primary_execution_regime
                    if compile_context.execution_admissibility is not None
                    else None
                ),
                "work_unit_count": len(compile_context.candidate_work_units),
                "capability_signature_count": len(
                    compile_context.capability_envelope.capability_signatures
                ),
                "assembler_version": ASSEMBLER_VERSION,
                "compile_version": COMPILE_VERSION,
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
        compile_context = (
            CompileContextResponse.model_validate(row.compile_context_json)
            if row.compile_context_json is not None
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
            compile_context=compile_context,
            compile_version=row.compile_version,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
