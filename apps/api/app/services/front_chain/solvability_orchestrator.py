"""Solvability Orchestrator — the single seam for A → B → C.

This is what ``front_chain_state_service.evaluate()`` calls. It owns:
    * Phase ordering (Phase A enumerator → Phase B deterministic bind
      → Phase B LLM rerank if needed → Phase C reducer).
    * Trigger criteria for Phase B's LLM rerank (narrow & conservative:
      only for subgoals the deterministic pass couldn't fully cover).
    * Degraded-path accounting: ``OrchestrationTrace`` captures
      ``phase_a_failed`` / ``failure_kind`` / ``phase_b_llm_failed``
      etc. separately from the contract's ``solvability_status``, so
      infra failures never masquerade as "needs clarification".
    * Trace merging across phases into a single trace summary for
      persistence.

LLM budget per ``orchestrate()`` call:
    * Phase A:  exactly 1 call
    * Phase B:  0 calls in the happy path
                 1 call per unresolved subgoal when rerank triggers

Worst case in current registry shape (~30 tools) is a handful of
reranks, well within the R9-unlocked per-turn ceiling.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.schemas.front_chain import FrontChainLLMTraceSummary
from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.schemas.solvability import (
    OrchestrationTrace,
    SolvabilityContract,
    SubgoalDraftEnvelope,
    ToolBinding,
    ToolBindingEnvelope,
)
from app.services.front_chain.solvability_subgoal_enumerator_service import (
    SUBGOAL_ENUMERATOR_VERSION,
    SolvabilitySubgoalEnumeratorService,
)
from app.services.front_chain.solvability_tool_binder_v2 import (
    TOOL_BINDER_V2_VERSION,
    SolvabilityCandidateNarrower,
    SolvabilityToolBinderLLMRerank,
    SolvabilityToolBinderV2,
)
from app.services.front_chain.solvability_verdict_reducer import (
    REDUCER_VERSION,
    SolvabilityVerdictReducer,
)

logger = logging.getLogger(__name__)

ORCHESTRATOR_VERSION = "front_chain_solvability_orchestrator_v1"


@dataclass
class OrchestrationResult:
    contract: SolvabilityContract
    trace_summary: FrontChainLLMTraceSummary
    orchestration_trace: OrchestrationTrace
    phase_a_trace: Optional[FrontChainLLMTraceSummary] = None
    phase_b_rerank_traces: list[FrontChainLLMTraceSummary] = field(default_factory=list)


class SolvabilityOrchestrator:
    """Phase A → B → C sequencer + trace keeper."""

    def __init__(
        self,
        *,
        enumerator: Optional[SolvabilitySubgoalEnumeratorService] = None,
        binder: Optional[SolvabilityToolBinderV2] = None,
        rerank: Optional[SolvabilityToolBinderLLMRerank] = None,
        reducer: Optional[SolvabilityVerdictReducer] = None,
        enable_llm_rerank: bool = True,
    ) -> None:
        self._enumerator = enumerator or SolvabilitySubgoalEnumeratorService()
        self._binder = binder or SolvabilityToolBinderV2()
        self._rerank = rerank or SolvabilityToolBinderLLMRerank()
        self._reducer = reducer or SolvabilityVerdictReducer()
        self._enable_llm_rerank = enable_llm_rerank

    def orchestrate(
        self,
        *,
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        capability_envelope: Optional[list[dict]] = None,
        known_capability_ids: Optional[Iterable[str]] = None,
    ) -> OrchestrationResult:
        envelope = list(capability_envelope or [])
        trace = OrchestrationTrace()

        # --- Phase A ------------------------------------------------------
        enum_result = self._enumerator.enumerate(
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
        )
        trace.stages_run.append("phase_a_enumerator")
        phase_a_trace = enum_result.trace_summary
        subgoal_envelope = enum_result.envelope
        if enum_result.failure_kind is not None:
            trace.phase_a_failed = True
            trace.phase_a_failure_kind = enum_result.failure_kind
            trace.degraded_reasons.append(
                f"phase_a_failed:{enum_result.failure_kind}"
            )

        # --- Phase B (deterministic) -------------------------------------
        binding_envelope = self._binder.bind_deterministic(
            subgoals=subgoal_envelope.subgoals,
            capability_envelope=envelope,
            known_capability_ids=known_capability_ids,
        )
        trace.stages_run.append("phase_b_deterministic")

        # --- Phase B (LLM rerank, only if needed) -----------------------
        rerank_traces: list[FrontChainLLMTraceSummary] = []
        if self._enable_llm_rerank and envelope:
            unresolved_ids = [
                b.subgoal_id
                for b in binding_envelope.bindings
                if b.coverage != "full" or b.unresolved_oov
            ]
            if unresolved_ids:
                binding_envelope, rerank_traces, rerank_failed = self._run_llm_rerank(
                    binding_envelope=binding_envelope,
                    subgoal_envelope=subgoal_envelope,
                    envelope=envelope,
                    unresolved_ids=unresolved_ids,
                )
                trace.phase_b_llm_invoked = True
                trace.phase_b_llm_failed = rerank_failed
                trace.phase_b_unresolved_subgoal_ids = unresolved_ids
                trace.stages_run.append("phase_b_llm_rerank")
                if rerank_failed:
                    trace.degraded_reasons.append("phase_b_llm_failed")

        # Record any still-unresolved subgoals after rerank. These feed
        # the reducer's "capability vs judgment gap" classification.
        still_unresolved = [
            b.subgoal_id
            for b in binding_envelope.bindings
            if b.coverage != "full" or b.unresolved_oov
        ]
        if still_unresolved:
            trace.degraded_reasons.append("phase_b_unresolved")

        # --- Phase C (reducer) -------------------------------------------
        reducer_result = self._reducer.reduce(
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
            subgoal_envelope=subgoal_envelope,
            binding_envelope=binding_envelope,
        )
        trace.stages_run.append("phase_c_reducer")

        contract = reducer_result.contract
        merged_trace = _merge_traces(phase_a_trace, rerank_traces)

        return OrchestrationResult(
            contract=contract,
            trace_summary=merged_trace,
            orchestration_trace=trace,
            phase_a_trace=phase_a_trace,
            phase_b_rerank_traces=rerank_traces,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _run_llm_rerank(
        self,
        *,
        binding_envelope: ToolBindingEnvelope,
        subgoal_envelope: SubgoalDraftEnvelope,
        envelope: list[dict],
        unresolved_ids: list[str],
    ) -> tuple[ToolBindingEnvelope, list[FrontChainLLMTraceSummary], bool]:
        """Run rerank on the subset of subgoals that weren't fully
        covered. Each call sees only that subgoal's candidate pool.

        Returns (updated_envelope, rerank_traces, any_failure).
        """
        subgoals_by_id = {sg.subgoal_id: sg for sg in subgoal_envelope.subgoals}
        narrower = SolvabilityCandidateNarrower()
        rerank_traces: list[FrontChainLLMTraceSummary] = []
        any_failure = False
        updated_bindings: list[ToolBinding] = []
        for binding in binding_envelope.bindings:
            if binding.subgoal_id not in unresolved_ids:
                updated_bindings.append(binding)
                continue
            subgoal = subgoals_by_id.get(binding.subgoal_id)
            if subgoal is None:
                updated_bindings.append(binding)
                continue
            pool = narrower.narrow(
                subgoal=subgoal, capability_envelope=envelope
            )
            updated, trace, failure_reason = self._rerank.rerank(
                binding=binding,
                subgoal=subgoal,
                candidate_pool=pool.candidate_tool_ids,
                capability_envelope=envelope,
            )
            rerank_traces.append(trace)
            if failure_reason is not None:
                any_failure = True
            updated_bindings.append(updated)
        new_stages = list(binding_envelope.stages) + [
            "llm_failed" if any_failure else "llm_rerank"
        ]
        return (
            binding_envelope.model_copy(
                update={
                    "bindings": updated_bindings,
                    "stages": new_stages,
                }
            ),
            rerank_traces,
            any_failure,
        )


def _merge_traces(
    phase_a_trace: FrontChainLLMTraceSummary,
    rerank_traces: list[FrontChainLLMTraceSummary],
) -> FrontChainLLMTraceSummary:
    """Merge Phase A + all Phase B rerank traces into a single summary
    for persistence. ``call_count`` sums across phases; prompt/completion
    tokens sum too. Finish reason is the worst of the lot (error trumps
    stop)."""
    call_count = phase_a_trace.call_count or 0
    prompt_tokens = phase_a_trace.prompt_tokens or 0
    completion_tokens = phase_a_trace.completion_tokens or 0
    total_tokens = phase_a_trace.total_tokens or 0
    duration_ms = phase_a_trace.duration_ms or 0
    finish_reason = phase_a_trace.finish_reason or "stop"
    for rt in rerank_traces:
        call_count += rt.call_count or 0
        prompt_tokens += rt.prompt_tokens or 0
        completion_tokens += rt.completion_tokens or 0
        total_tokens += rt.total_tokens or 0
        if rt.duration_ms:
            duration_ms = max(duration_ms, rt.duration_ms)
        if rt.finish_reason == "error":
            finish_reason = "error"
    return FrontChainLLMTraceSummary(
        call_count=call_count,
        interpreter_version=ORCHESTRATOR_VERSION,
        llm_mode=phase_a_trace.llm_mode,
        provider_key=phase_a_trace.provider_key,
        prompt_hash=phase_a_trace.prompt_hash,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        finish_reason=finish_reason,
        duration_ms=duration_ms,
    )


__all__ = [
    "ORCHESTRATOR_VERSION",
    "OrchestrationResult",
    "SolvabilityOrchestrator",
]
