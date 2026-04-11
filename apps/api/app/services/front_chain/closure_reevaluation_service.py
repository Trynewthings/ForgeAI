"""Closure reevaluation service (Phase 1B).

After the deterministic ``ContractUpdateEngine`` patches the contracts, this
service re-runs only the deterministic ``ClosureGapMerger`` and
``ClarificationPolicyEngine`` steps to produce the new merged gap set and
top questions for the next turn.

It does NOT call the front-chain interpreter LLM. It does NOT touch
contracts. It only consumes the patched contracts and produces gap-set +
question outputs.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.schemas.front_chain import (
    FrontChainClarificationQuestion,
    MergedGapSet,
)
from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.services.front_chain.clarification_policy_engine import ClarificationPolicyEngine
from app.services.front_chain.closure_gap_merger import ClosureGapMerger

REEVALUATION_VERSION = "front_chain_closure_reevaluation_v1"


@dataclass
class ReevaluationResult:
    merged_gaps: MergedGapSet
    top_questions: list[FrontChainClarificationQuestion]


class ClosureReevaluationService:
    """Selective deterministic reevaluation (gaps + policy only)."""

    def __init__(
        self,
        *,
        merger: ClosureGapMerger | None = None,
        policy_engine: ClarificationPolicyEngine | None = None,
    ) -> None:
        self._merger = merger or ClosureGapMerger()
        self._policy_engine = policy_engine or ClarificationPolicyEngine(max_questions=3)

    def reevaluate(
        self,
        *,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
    ) -> ReevaluationResult:
        merged_gaps = self._merger.merge(
            problem_formulation,
            input_closure,
            output_closure,
        )
        top_questions = self._policy_engine.select_top_questions(merged_gaps)
        return ReevaluationResult(
            merged_gaps=merged_gaps,
            top_questions=top_questions,
        )
