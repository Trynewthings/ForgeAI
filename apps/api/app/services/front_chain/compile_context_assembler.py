"""Phase 4: CompileContextAssembler — deterministic packaging.

This module assembles a ``CompileContextResponse`` from the six v2 authority
objects stored on a ``FrontChainState`` row. It is the planner-facing exit
point of the front-chain pipeline.

Key constraints:
    R1  — CompileContextResponse is the planner's sole authoritative input.
    R2  — candidate_work_units are hint material only, NOT final nodes.
    R5  — selected_blueprint_type is NOT planner authority — the v2 path
          intentionally leaves it unset; the planner derives it from
          ``execution_admissibility.primary_execution_regime`` if needed.
    R7  — This module is pure deterministic packaging.  Zero LLM calls.
    R10 — No imports from old compiler / requirement services.

Deprecated fields the v2 path does NOT populate:
    * ``selected_blueprint_type`` — see R5 above.
    * ``graph_closure_status`` — redundant with ``solvability.graph_feasibility``.
    * ``selected_template_id`` — old template-selection concept; no v2 role.
    * ``semantic_core`` — hard-removed from the schema.
"""
from __future__ import annotations

from typing import Optional

from app.schemas.problem_formulation import (
    InputClosureContract,
    OutputClosureContract,
    ProblemFormulationContract,
)
from app.schemas.requirement_compiler import (
    CompileContextCapabilityEnvelopeResponse,
    CompileContextConstraintsResponse,
    CompileContextResponse,
    CompileContextWorkUnitResponse,
)
from app.schemas.solvability import (
    CapabilitySignature,
    SolvabilityContract,
)
from app.schemas.workflow_native import ExecutionAdmissibilityContract

ASSEMBLER_VERSION = "compile_context_assembler_v1"


class CompileContextAssembler:
    """Deterministic assembler: six authority objects → CompileContextResponse.

    No LLM calls.  No old-world service imports.  Pure packaging (R7).
    """

    def assemble(
        self,
        *,
        state_id: str,
        project_id: str,
        requirement_id: Optional[str],
        problem_formulation: ProblemFormulationContract,
        input_closure: InputClosureContract,
        output_closure: OutputClosureContract,
        solvability: SolvabilityContract,
        execution_admissibility: ExecutionAdmissibilityContract,
    ) -> CompileContextResponse:
        return CompileContextResponse(
            compile_context_version="compile_context_v1",
            project_id=project_id,
            requirement_id=requirement_id,
            # Deprecated fields intentionally left unset on the v2 path:
            # selected_blueprint_type, graph_closure_status, selected_template_id
            problem_formulation=problem_formulation,
            input_closure=input_closure,
            output_closure=output_closure,
            solvability=solvability,
            execution_admissibility=execution_admissibility,
            capability_envelope=_build_capability_envelope(solvability),
            compile_constraints=_build_compile_constraints(execution_admissibility),
            candidate_work_units=_build_candidate_work_units(solvability),
            compile_trace={
                "authority_source": "front_chain_v2",
                "assembler_version": ASSEMBLER_VERSION,
                "state_id": state_id,
            },
        )


# ---------------------------------------------------------------------------
# Private helpers (all deterministic, no LLM, no old-world imports)
# ---------------------------------------------------------------------------


def _build_capability_envelope(
    solvability: SolvabilityContract,
) -> CompileContextCapabilityEnvelopeResponse:
    """Build capability signatures from solvability.capability_matches.

    Each matched capability becomes a lightweight CapabilitySignature.
    Port kinds (accepted_input_kinds, produced_output_kinds) are derived
    from the corresponding subgoal's evidence kinds.
    """
    subgoal_index = {sg.subgoal_id: sg for sg in solvability.required_subgoals}
    seen_ids: set[str] = set()
    signatures: list[CapabilitySignature] = []

    for match in solvability.capability_matches:
        subgoal = subgoal_index.get(match.subgoal_id)
        for cap_id in match.matched_capability_ids:
            if cap_id in seen_ids:
                continue
            seen_ids.add(cap_id)
            signatures.append(
                CapabilitySignature(
                    capability_id=cap_id,
                    logical_step_kinds=list(match.required_capability_classes),
                    accepted_input_kinds=list(subgoal.input_evidence_kinds) if subgoal else [],
                    produced_output_kinds=list(subgoal.output_evidence_kinds) if subgoal else [],
                    graph_support=["linear", "dag"],
                    runtime_adapter_kind="unknown",
                )
            )

    return CompileContextCapabilityEnvelopeResponse(capability_signatures=signatures)


def _build_compile_constraints(
    admissibility: ExecutionAdmissibilityContract,
) -> CompileContextConstraintsResponse:
    notes: list[str] = []
    require_human_gate_before: list[str] = []
    forbidden_side_effects: list[str] = []

    if admissibility.requires_human_review:
        require_human_gate_before.append("act")
        notes.append("Human review required: human gate before side-effect nodes.")

    if admissibility.max_automation_level in ("analysis_only", "plan_only"):
        forbidden_side_effects.extend(["filesystem_write", "network_write", "system_modify"])
        notes.append(f"Automation level {admissibility.max_automation_level}: side-effect nodes forbidden.")

    return CompileContextConstraintsResponse(
        allowed_node_semantic_types=["acquire", "transform", "act", "verify", "human_gate"],
        forbidden_side_effect_classes=forbidden_side_effects,
        require_human_gate_before=require_human_gate_before,
        require_verification_after=[],
        compile_notes=notes,
    )


def _build_candidate_work_units(
    solvability: SolvabilityContract,
) -> list[CompileContextWorkUnitResponse]:
    """R2: work units are hint material only, NOT final nodes."""
    units: list[CompileContextWorkUnitResponse] = []
    for index, subgoal in enumerate(solvability.required_subgoals, start=1):
        units.append(
            CompileContextWorkUnitResponse(
                work_unit_id=f"hint_{index}",
                label=subgoal.label,
                objective=subgoal.description,
                concern_kind=subgoal.subgoal_kind,
                concern_tags=_unique_nonempty_strings(
                    [subgoal.graph_role, *subgoal.input_evidence_kinds, *subgoal.output_evidence_kinds]
                ),
                required_capability_themes=list(subgoal.required_capability_classes),
                supporting_subgoal_ids=[subgoal.subgoal_id],
                parallelism_hint="available" if subgoal.graph_role == "fanout_branch" else None,
                human_gate_hint="required" if subgoal.human_review_required else None,
            )
        )
    return units


def _unique_nonempty_strings(values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped
