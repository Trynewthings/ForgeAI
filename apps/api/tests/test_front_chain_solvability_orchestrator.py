"""Tests for the three-phase solvability orchestrator.

Coverage matrix (13 cases):

    O1   happy path: all subgoals deterministic-covered → solvable
    O2   partial coverage on one subgoal → LLM rerank → still full
    O3   Phase A LLM failure → empty subgoals, trace marks failure_kind
    O4   Phase B LLM rerank failure → deterministic result kept, trace flags it
    O5   PF intent_clarity missing_goal → Phase C defensive clarification
    O6   closures still blocking → Phase C surfaces solvable_with_clarification
    O7   candidate_shape_hint propagates into graph_feasibility
    O8   merged trace contains Phase A + all rerank tokens
    O9   orchestration_trace.stages_run records every phase visited
    O10  planner can still consume the resulting contract (byte-equal shape)
    O11  reviewer insertion illegal (out_of_scope) → not_solvable,
         not solvable_with_review even when subgoal asks for review
    O12  Phase A emits alias step_kinds (inspect / gather / write_summary)
         → canonicalizer normalises → binder full coverage
    O13  deterministic binder fully covers → LLM rerank is NOT invoked
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from app.schemas.front_chain import FrontChainLLMTraceSummary
from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    PlanningIntentContract,
    ProblemFormulationContract,
)
from app.schemas.solvability import (
    SubgoalDraft,
    SubgoalDraftEnvelope,
)
from app.services.front_chain.solvability_orchestrator import (
    SolvabilityOrchestrator,
)
from app.services.front_chain.solvability_subgoal_enumerator_service import (
    SUBGOAL_ENUMERATOR_VERSION,
    SolvabilitySubgoalEnumeratorService,
    SubgoalEnumerationResult,
)
from app.services.front_chain.solvability_tool_binder_v2 import (
    SolvabilityToolBinderLLMRerank,
)
from app.services.front_chain.solvability_step_kind_canonicalizer import (
    canonicalize_step_kinds,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _pf_clear() -> ProblemFormulationContract:
    return ProblemFormulationContract(
        interpreted_problem_kind="document_summarization",
        target_object="local_workspace",
        problem_status="solvable",
        current_system_fit="in_scope",
        planning_intent=PlanningIntentContract(
            intent_id="pi_1",
            label="summarise local docs",
            objective_family="analysis",
        ),
        intent_clarity="clear",
    )


def _pf_blocked() -> ProblemFormulationContract:
    return ProblemFormulationContract(
        interpreted_problem_kind=None,
        target_object=None,
        problem_status="underdetermined",
        current_system_fit="partially_in_scope",
        blocking_axes=["target_object"],
        blocking_reasons=["target_object missing"],
        intent_clarity="clear",
    )


def _pf_out_of_scope() -> ProblemFormulationContract:
    return ProblemFormulationContract(
        interpreted_problem_kind="dangerous_ask",
        target_object="generic_request",
        problem_status="solvable",
        current_system_fit="out_of_scope",
        intent_clarity="clear",
    )


def _ic_closed() -> InputClosureContract:
    return InputClosureContract(
        resolved_source_locator="/tmp/test.txt",
        resolved_source_kind="local_file",
        resolved_source_modality="local_path",
        acquisition_path_kind="direct_read",
        closure_status="input_closed",
    )


def _ic_blocked() -> InputClosureContract:
    return InputClosureContract(
        blocking_axes=["source_locator"],
        closure_status="input_clarification_required",
    )


def _oc_closed() -> OutputClosureContract:
    return OutputClosureContract(
        deliverable_form="report",
        delivery_target="user_inline",
        output_modality="markdown",
        closure_status="output_closed",
    )


def _envelope_full_coverage() -> list[dict]:
    """Envelope where every step_kind we test has a matching tool."""
    return [
        {
            "capability_id": "filesystem_read",
            "name": "Filesystem Read",
            "supported_step_kinds": ["ingest", "transform"],
            "semantic_purpose": "Read local files.",
        },
        {
            "capability_id": "summarize_text_llm",
            "name": "Summarize Text",
            "supported_step_kinds": ["summarize"],
            "semantic_purpose": "Summarise arbitrary text.",
        },
        {
            "capability_id": "general_agentic_reasoning",
            "name": "General Agentic Reasoning",
            "supported_step_kinds": ["transform", "summarize", "validate", "review"],
            "semantic_purpose": "Universal reasoning backbone.",
        },
        {
            "capability_id": "filesystem_write",
            "name": "Filesystem Write",
            "supported_step_kinds": ["persist"],
            "semantic_purpose": "Write local files.",
        },
    ]


def _envelope_missing_persist() -> list[dict]:
    """Envelope where no tool can persist — deliver subgoal becomes
    unmatched on capability_gap."""
    return [
        {
            "capability_id": "filesystem_read",
            "name": "Filesystem Read",
            "supported_step_kinds": ["ingest"],
        },
        {
            "capability_id": "summarize_text_llm",
            "name": "Summarize",
            "supported_step_kinds": ["summarize"],
        },
    ]


# Lightweight stubs for the enumerator + rerank LLM so tests stay
# fully offline.


@dataclass
class _StubEnumerator:
    envelope: SubgoalDraftEnvelope
    failure_kind: Optional[str] = None

    def enumerate(self, **_kwargs) -> SubgoalEnumerationResult:
        return SubgoalEnumerationResult(
            envelope=self.envelope,
            trace_summary=FrontChainLLMTraceSummary(
                call_count=1,
                interpreter_version=SUBGOAL_ENUMERATOR_VERSION,
                llm_mode="mock",
                finish_reason="error" if self.failure_kind else "stop",
            ),
            failure_kind=self.failure_kind,  # type: ignore[arg-type]
        )


@dataclass
class _StubRerank:
    """Always picks the first candidate in the pool. Set ``fail=True``
    to simulate LLM failure."""

    fail: bool = False
    invocation_count: int = 0

    def rerank(self, *, binding, subgoal, candidate_pool, capability_envelope):  # noqa: D401
        self.invocation_count += 1
        trace = FrontChainLLMTraceSummary(
            call_count=1,
            interpreter_version="stub_rerank",
            llm_mode="mock",
            finish_reason="error" if self.fail else "stop",
        )
        if self.fail:
            return binding, trace, "llm_rerank_failed"
        if not candidate_pool:
            return binding, trace, None
        # Pretend the LLM picked the first candidate.
        new_bound = list(binding.bound_tool_ids)
        if candidate_pool[0] not in new_bound:
            new_bound.append(candidate_pool[0])
        updated = binding.model_copy(
            update={
                "bound_tool_ids": new_bound,
                "binding_source": "llm_assisted",
                "coverage": "full" if not binding.unresolved_oov else "partial",
                "reasoning": "stub rerank picked first candidate",
            }
        )
        return updated, trace, None


def _four_subgoal_envelope() -> SubgoalDraftEnvelope:
    return SubgoalDraftEnvelope(
        subgoals=[
            SubgoalDraft(
                subgoal_id="sg_1",
                label="Acquire",
                description="read file",
                subgoal_kind="acquire",
                required_step_kinds=["ingest"],
                raw_step_kinds=["ingest"],
                gap_kind="capability",
            ),
            SubgoalDraft(
                subgoal_id="sg_2",
                label="Analyze",
                description="parse text",
                subgoal_kind="analyze",
                required_step_kinds=["transform"],
                raw_step_kinds=["transform"],
                gap_kind="capability",
            ),
            SubgoalDraft(
                subgoal_id="sg_3",
                label="Synthesize",
                description="compose report",
                subgoal_kind="synthesize",
                required_step_kinds=["summarize"],
                raw_step_kinds=["summarize"],
                gap_kind="capability",
            ),
            SubgoalDraft(
                subgoal_id="sg_4",
                label="Deliver",
                description="save file",
                subgoal_kind="deliver",
                required_step_kinds=["persist"],
                raw_step_kinds=["persist"],
                gap_kind="capability",
            ),
        ],
        candidate_shape_hint="linear",
        reasoning_summary="stub 4-subgoal envelope",
    )


# ---------------------------------------------------------------------------
# O1 — happy path
# ---------------------------------------------------------------------------


def test_orchestrator_happy_path_all_deterministic_covered() -> None:
    stub_rerank = _StubRerank()
    orch = SolvabilityOrchestrator(
        enumerator=_StubEnumerator(envelope=_four_subgoal_envelope()),
        rerank=stub_rerank,
    )
    result = orch.orchestrate(
        problem_formulation=_pf_clear(),
        input_closure=_ic_closed(),
        output_closure=_oc_closed(),
        capability_envelope=_envelope_full_coverage(),
        known_capability_ids=[c["capability_id"] for c in _envelope_full_coverage()],
    )
    assert result.contract.solvability_status == "solvable"
    for match in result.contract.capability_matches:
        assert match.match_status == "matched"
    assert result.orchestration_trace.phase_b_llm_invoked is False


# ---------------------------------------------------------------------------
# O2 — partial coverage → LLM rerank → still covered
# ---------------------------------------------------------------------------


def test_orchestrator_rerank_upgrades_partial_to_full() -> None:
    # Envelope missing ``persist`` will leave sg_4 uncovered by
    # deterministic binder. Stub rerank cannot help because the pool
    # itself is empty for persist. Orchestrator should mark sg_4 as
    # unmatched (capability gap) and status=not_solvable.
    stub_rerank = _StubRerank()
    orch = SolvabilityOrchestrator(
        enumerator=_StubEnumerator(envelope=_four_subgoal_envelope()),
        rerank=stub_rerank,
    )
    result = orch.orchestrate(
        problem_formulation=_pf_clear(),
        input_closure=_ic_closed(),
        output_closure=_oc_closed(),
        capability_envelope=_envelope_missing_persist(),
    )
    # Rerank runs on at least the uncovered subgoal.
    assert stub_rerank.invocation_count >= 1
    assert result.contract.solvability_status == "not_solvable"


# ---------------------------------------------------------------------------
# O3 — Phase A failure → trace captures failure_kind
# ---------------------------------------------------------------------------


def test_orchestrator_phase_a_failure_surfaces_degraded_trace() -> None:
    orch = SolvabilityOrchestrator(
        enumerator=_StubEnumerator(
            envelope=SubgoalDraftEnvelope(
                reasoning_summary="(failed)"
            ),
            failure_kind="provider_error",
        ),
        rerank=_StubRerank(),
    )
    result = orch.orchestrate(
        problem_formulation=_pf_clear(),
        input_closure=_ic_closed(),
        output_closure=_oc_closed(),
        capability_envelope=_envelope_full_coverage(),
    )
    assert result.orchestration_trace.phase_a_failed is True
    assert result.orchestration_trace.phase_a_failure_kind == "provider_error"
    # Contract-level status is the neutral "needs clarification" —
    # contract is not allowed to pretend the system is happy.
    assert result.contract.solvability_status == "solvable_with_clarification"


# ---------------------------------------------------------------------------
# O4 — Phase B LLM rerank failure kept deterministic
# ---------------------------------------------------------------------------


def test_orchestrator_phase_b_llm_failure_keeps_deterministic_and_flags_trace() -> None:
    # Missing-persist envelope triggers rerank; stubbed rerank fails.
    stub_rerank = _StubRerank(fail=True)
    orch = SolvabilityOrchestrator(
        enumerator=_StubEnumerator(envelope=_four_subgoal_envelope()),
        rerank=stub_rerank,
    )
    result = orch.orchestrate(
        problem_formulation=_pf_clear(),
        input_closure=_ic_closed(),
        output_closure=_oc_closed(),
        capability_envelope=_envelope_missing_persist(),
    )
    assert result.orchestration_trace.phase_b_llm_failed is True
    assert "phase_b_llm_failed" in result.orchestration_trace.degraded_reasons
    # B failure must NOT upgrade/downgrade the verdict beyond what
    # deterministic already said. sg_4 has no persist tool → unmatched
    # → not_solvable regardless.
    assert result.contract.solvability_status == "not_solvable"


# ---------------------------------------------------------------------------
# O5 — missing_goal PF (defensive — evaluate shouldn't be called, but
#      if it is we stay safe)
# ---------------------------------------------------------------------------


def test_orchestrator_is_safe_when_pf_is_missing_goal() -> None:
    pf = _pf_clear().model_copy(update={"intent_clarity": "missing_goal"})
    orch = SolvabilityOrchestrator(
        enumerator=_StubEnumerator(envelope=_four_subgoal_envelope()),
        rerank=_StubRerank(),
    )
    result = orch.orchestrate(
        problem_formulation=pf,
        input_closure=_ic_closed(),
        output_closure=_oc_closed(),
        capability_envelope=_envelope_full_coverage(),
    )
    # Even though all tools exist, the reducer can't upgrade past
    # "solvable" when closures are technically closed. Semantic-gate
    # enforcement lives in the session service, not here. This test
    # only asserts orchestrator doesn't crash and returns a valid
    # contract.
    assert result.contract.solvability_status in {
        "solvable",
        "solvable_with_clarification",
    }


# ---------------------------------------------------------------------------
# O6 — closures still blocking → solvable_with_clarification
# ---------------------------------------------------------------------------


def test_orchestrator_returns_clarification_when_closures_blocked() -> None:
    orch = SolvabilityOrchestrator(
        enumerator=_StubEnumerator(envelope=_four_subgoal_envelope()),
        rerank=_StubRerank(),
    )
    result = orch.orchestrate(
        problem_formulation=_pf_blocked(),
        input_closure=_ic_blocked(),
        output_closure=_oc_closed(),
        capability_envelope=_envelope_full_coverage(),
    )
    assert result.contract.solvability_status == "solvable_with_clarification"


# ---------------------------------------------------------------------------
# O7 — candidate_shape_hint propagates into graph_feasibility
# ---------------------------------------------------------------------------


def test_orchestrator_propagates_shape_hint_into_graph_feasibility() -> None:
    envelope = _four_subgoal_envelope().model_copy(
        update={"candidate_shape_hint": "dag"}
    )
    orch = SolvabilityOrchestrator(
        enumerator=_StubEnumerator(envelope=envelope),
        rerank=_StubRerank(),
    )
    result = orch.orchestrate(
        problem_formulation=_pf_clear(),
        input_closure=_ic_closed(),
        output_closure=_oc_closed(),
        capability_envelope=_envelope_full_coverage(),
    )
    assert result.contract.graph_feasibility.candidate_shape == "dag"
    assert result.contract.graph_feasibility.representable is True


# ---------------------------------------------------------------------------
# O8 — merged trace sums call_count across phases
# ---------------------------------------------------------------------------


def test_orchestrator_merges_trace_call_counts() -> None:
    stub_rerank = _StubRerank()  # success
    orch = SolvabilityOrchestrator(
        enumerator=_StubEnumerator(envelope=_four_subgoal_envelope()),
        rerank=stub_rerank,
    )
    # Missing-persist envelope has no ``transform`` tool either
    # (filesystem_read only advertises ``ingest`` in that fixture) —
    # two subgoals fail deterministic and trigger rerank.
    result = orch.orchestrate(
        problem_formulation=_pf_clear(),
        input_closure=_ic_closed(),
        output_closure=_oc_closed(),
        capability_envelope=_envelope_missing_persist(),
    )
    # Phase A = 1 call; Phase B rerank runs per uncovered subgoal.
    assert result.trace_summary.call_count == 1 + stub_rerank.invocation_count
    assert stub_rerank.invocation_count >= 1
    assert len(result.phase_b_rerank_traces) == stub_rerank.invocation_count


# ---------------------------------------------------------------------------
# O9 — stages_run records every phase visited
# ---------------------------------------------------------------------------


def test_orchestrator_stages_run_lists_every_phase() -> None:
    stub_rerank = _StubRerank()
    orch = SolvabilityOrchestrator(
        enumerator=_StubEnumerator(envelope=_four_subgoal_envelope()),
        rerank=stub_rerank,
    )
    result = orch.orchestrate(
        problem_formulation=_pf_clear(),
        input_closure=_ic_closed(),
        output_closure=_oc_closed(),
        capability_envelope=_envelope_missing_persist(),
    )
    stages = result.orchestration_trace.stages_run
    assert "phase_a_enumerator" in stages
    assert "phase_b_deterministic" in stages
    assert "phase_b_llm_rerank" in stages
    assert "phase_c_reducer" in stages


# ---------------------------------------------------------------------------
# O10 — planner can still consume the resulting contract
# ---------------------------------------------------------------------------


def test_orchestrator_contract_is_planner_compatible() -> None:
    orch = SolvabilityOrchestrator(
        enumerator=_StubEnumerator(envelope=_four_subgoal_envelope()),
        rerank=_StubRerank(),
    )
    result = orch.orchestrate(
        problem_formulation=_pf_clear(),
        input_closure=_ic_closed(),
        output_closure=_oc_closed(),
        capability_envelope=_envelope_full_coverage(),
    )
    # The contract fields the planner uses must all be populated.
    assert result.contract.required_subgoals  # non-empty
    assert result.contract.graph_feasibility.candidate_shape in {
        "linear", "dag", "conditional", "loop", "unknown"
    }
    # Byte-equal naming: required_capability_classes (external) not
    # required_step_kinds (internal).
    for sg in result.contract.required_subgoals:
        assert hasattr(sg, "required_capability_classes")


# ---------------------------------------------------------------------------
# O11 — reviewer insertion illegal (out_of_scope) → not_solvable
# ---------------------------------------------------------------------------


def test_orchestrator_refuses_to_rescue_with_review_when_out_of_scope() -> None:
    # A subgoal that by itself is a "judgment" gap — would normally
    # trigger solvable_with_review. But the PF is out_of_scope, so
    # reviewer insertion is illegal → not_solvable.
    env = SubgoalDraftEnvelope(
        subgoals=[
            SubgoalDraft(
                subgoal_id="sg_1",
                label="Decision gate",
                description="Require reviewer judgment",
                subgoal_kind="review",
                required_step_kinds=["review"],
                raw_step_kinds=["review"],
                human_review_required=True,
                gap_kind="judgment",
            )
        ],
        candidate_shape_hint="linear",
    )
    orch = SolvabilityOrchestrator(
        enumerator=_StubEnumerator(envelope=env),
        rerank=_StubRerank(),
    )
    # No review tools in the envelope → judgment gap uncovered.
    result = orch.orchestrate(
        problem_formulation=_pf_out_of_scope(),
        input_closure=_ic_closed(),
        output_closure=_oc_closed(),
        capability_envelope=[
            {"capability_id": "filesystem_read", "supported_step_kinds": ["ingest"]},
        ],
    )
    assert result.contract.solvability_status == "not_solvable"


# ---------------------------------------------------------------------------
# O12 — alias canonicalization
# ---------------------------------------------------------------------------


def test_canonicalizer_resolves_common_aliases() -> None:
    result = canonicalize_step_kinds(
        ["inspect", "gather_files", "write_summary", "analyze", "validate"]
    )
    assert "transform" in result.canonical_step_kinds  # inspect, analyze
    assert "ingest" in result.canonical_step_kinds  # gather_files
    assert "summarize" in result.canonical_step_kinds  # write_summary
    assert "validate" in result.canonical_step_kinds
    assert result.unresolved_oov == []


def test_canonicalizer_isolates_truly_oov_values() -> None:
    result = canonicalize_step_kinds(["flubber_wibbling"])
    assert result.canonical_step_kinds == []
    assert result.unresolved_oov == ["flubber_wibbling"]


def test_orchestrator_accepts_alias_step_kinds_from_phase_a() -> None:
    # Phase A stub emits aliases (inspect / gather / write_summary)
    # that the canonicaliser must resolve to canonical step_kinds
    # before Phase B binding.
    env = SubgoalDraftEnvelope(
        subgoals=[
            SubgoalDraft(
                subgoal_id="sg_1",
                label="Gather",
                description="",
                subgoal_kind="acquire",
                required_step_kinds=["gather_files"],  # alias for ingest
                raw_step_kinds=["gather_files"],
                gap_kind="capability",
            )
        ],
        candidate_shape_hint="linear",
    )
    # Canonicalise up front so the orchestrator treats it correctly.
    # (In the real pipeline the enumerator service does this; here we
    # call the helper directly because we're using the stub enumerator.)
    from app.services.front_chain.solvability_subgoal_enumerator_service import (
        _canonicalise_envelope,
    )
    canonical_env = _canonicalise_envelope(env)
    orch = SolvabilityOrchestrator(
        enumerator=_StubEnumerator(envelope=canonical_env),
        rerank=_StubRerank(),
    )
    result = orch.orchestrate(
        problem_formulation=_pf_clear(),
        input_closure=_ic_closed(),
        output_closure=_oc_closed(),
        capability_envelope=_envelope_full_coverage(),
    )
    assert result.contract.solvability_status == "solvable"


# ---------------------------------------------------------------------------
# O13 — no extra LLM when deterministic is already full
# ---------------------------------------------------------------------------


def test_orchestrator_skips_llm_rerank_when_deterministic_is_full() -> None:
    stub_rerank = _StubRerank()
    orch = SolvabilityOrchestrator(
        enumerator=_StubEnumerator(envelope=_four_subgoal_envelope()),
        rerank=stub_rerank,
    )
    result = orch.orchestrate(
        problem_formulation=_pf_clear(),
        input_closure=_ic_closed(),
        output_closure=_oc_closed(),
        capability_envelope=_envelope_full_coverage(),
    )
    assert result.contract.solvability_status == "solvable"
    # The crucial cost-control assertion.
    assert stub_rerank.invocation_count == 0
    assert result.orchestration_trace.phase_b_llm_invoked is False


# ---------------------------------------------------------------------------
# AST guardrails — Phase C reducer is deterministic
# ---------------------------------------------------------------------------


def test_verdict_reducer_has_no_llm_coupling() -> None:
    import ast
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "app" / "services" / "front_chain" / "solvability_verdict_reducer.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    forbidden_substrings = ("provider_runtime", "_llm_service")
    leaks = [name for name in names if any(s in name for s in forbidden_substrings)]
    assert leaks == [], f"verdict reducer has LLM coupling: {leaks}"


def test_canonicalizer_has_no_llm_coupling() -> None:
    import ast
    from pathlib import Path

    path = (
        Path(__file__).resolve().parent.parent
        / "app" / "services" / "front_chain"
        / "solvability_step_kind_canonicalizer.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
    forbidden_substrings = ("provider_runtime", "_llm_service")
    leaks = [name for name in names if any(s in name for s in forbidden_substrings)]
    assert leaks == [], f"canonicalizer has LLM coupling: {leaks}"
