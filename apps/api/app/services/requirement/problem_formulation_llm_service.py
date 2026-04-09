from __future__ import annotations

from typing import Any

from app.schemas.problem_formulation import ProblemFormulationLLMCandidate
from app.schemas.requirement_session import RequirementAuthoringSemanticStateResponse
from app.services.requirement.compiler.contracts import LLMPromptContract
from app.services.requirement.compiler.llm_assisted import CompilerLLMClient


def _problem_formulation_prompt_contract() -> LLMPromptContract:
    return LLMPromptContract(
        contract_id="compiler.problem_formulation.v1_0_0",
        purpose=(
            "Interpret the current requirement as a bounded problem envelope for the current system. "
            "Return only problem formulation signals, not workflow nodes, edges, steps, or build/runtime structure."
        ),
        allowed_inputs=[
            "Current semantic state.",
            "Normalized requirement payload fields such as goal, input_description, and output_description.",
            "Current deterministic target-object guess and bounded target-object vocabulary.",
            "Allowed clarification axes for problem formulation.",
        ],
        forbidden_behavior=[
            "Do not invent workflow graphs, nodes, edges, or execution structure.",
            "Do not invent capabilities outside the current system vocabulary.",
            "Do not return markdown, prose, or any content outside one JSON object.",
            "Do not decide capability legality, graph feasibility, or runtime support.",
        ],
        output_schema=ProblemFormulationLLMCandidate.model_json_schema(),
        uncertainty_handling=[
            'If the problem remains ambiguous, leave fields null or generic and use "required_decisions" / "blocking_axes".',
            'Use "current_system_fit" to indicate only high-level fit, not capability proof.',
            'Do not fabricate missing input or output facts.',
        ],
    )


class ProblemFormulationLLMService:
    def propose(
        self,
        *,
        llm_client: CompilerLLMClient | None,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        payload: dict[str, Any],
        deterministic_target_object: str,
    ) -> ProblemFormulationLLMCandidate | None:
        if llm_client is None:
            return None
        model, accepted, _validation_records, _fallback_events, _stage_trace = llm_client.request_structured(
            stage="problem_formulation",
            schema_model=ProblemFormulationLLMCandidate,
            prompt_contract=_problem_formulation_prompt_contract(),
            prompt_input={
                "semantic_state": semantic_state.model_dump(mode="python"),
                "requirement_payload": {
                    "goal": payload.get("goal"),
                    "input_description": payload.get("input_description"),
                    "output_description": payload.get("output_description"),
                    "domain": payload.get("domain"),
                    "recurrence_mode": payload.get("recurrence_mode"),
                },
                "deterministic_target_object": deterministic_target_object,
                "allowed_target_objects": [
                    "repository",
                    "web_resource",
                    "dataset",
                    "local_workspace",
                    "generic_request",
                    "software_project",
                ],
                "allowed_problem_axes": [
                    "target_object",
                    "deliverable_form",
                    "analysis_frame",
                    "workflow_shape",
                    "source_modality",
                    "source_locator",
                    "delivery_target",
                    "system_fit",
                ],
            },
        )
        if not accepted or model is None:
            return None
        return ProblemFormulationLLMCandidate.model_validate(model.model_dump(mode="python"))
