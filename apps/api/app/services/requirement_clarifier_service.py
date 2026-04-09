from __future__ import annotations

import json
import re
from typing import Any

from app.schemas.requirement_session import (
    RequirementAuthoringSemanticStateResponse,
    RequirementDraftPatchResponse,
    RequirementDraftPatchStateResponse,
    RequirementSemanticFieldProvenanceResponse,
)
from app.schemas.semantic_core import DataSemanticPack
from app.schemas.requirement_payloads import (
    empty_acceptance_criteria_payload,
    empty_constraints_payload,
    normalize_acceptance_criteria_payload,
    normalize_constraints_payload,
    normalize_external_tools_payload,
    normalize_interactivity_level,
    normalize_optional_text,
    normalize_recurrence_mode,
)
from app.services.capabilities import CapabilityRegistryService
from app.services.requirement.compiler.contracts import LLMPromptContract


class RequirementClarifierService:
    FIELD_ALIASES = {
        "goal": ["goal", "目标", "problem", "problem to solve"],
        "target_users": ["target users", "users", "audience", "用户"],
        "delivery_form": ["delivery form", "platform", "交付形式", "form factor"],
        "p0_features": ["p0 features", "must-have features", "core features", "features", "核心功能"],
        "p1_features": ["p1 features", "nice-to-have features", "future features"],
        "constraints": ["constraints", "限制", "约束"],
        "input_items": ["inputs", "input", "input description", "输入"],
        "output_items": ["outputs", "output", "output description", "输出"],
        "deployment": ["deployment", "部署"],
        "acceptance_criteria": ["quality requirements", "quality", "non-functional", "质量要求", "acceptance criteria", "success criteria"],
        "domain": ["domain", "business context", "context"],
        "interactivity_level": ["interactivity", "coordination level", "interaction level"],
        "recurrence_mode": ["recurrence", "frequency", "cadence", "schedule"],
        "external_tools": ["tools", "external tools", "integrations", "sources", "systems"],
        "human_review_required": ["human review required", "review required", "approval required"],
    }
    LIST_FIELDS = {
        "target_users",
        "p0_features",
        "p1_features",
        "constraints",
        "input_items",
        "output_items",
        "acceptance_criteria",
        "external_tools",
    }
    BOOLEAN_FIELDS = {"human_review_required"}

    def __init__(self) -> None:
        self.capability_registry = CapabilityRegistryService()

    def build_empty_requirement_draft(
        self,
        *,
        project_id: str,
        session_id: str,
        raw_user_input: str | None = None,
        messages: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return {
            "project_id": project_id,
            "title": "Requirement Session Draft",
            "summary": None,
            "goal": None,
            "target_users": None,
            "input_description": None,
            "output_description": None,
            "domain": None,
            "interactivity_level": None,
            "human_review_required": False,
            "recurrence_mode": None,
            "constraints_json": empty_constraints_payload().model_dump(mode="python"),
            "acceptance_criteria_json": empty_acceptance_criteria_payload().model_dump(mode="python"),
            "non_goals_json": {},
            "priority_features_json": {},
            "external_tools_needed_json": {"items": []},
            "raw_conversation_json": {
                "source": "requirement_session",
                "session_id": session_id,
                "raw_user_input": raw_user_input or "",
                "messages": messages or [],
            },
            "structured_spec_json": {
                "source": "requirement_session",
                "session_id": session_id,
                "delivery_form": None,
                "p0_features": [],
                "p1_features": [],
                "deployment": None,
                "target_users_list": [],
                "acceptance_criteria_list": [],
                "conversation_summary": "",
                "authoring_semantic_state": self._build_empty_authoring_semantic_state().model_dump(mode="python"),
                "draft_projection_meta": self._build_empty_draft_projection_meta(),
                "draft_patch_state": self._default_patch_state(no_op_reason="no_patch_attempted").model_dump(mode="python"),
            },
        }

    def update_requirement_draft_from_messages(
        self,
        *,
        project_id: str,
        session_id: str,
        messages: list[str],
        raw_user_input: str | None = None,
        conversation: list[dict[str, str]] | None = None,
        previous_draft: dict[str, Any] | None = None,
        authoring_feedback: dict[str, Any] | None = None,
        llm_client=None,
    ) -> dict[str, Any]:
        semantic_state = self._load_authoring_semantic_state(previous_draft)
        semantic_state, refinement_meta = self._refine_authoring_semantic_state(
            semantic_state=semantic_state,
            messages=messages,
            conversation=conversation or [],
            authoring_feedback=authoring_feedback,
            llm_client=llm_client,
        )

        draft = self.build_empty_requirement_draft(
            project_id=project_id,
            session_id=session_id,
            raw_user_input=raw_user_input,
            messages=conversation,
        )
        if previous_draft:
            draft = self._merge_with_previous_draft(draft, previous_draft)
        draft = self._project_requirement_draft_from_semantic_state(
            draft=draft,
            semantic_state=semantic_state,
            session_id=session_id,
        )
        patch_state = self._build_semantic_refinement_patch_state(refinement_meta)
        draft.setdefault("structured_spec_json", {})
        draft["structured_spec_json"]["authoring_semantic_state"] = semantic_state.model_dump(mode="python")
        draft["structured_spec_json"]["draft_patch_state"] = patch_state.model_dump(mode="python")
        return draft

    def build_assistant_reply(
        self,
        requirement_draft: dict[str, Any],
        *,
        authoring_feedback: dict[str, Any] | None = None,
    ) -> str:
        summary = self._draft_summary_text(requirement_draft)
        if authoring_feedback:
            completion_state = authoring_feedback.get("completion_state_preview")
            explanation = authoring_feedback.get("explanation") or {}
            readiness = explanation.get("readiness") or {}
            if completion_state == "ready_to_compile":
                return (
                    f"{summary}\n\n"
                    "The requirement draft now looks ready enough to finalize and export into the "
                    "compiler-facing Requirement flow."
                )
            if completion_state == "compilable_with_warnings":
                warnings = (authoring_feedback.get("warnings") or [])[:2]
                warning_line = "\n".join(f"- {item}" for item in warnings) if warnings else "- Review warning-level details in the preview."
                return (
                    f"{summary}\n\n"
                    "The requirement draft is ready enough to finalize and export, but warning-level details still need review:\n"
                    f"{warning_line}"
                )
            blocking_items = list(readiness.get("blocking_items") or [])[:3]
            if blocking_items:
                formatted = "\n".join(f"{index + 1}. {item}" for index, item in enumerate(blocking_items))
                return f"{summary}\n\nBlocking clarification work remains:\n{formatted}"
        return f"{summary}\n\nKeep refining the requirement draft until the blocking clarification items are resolved."

    def build_provider_messages(
        self,
        requirement_draft: dict[str, Any],
        conversation: list[dict[str, str]],
        *,
        authoring_feedback: dict[str, Any] | None = None,
    ) -> list[dict[str, str]]:
        compact_feedback = None
        if authoring_feedback:
            compact_feedback = {
                "completion_state_preview": authoring_feedback.get("completion_state_preview"),
                "gaps": authoring_feedback.get("gaps", []),
                "open_questions": authoring_feedback.get("open_questions", []),
                "recommended_family": authoring_feedback.get("recommended_family"),
                "selected_template_id": (authoring_feedback.get("template_selection") or {}).get("selected_template_id"),
                "blocking_clarification_count": authoring_feedback.get("blocking_clarification_count", 0),
                "warning_clarification_count": authoring_feedback.get("warning_clarification_count", 0),
            }
        transcript = "\n".join(f"{item['role']}: {item['content']}" for item in conversation[-12:])
        compact_draft = {
            "title": requirement_draft.get("title"),
            "summary": requirement_draft.get("summary"),
            "goal": requirement_draft.get("goal"),
            "target_users": requirement_draft.get("target_users"),
            "input_description": requirement_draft.get("input_description"),
            "output_description": requirement_draft.get("output_description"),
            "domain": requirement_draft.get("domain"),
            "interactivity_level": requirement_draft.get("interactivity_level"),
            "human_review_required": requirement_draft.get("human_review_required"),
            "recurrence_mode": requirement_draft.get("recurrence_mode"),
            "external_tools_needed_json": requirement_draft.get("external_tools_needed_json"),
            "structured_spec_json": requirement_draft.get("structured_spec_json", {}),
            "authoring_semantic_state": (requirement_draft.get("structured_spec_json") or {}).get("authoring_semantic_state", {}),
        }
        return [
            {
                "role": "system",
                "content": (
                    "You are the Requirement Analyst for AgentForge. "
                    "Respond as a real clarification assistant, not as a template. "
                    "Use the current requirement draft and latest conversation. "
                    "Treat the deterministic authoring-feedback preview as authoritative for whether finalize is blocked or allowed. "
                    "If required fields are still missing, ask at most three concrete next-step questions. "
                    "If the requirement draft is already ready enough, summarize it and say it can be finalized. "
                    "Keep the response concise and directly useful."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Current requirement draft JSON:\n"
                    f"{json.dumps(compact_draft, ensure_ascii=False, indent=2)}\n\n"
                    "Current deterministic authoring-feedback preview:\n"
                    f"{json.dumps(compact_feedback or {}, ensure_ascii=False, indent=2)}\n\n"
                    "Conversation transcript:\n"
                    f"{transcript}"
                ),
            },
        ]

    def build_markdown_projection(self, project_name: str, requirement_draft: dict[str, Any], messages: list[str]) -> str:
        def render_list(items: list[str]) -> str:
            return "\n".join(f"- {item}" for item in items) if items else "- None specified yet"

        spec = requirement_draft.get("structured_spec_json") or {}
        constraints_items = ((requirement_draft.get("constraints_json") or {}).get("items") or [])
        acceptance_items = ((requirement_draft.get("acceptance_criteria_json") or {}).get("items") or [])
        tool_items = ((requirement_draft.get("external_tools_needed_json") or {}).get("items") or [])
        return "\n".join(
            [
                f"# Requirement Draft: {project_name}",
                "",
                "## Goal",
                requirement_draft.get("goal") or "Not specified",
                "",
                "## Summary",
                requirement_draft.get("summary") or "Not specified",
                "",
                "## Target Users",
                render_list(self._split_text_list(requirement_draft.get("target_users")) or spec.get("target_users_list", [])),
                "",
                "## Delivery Form",
                spec.get("delivery_form") or "Not specified",
                "",
                "## P0 Features",
                render_list(spec.get("p0_features", [])),
                "",
                "## P1 Features",
                render_list(spec.get("p1_features", [])),
                "",
                "## Inputs",
                render_list(self._split_text_list(requirement_draft.get("input_description"))),
                "",
                "## Outputs",
                render_list(self._split_text_list(requirement_draft.get("output_description"))),
                "",
                "## Acceptance Criteria",
                render_list([item.get("description") for item in acceptance_items if item.get("description")]),
                "",
                "## Constraints",
                render_list([item.get("rule") for item in constraints_items if item.get("rule")]),
                "",
                "## Domain",
                requirement_draft.get("domain") or "Not specified",
                "",
                "## Interactivity Level",
                requirement_draft.get("interactivity_level") or "Not specified",
                "",
                "## Recurrence Mode",
                requirement_draft.get("recurrence_mode") or "Not specified",
                "",
                "## Human Review Required",
                "Yes" if requirement_draft.get("human_review_required") else "No",
                "",
                "## External Tools",
                render_list([str(item) for item in tool_items if str(item).strip()]),
                "",
                "## Deployment",
                spec.get("deployment") or "Not specified",
                "",
                "## Conversation Notes",
                render_list(messages),
            ]
        )

    def _build_empty_authoring_semantic_state(self) -> RequirementAuthoringSemanticStateResponse:
        return RequirementAuthoringSemanticStateResponse()

    def _build_empty_draft_projection_meta(self) -> dict[str, Any]:
        return {
            "projection_source": "authoring_semantic_state",
            "field_provenance": {},
        }

    def _load_authoring_semantic_state(
        self,
        previous_draft: dict[str, Any] | None,
    ) -> RequirementAuthoringSemanticStateResponse:
        raw = (((previous_draft or {}).get("structured_spec_json") or {}).get("authoring_semantic_state") or {})
        try:
            return RequirementAuthoringSemanticStateResponse.model_validate(raw)
        except Exception:
            return self._build_empty_authoring_semantic_state()

    def _refine_authoring_semantic_state(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        messages: list[str],
        conversation: list[dict[str, str]],
        authoring_feedback: dict[str, Any] | None,
        llm_client=None,
    ) -> tuple[RequirementAuthoringSemanticStateResponse, dict[str, Any]]:
        hints = self._extract_semantic_hints(
            semantic_state=semantic_state,
            messages=messages,
            conversation=conversation,
        )
        llm_state = self._llm_semantic_refinement(
            semantic_state=semantic_state,
            hints=hints,
            conversation=conversation,
            authoring_feedback=authoring_feedback,
            llm_client=llm_client,
        )
        refinement_source = "llm_semantic_refinement"
        fallback_used = False
        next_state = llm_state
        if next_state is None:
            next_state = self._fallback_semantic_refinement(
                semantic_state=semantic_state,
                conversation=conversation,
            )
            refinement_source = "fallback_semantic_refinement"
            fallback_used = True
        else:
            next_state = self._merge_refined_state_with_previous(
                previous_state=semantic_state,
                refined_state=next_state,
            )

        next_state = self._validate_semantic_state(
            semantic_state=next_state,
            hints=hints,
        )
        return next_state, {
            "llm_used": llm_state is not None,
            "fallback_used": fallback_used,
            "refinement_source": refinement_source,
            "confidence": next_state.confidence,
        }

    def _merge_refined_state_with_previous(
        self,
        *,
        previous_state: RequirementAuthoringSemanticStateResponse,
        refined_state: RequirementAuthoringSemanticStateResponse,
    ) -> RequirementAuthoringSemanticStateResponse:
        merged = refined_state.model_copy(deep=True)
        for field_name in (
            "goal",
            "domain",
            "delivery_form",
            "deployment",
            "interactivity_level",
            "recurrence_mode",
        ):
            if not self._normalize_text(getattr(merged, field_name)):
                setattr(merged, field_name, getattr(previous_state, field_name))
        for field_name in (
            "target_users",
            "input_items",
            "output_items",
            "acceptance_criteria",
            "constraints",
            "p0_features",
            "p1_features",
            "external_tools",
            "evidence_requirements",
            "pending_semantic_fields",
            "ambiguities",
        ):
            merged_items = list(getattr(merged, field_name) or [])
            if not merged_items:
                setattr(merged, field_name, list(getattr(previous_state, field_name) or []))
        if not merged.human_review_required:
            merged.human_review_required = bool(previous_state.human_review_required)
        if merged.target_object == "generic_request" and previous_state.target_object != "generic_request":
            merged.target_object = previous_state.target_object
        if merged.deliverable_contract == "generic_output" and previous_state.deliverable_contract != "generic_output":
            merged.deliverable_contract = previous_state.deliverable_contract
        if merged.deliverable_form == "generic_output" and previous_state.deliverable_form != "generic_output":
            merged.deliverable_form = previous_state.deliverable_form
        if merged.analysis_frame == "generic" and previous_state.analysis_frame != "generic":
            merged.analysis_frame = previous_state.analysis_frame
        if merged.decision_posture == "generic" and previous_state.decision_posture != "generic":
            merged.decision_posture = previous_state.decision_posture
        if not merged.evidence_basis:
            merged.evidence_basis = list(previous_state.evidence_basis or [])
        if merged.workflow_shape == "single_service" and previous_state.workflow_shape != "single_service":
            merged.workflow_shape = previous_state.workflow_shape
        if merged.source_modality == "unknown" and previous_state.source_modality != "unknown":
            merged.source_modality = previous_state.source_modality
        merged.domain_packs = self._merge_domain_packs(
            previous_state=previous_state,
            merged_state=merged,
        )
        merged.field_provenance = dict(previous_state.field_provenance or {}) | dict(merged.field_provenance or {})
        return merged

    def _merge_domain_packs(
        self,
        *,
        previous_state: RequirementAuthoringSemanticStateResponse,
        merged_state: RequirementAuthoringSemanticStateResponse,
    ):
        packs = merged_state.domain_packs.model_copy(deep=True)
        previous_packs = previous_state.domain_packs
        for pack_name in ("repository_pack", "research_pack", "data_pack", "software_pack", "automation_pack"):
            current_pack = getattr(packs, pack_name)
            prior_pack = getattr(previous_packs, pack_name)
            if current_pack is None and prior_pack is not None:
                setattr(packs, pack_name, prior_pack.model_copy(deep=True))
                continue
            if current_pack is None or prior_pack is None:
                continue
            for field_name in current_pack.__class__.model_fields:
                if getattr(current_pack, field_name) is None and getattr(prior_pack, field_name) is not None:
                    setattr(current_pack, field_name, getattr(prior_pack, field_name))
        return packs

    def _extract_semantic_hints(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        messages: list[str],
        conversation: list[dict[str, str]],
    ) -> dict[str, Any]:
        path_hints: list[str] = []
        repo_url_hints: list[str] = []
        web_url_hints: list[str] = []
        output_format_hints: list[str] = []
        output_file_hints: list[str] = []
        data_file_hints: list[str] = []
        review_hints: list[str] = []

        for message in conversation:
            if message.get("role") != "user":
                continue
            content = message.get("content") or ""
            lower = content.lower()
            path_hints.extend(re.findall(r"(\/[A-Za-z0-9._\-\/]+)", content))
            repo_url_hints.extend(re.findall(r"https?://github\.com/[^\s)]+", content, flags=re.IGNORECASE))
            web_url_hints.extend(re.findall(r"https?://[^\s)]+", content, flags=re.IGNORECASE))
            if any(token in lower for token in ("markdown", ".md", "md 文档", "markdown文件", "markdown 文档")):
                output_format_hints.append("markdown")
            if any(token in lower for token in ("pdf", ".pdf")):
                output_format_hints.append("pdf")
            if any(token in lower for token in ("json", ".json")):
                output_format_hints.append("json")
            output_file_hints.extend(re.findall(r"\b[A-Za-z0-9._-]+\.(?:md|markdown|pdf|json|csv|txt)\b", content))
            data_file_hints.extend(re.findall(r"\b[A-Za-z0-9._-]+\.(?:csv|tsv|xlsx|xls|parquet)\b", content, flags=re.IGNORECASE))
            if re.search(r"\b(review|approve|approval|manual)\b", lower) or any(
                token in content for token in ("审阅", "评审", "人工确认")
            ):
                review_hints.append(content.strip())

        return {
            "latest_user_message": messages[-1] if messages else "",
            "absolute_paths": self._dedupe(path_hints),
            "repository_urls": self._dedupe(repo_url_hints),
            "web_urls": self._dedupe(web_url_hints),
            "output_formats": self._dedupe(output_format_hints),
            "output_files": self._dedupe(output_file_hints),
            "data_files": self._dedupe(data_file_hints),
            "review_hints": self._dedupe(review_hints),
            "previous_semantic_core": semantic_state.trace_summary(),
        }

    def _llm_semantic_refinement(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        hints: dict[str, Any],
        conversation: list[dict[str, str]],
        authoring_feedback: dict[str, Any] | None,
        llm_client,
    ) -> RequirementAuthoringSemanticStateResponse | None:
        if llm_client is None:
            return None
        model, _, _, _, _ = llm_client.request_structured(
            stage="session_semantic_refinement",
            schema_model=RequirementAuthoringSemanticStateResponse,
            prompt_contract=self._authoring_semantic_refinement_prompt_contract(),
            prompt_input={
                "previous_semantic_state": semantic_state.model_dump(mode="python"),
                "semantic_hints": hints,
                "recent_conversation": conversation[-10:],
                "authoring_feedback_preview": {
                    "completion_state_preview": (authoring_feedback or {}).get("completion_state_preview"),
                    "open_questions": (authoring_feedback or {}).get("open_questions", []),
                    "warnings": (authoring_feedback or {}).get("warnings", []),
                },
            },
        )
        return model if isinstance(model, RequirementAuthoringSemanticStateResponse) else None

    def _validate_semantic_state(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        hints: dict[str, Any],
    ) -> RequirementAuthoringSemanticStateResponse:
        state = semantic_state.model_copy(deep=True)
        state.goal = self._normalize_text(state.goal)
        state.target_users = self._dedupe([item for item in state.target_users if self._normalize_text(item)])
        state.input_items = self._dedupe([item for item in state.input_items if self._normalize_text(item)])
        state.output_items = self._dedupe([item for item in state.output_items if self._normalize_text(item)])
        for hinted_path in hints.get("absolute_paths", []):
            state.input_items = [
                hinted_path if hinted_path in item else item
                for item in state.input_items
            ]
        for hinted_file in hints.get("output_files", []):
            state.output_items = [
                hinted_file if hinted_file in item else item
                for item in state.output_items
            ]
        state.input_items = self._dedupe(state.input_items)
        state.output_items = self._dedupe(state.output_items)
        state.acceptance_criteria = self._dedupe([item for item in state.acceptance_criteria if self._normalize_text(item)])
        state.constraints = self._dedupe([item for item in state.constraints if self._normalize_text(item)])
        state.p0_features = self._dedupe([item for item in state.p0_features if self._normalize_text(item)])
        state.p1_features = self._dedupe([item for item in state.p1_features if self._normalize_text(item)])
        state.external_tools = self._dedupe([item for item in state.external_tools if self._normalize_text(item)])
        state.domain = self._normalize_text(state.domain)
        state.delivery_form = self._normalize_text(state.delivery_form)
        state.deployment = self._normalize_text(state.deployment)
        state.interactivity_level = normalize_interactivity_level(state.interactivity_level)
        state.recurrence_mode = normalize_recurrence_mode(state.recurrence_mode)
        state.human_review_required = bool(state.human_review_required or bool(hints.get("review_hints")))
        state.domain_packs = self._derive_domain_packs(
            semantic_state=state,
            hints=hints,
        )
        state = RequirementAuthoringSemanticStateResponse.model_validate(state.model_dump(mode="python"))

        ambiguities = list(state.ambiguities or [])
        if state.target_object == "repository" and state.source_modality in {"web_source", "local_path"}:
            ambiguities.append("repository_source_modality")
        if state.deliverable_contract == "architecture_report" and state.target_object != "repository":
            ambiguities.append("target_object")
        if state.deliverable_form == "report" and state.analysis_frame == "generic":
            ambiguities.append("analysis_frame")
        if state.deliverable_contract in {"architecture_report", "research_briefing"} and not state.evidence_requirements:
            ambiguities.append("evidence_requirements")
        if state.deliverable_form in {"report", "briefing"} and not state.evidence_basis:
            ambiguities.append("evidence_basis")
        if state.workflow_shape == "single_service" and state.deliverable_contract in {"architecture_report", "research_briefing"}:
            ambiguities.append("workflow_shape")
        state.ambiguities = self._dedupe(ambiguities)
        state.pending_semantic_fields = self._derive_pending_semantic_fields(
            semantic_state=state,
            authoring_feedback=None,
        )
        state.ambiguities = self._derive_authoring_ambiguities(state)
        state.confidence = self._authoring_semantic_confidence(state)
        return state

    def _fallback_semantic_refinement(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        conversation: list[dict[str, str]],
    ) -> RequirementAuthoringSemanticStateResponse:
        next_state = semantic_state.model_copy(deep=True)
        user_turn_index = 0
        for message in conversation:
            if message.get("role") != "user":
                continue
            capture = self._build_empty_capture()
            content = message.get("content") or ""
            self._apply_explicit_fields(capture, content)
            self._apply_heuristics(capture, content)
            self._apply_conversation_slot_patterns(capture, content)
            if self._normalize_text(next_state.goal) and not self._message_explicitly_sets_goal(content):
                capture["goal"] = None
            for field_name in self.LIST_FIELDS:
                capture[field_name] = self._dedupe(capture[field_name])
            next_state = self._merge_capture_into_semantic_state(
                semantic_state=next_state,
                capture=capture,
                source_turn_index=user_turn_index,
            )
            user_turn_index += 1
        return self._infer_authoring_semantic_shape(next_state)

    def _build_semantic_refinement_patch_state(
        self,
        refinement_meta: dict[str, Any],
    ) -> RequirementDraftPatchStateResponse:
        no_op_reason = "semantic_refinement_applied"
        if refinement_meta.get("fallback_used"):
            no_op_reason = "semantic_refinement_fallback"
        elif not refinement_meta.get("llm_used"):
            no_op_reason = "semantic_refinement_unavailable"
        return RequirementDraftPatchStateResponse(
            applied=False,
            confidence=max(0.0, min(1.0, float(refinement_meta.get("confidence", 0)) / 100.0)),
            notes=[f"Authoring semantic pipeline source: {refinement_meta.get('refinement_source', 'unknown')}"],
            no_op_reason=no_op_reason,
            capability_gap_summary=self.capability_registry.summarize_capability_gaps([]),
        )

    def _merge_capture_into_semantic_state(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        capture: dict[str, Any],
        source_turn_index: int,
    ) -> RequirementAuthoringSemanticStateResponse:
        state = semantic_state.model_copy(deep=True)
        provenance = dict(state.field_provenance)

        scalar_map = {
            "goal": capture.get("goal"),
            "domain": capture.get("domain"),
            "delivery_form": capture.get("delivery_form"),
            "deployment": capture.get("deployment"),
            "interactivity_level": normalize_interactivity_level(capture.get("interactivity_level")),
            "recurrence_mode": normalize_recurrence_mode(capture.get("recurrence_mode")),
        }
        for field_name, raw_value in scalar_map.items():
            value = self._normalize_text(raw_value)
            if not value:
                continue
            setattr(state, field_name, value)
            provenance[field_name] = RequirementSemanticFieldProvenanceResponse(
                source_turn_index=source_turn_index,
                source_role="user",
                derivation_method="semantic_capture",
                confidence=0.92,
            )

        if capture.get("human_review_required"):
            state.human_review_required = True
            provenance["human_review_required"] = RequirementSemanticFieldProvenanceResponse(
                source_turn_index=source_turn_index,
                source_role="user",
                derivation_method="semantic_capture",
                confidence=0.9,
            )

        list_map = {
            "target_users": capture.get("target_users", []),
            "input_items": capture.get("input_items", []),
            "output_items": capture.get("output_items", []),
            "acceptance_criteria": capture.get("acceptance_criteria", []),
            "constraints": capture.get("constraints", []),
            "p0_features": capture.get("p0_features", []),
            "p1_features": capture.get("p1_features", []),
            "external_tools": capture.get("external_tools", []),
        }
        for field_name, raw_items in list_map.items():
            items = self._dedupe([item for item in raw_items if self._normalize_text(item)])
            if not items:
                continue
            setattr(state, field_name, items)
            provenance[field_name] = RequirementSemanticFieldProvenanceResponse(
                source_turn_index=source_turn_index,
                source_role="user",
                derivation_method="semantic_capture",
                confidence=0.92,
            )

        state.field_provenance = provenance
        return state

    def _infer_authoring_semantic_shape(
        self,
        semantic_state: RequirementAuthoringSemanticStateResponse,
    ) -> RequirementAuthoringSemanticStateResponse:
        state = semantic_state.model_copy(deep=True)
        combined = " ".join(
            part
            for part in [
                state.goal or "",
                "; ".join(state.input_items),
                "; ".join(state.output_items),
                state.domain or "",
                "; ".join(state.acceptance_criteria),
            ]
            if part
        ).lower()
        structured_data_language = any(
            token in combined
            for token in (
                "csv",
                "xlsx",
                "xls",
                "spreadsheet",
                "table",
                "tabular",
                "rows",
                "columns",
                "metrics",
                "kpi",
                "dataset",
                "dataframe",
                "数据表",
                "表格",
                "指标",
                "数据集",
                "电子表格",
            )
        )

        if re.search(r"\b(repository|repo|codebase)\b", combined) or any(
            token in combined for token in ("仓库", "代码库", "代码仓库")
        ):
            state.target_object = "repository"
        elif structured_data_language:
            state.target_object = "dataset"
        elif any(token in combined for token in ("web", "网页", "search", "research", "来源", "搜索")):
            state.target_object = "web_resource"
        elif any(token in combined for token in ("file", "文件", "path", "路径", "workspace", "目录")):
            state.target_object = "local_workspace"
        else:
            state.target_object = state.target_object or "generic_request"

        if any(token in combined for token in ("briefing", "brief", "简报")):
            state.deliverable_form = "briefing"
        elif any(token in combined for token in ("report", "报告", "说明")):
            state.deliverable_form = "report"
        elif any(token in combined for token in ("markdown", "文档", ".md", "artifact", "文件")):
            state.deliverable_form = "artifact_bundle"
        else:
            state.deliverable_form = state.deliverable_form or "generic_output"

        if any(token in combined for token in ("architecture report", "architectural", "architecture", "架构报告", "整体架构", "模块结构", "运行入口", "entrypoint", "entry point")):
            state.analysis_frame = "architecture"
        elif state.target_object == "dataset" and any(
            token in combined for token in ("compare", "comparison", "against", "versus", "对比", "比较", "across")
        ):
            state.analysis_frame = "summary"
        elif state.target_object == "dataset" and any(
            token in combined for token in ("validate", "validation", "schema", "missing required columns", "required columns", "校验", "验证", "缺失列", "必填列")
        ):
            state.analysis_frame = "audit"
        elif any(token in combined for token in ("audit", "risk", "hotspot", "审计", "风险", "热点")):
            state.analysis_frame = "audit"
        elif any(token in combined for token in ("whether this project is good", "is this project good", "评估", "evaluate", "assessment", "goodness", "是否好", "好不好")):
            state.analysis_frame = "assessment"
        elif any(token in combined for token in ("briefing", "summary", "summarize", "summarise", "digest", "概述", "总结", "说明", "简报", "摘要")):
            state.analysis_frame = "summary"
        else:
            state.analysis_frame = state.analysis_frame or "generic"

        if any(token in combined for token in ("whether this project is good", "is this project good", "评估", "evaluate", "assessment", "judge", "是否好", "好不好")):
            state.decision_posture = "evaluative"
        elif state.target_object == "dataset" and any(
            token in combined for token in ("compare", "comparison", "against", "versus", "对比", "比较", "按", "across")
        ):
            state.decision_posture = "comparative"
        elif any(token in combined for token in ("recommend", "suggest", "改进建议", "建议", "improve")):
            state.decision_posture = "recommendation"
        elif state.deliverable_form in {"report", "briefing"} or state.analysis_frame != "generic":
            state.decision_posture = "descriptive"
        else:
            state.decision_posture = state.decision_posture or "generic"

        if state.target_object == "repository" and state.deliverable_form == "report" and state.analysis_frame == "architecture":
            state.deliverable_contract = "architecture_report"
        elif (
            state.analysis_frame == "summary"
            and (
                (state.target_object == "web_resource" and state.deliverable_form in {"briefing", "report"})
                or (
                    state.target_object == "local_workspace"
                    and state.deliverable_form == "briefing"
                    and any(token in combined for token in ("briefing", "brief", "简报", "notes", "document", "docs", "manual"))
                )
            )
        ):
            state.deliverable_contract = "research_briefing"
        elif (
            state.target_object in {"dataset", "local_workspace"}
            and structured_data_language
            and state.deliverable_form == "report"
            and state.analysis_frame == "summary"
            and state.decision_posture == "comparative"
        ):
            state.deliverable_contract = "dataset_comparison_report"
        elif (
            state.target_object in {"dataset", "local_workspace"}
            and structured_data_language
            and state.deliverable_form == "report"
            and state.analysis_frame in {"audit", "assessment"}
        ):
            state.deliverable_contract = "dataset_validation_report"
        elif (
            state.target_object in {"dataset", "local_workspace"}
            and structured_data_language
            and state.deliverable_form == "report"
            and state.analysis_frame == "summary"
        ):
            state.deliverable_contract = "dataset_summary_report"
        elif (
            state.target_object in {"dataset", "local_workspace"}
            and structured_data_language
            and state.deliverable_form in {"report", "artifact_bundle", "dataset"}
            and state.analysis_frame in {"summary", "assessment", "audit"}
        ):
            state.deliverable_contract = "dataset_summary"
        elif state.deliverable_form == "artifact_bundle":
            state.deliverable_contract = "artifact_bundle"
        else:
            state.deliverable_contract = state.deliverable_contract if state.deliverable_contract != "generic_output" else "generic_output"

        if state.target_object == "repository" and state.analysis_frame in {"architecture", "audit", "assessment"}:
            state.primary_objective = "analyze"
        elif any(
            token in combined
            for token in ("analyze", "analyse", "inspect", "review", "examine", "分析", "检查", "审查", "梳理")
        ):
            state.primary_objective = "analyze"
        elif any(token in combined for token in ("collect", "search", "gather", "抓取", "收集", "搜索")):
            state.primary_objective = "collect"
        elif state.target_object == "dataset" and any(
            token in combined for token in ("aggregate", "group", "compare", "score", "统计", "聚合", "对比", "评分")
        ):
            state.primary_objective = "transform"
        elif state.target_object == "dataset" and any(
            token in combined for token in ("validate", "validation", "schema", "missing required columns", "required columns", "校验", "验证", "缺失列", "必填列")
        ):
            state.primary_objective = "validate"
        elif state.analysis_frame in {"assessment", "audit"}:
            state.primary_objective = "validate"
        elif any(token in combined for token in ("summary", "summarize", "brief", "报告", "说明", "总结")):
            state.primary_objective = "synthesize"
        else:
            state.primary_objective = state.primary_objective or "deliver"

        if state.target_object == "repository" and any(
            "/" in item
            or any(token in item.lower() for token in ("local", "workspace", "path", "directory"))
            or any(token in item for token in ("本地", "路径", "目录", "工作区"))
            for item in state.input_items
        ):
            state.source_modality = "workspace_repo"
        elif state.target_object == "repository" and any("github.com" in item or item.startswith("http") for item in state.input_items):
            state.source_modality = "remote_repo"
        elif state.target_object == "web_resource":
            state.source_modality = "web_source"
        elif state.target_object == "dataset":
            state.source_modality = "local_path"
        elif state.target_object == "local_workspace":
            state.source_modality = "local_path"
        else:
            state.source_modality = state.source_modality or "unknown"

        if state.human_review_required:
            state.workflow_shape = "review_gated_workflow"
        elif (
            state.deliverable_contract in {"architecture_report", "research_briefing"}
            or state.target_object == "dataset"
            or len(state.acceptance_criteria) > 1
        ):
            state.workflow_shape = "bounded_workflow"
        else:
            state.workflow_shape = state.workflow_shape or "single_service"

        evidence_basis: list[str] = []
        if state.target_object == "repository":
            if any(token in combined for token in ("module", "模块", "structure", "结构", "dependency", "依赖")) or state.analysis_frame in {"architecture", "assessment", "audit"}:
                evidence_basis.append("repo_structure")
            if any(token in combined for token in ("dependency", "依赖", "relationship", "关系")) or state.analysis_frame in {"architecture", "audit"}:
                evidence_basis.append("repo_relationships")
            if any(token in combined for token in ("runtime", "build", "entrypoint", "entry point", "运行", "构建", "入口", "config")) or state.analysis_frame in {"architecture", "audit"}:
                evidence_basis.append("repo_execution_surface")
        elif state.target_object == "web_resource":
            evidence_basis.append("web_sources")
        elif state.target_object == "dataset":
            evidence_basis.append("structured_data")
        elif state.target_object == "local_workspace":
            if structured_data_language:
                evidence_basis.append("structured_data")
            else:
                evidence_basis.append("local_documents")
        state.evidence_basis = self._dedupe(evidence_basis)

        if state.deliverable_contract == "architecture_report":
            state.evidence_requirements = ["repo.inventory", "repo.structure", "repo.evidence_bundle"]
        elif state.deliverable_contract == "research_briefing":
            state.evidence_requirements = ["research.sources", "research.brief.draft"]
        elif state.deliverable_contract == "artifact_bundle":
            state.evidence_requirements = ["artifact.saved"]
        elif state.deliverable_contract in {
            "dataset_summary",
            "dataset_summary_report",
            "dataset_validation_report",
            "dataset_comparison_report",
        }:
            state.evidence_requirements = []
        elif state.target_object == "repository" and state.evidence_basis:
            state.evidence_requirements = ["repo.inventory", "repo.structure"]
            if "repo_relationships" in state.evidence_basis:
                state.evidence_requirements.append("repo.relationships")
            if "repo_execution_surface" in state.evidence_basis:
                state.evidence_requirements.append("repo.execution_surface")
        elif state.target_object == "web_resource":
            state.evidence_requirements = ["research.sources"]
        else:
            state.evidence_requirements = []
        return state

    def _derive_domain_packs(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        hints: dict[str, Any],
    ):
        packs = semantic_state.domain_packs.model_copy(deep=True)
        combined = " ".join(
            part
            for part in [
                semantic_state.goal or "",
                "; ".join(semantic_state.input_items),
                "; ".join(semantic_state.output_items),
                "; ".join(semantic_state.acceptance_criteria),
                semantic_state.domain or "",
            ]
            if part
        ).lower()
        data_indicators = list(hints.get("data_files") or [])
        dataish = semantic_state.target_object == "dataset" or "structured_data" in set(semantic_state.evidence_basis or []) or bool(data_indicators)
        if dataish:
            pack = packs.data_pack.model_copy(deep=True) if packs.data_pack is not None else DataSemanticPack()
            if pack.data_source_kind is None:
                joined_sources = " ".join([*semantic_state.input_items, *data_indicators]).lower()
                if ".csv" in joined_sources or "csv" in combined:
                    pack.data_source_kind = "csv"
                elif any(ext in joined_sources for ext in (".xlsx", ".xls")) or any(token in combined for token in ("xlsx", "xls", "excel")):
                    pack.data_source_kind = "xlsx"
                elif any(token in combined for token in ("table", "tabular", "rows", "columns", "数据表", "表格")):
                    pack.data_source_kind = "table"
                else:
                    pack.data_source_kind = "mixed"
            if pack.aggregation_mode is None:
                if any(token in combined for token in ("group", "grouped", "breakdown", "分组", "拆分")):
                    pack.aggregation_mode = "group"
                elif any(token in combined for token in ("compare", "comparison", "对比", "比较")):
                    pack.aggregation_mode = "compare"
                elif any(token in combined for token in ("score", "评分", "rank", "ranking")):
                    pack.aggregation_mode = "score"
                else:
                    pack.aggregation_mode = "summarize"
            if pack.metric_focus is None:
                if any(token in combined for token in ("top", "revenue", "metric", "指标", "统计", "kpi")):
                    pack.metric_focus = "explicit"
                else:
                    pack.metric_focus = "inferred"
            if pack.output_granularity is None:
                if any(token in combined for token in ("executive", "summary", "摘要", "概览", "概述")):
                    pack.output_granularity = "executive_summary"
                elif any(token in combined for token in ("group", "breakdown", "分组", "区域")):
                    pack.output_granularity = "grouped"
                else:
                    pack.output_granularity = "row_level"
            if pack.validation_need is None:
                if any(token in combined for token in ("anomaly", "异常")):
                    pack.validation_need = "anomaly_check"
                elif any(token in combined for token in ("schema", "列", "字段格式")):
                    pack.validation_need = "schema"
                elif any(token in combined for token in ("consistency", "一致性", "核对")):
                    pack.validation_need = "consistency"
                else:
                    pack.validation_need = "none"
            packs.data_pack = pack
        return packs

    def _derive_pending_semantic_fields(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        authoring_feedback: dict[str, Any] | None,
    ) -> list[str]:
        pending: list[str] = []
        if not self._normalize_text(semantic_state.goal):
            pending.append("goal")
        if semantic_state.target_object == "generic_request" and not semantic_state.input_items:
            pending.append("target_object")
        if semantic_state.deliverable_form == "generic_output" and not semantic_state.output_items:
            pending.append("deliverable_form")
        if semantic_state.deliverable_form == "report" and semantic_state.analysis_frame == "generic":
            pending.append("analysis_frame")
        if not semantic_state.input_items:
            pending.append("source_locator")
        if semantic_state.target_object == "repository" and semantic_state.source_modality == "unknown":
            pending.append("source_modality")
        if not semantic_state.output_items:
            pending.append("delivery_target")
        if not semantic_state.acceptance_criteria:
            pending.append("acceptance_semantics")
        if semantic_state.deliverable_form in {"report", "briefing"} and not semantic_state.evidence_basis:
            pending.append("evidence_basis")
        if semantic_state.deliverable_contract in {"architecture_report", "research_briefing"} and not semantic_state.evidence_requirements:
            pending.append("evidence_requirements")
        if semantic_state.deliverable_contract in {"architecture_report", "research_briefing"} and semantic_state.workflow_shape == "single_service":
            pending.append("workflow_shape")
        return self._dedupe([item for item in pending if item])

    def _derive_authoring_ambiguities(
        self,
        semantic_state: RequirementAuthoringSemanticStateResponse,
    ) -> list[str]:
        ambiguities: list[str] = []
        if semantic_state.target_object == "generic_request" and not semantic_state.input_items:
            ambiguities.append("target_object")
        if semantic_state.deliverable_form == "generic_output" and not semantic_state.output_items:
            ambiguities.append("deliverable_form")
        if semantic_state.deliverable_form == "report" and semantic_state.analysis_frame == "generic":
            ambiguities.append("analysis_frame")
        if semantic_state.target_object == "repository" and semantic_state.source_modality == "unknown":
            ambiguities.append("source_modality")
        if not semantic_state.input_items:
            ambiguities.append("source_locator")
        if not semantic_state.output_items:
            ambiguities.append("delivery_target")
        if not semantic_state.acceptance_criteria:
            ambiguities.append("acceptance_semantics")
        if semantic_state.deliverable_form in {"report", "briefing"} and not semantic_state.evidence_basis:
            ambiguities.append("evidence_basis")
        if semantic_state.deliverable_contract in {"architecture_report", "research_briefing"} and not semantic_state.evidence_requirements:
            ambiguities.append("evidence_requirements")
        if semantic_state.deliverable_contract in {"architecture_report", "research_briefing"} and semantic_state.workflow_shape == "single_service":
            ambiguities.append("workflow_shape")
        return self._dedupe(ambiguities)

    def _authoring_semantic_confidence(
        self,
        semantic_state: RequirementAuthoringSemanticStateResponse,
    ) -> float:
        score = 0.25
        if self._normalize_text(semantic_state.goal):
            score += 0.15
        if semantic_state.input_items:
            score += 0.15
        if semantic_state.output_items:
            score += 0.15
        if semantic_state.acceptance_criteria:
            score += 0.15
        if semantic_state.target_object and semantic_state.target_object != "generic_request":
            score += 0.1
        if semantic_state.deliverable_form and semantic_state.deliverable_form != "generic_output":
            score += 0.05
        if semantic_state.analysis_frame and semantic_state.analysis_frame != "generic":
            score += 0.05
        if semantic_state.deliverable_contract and semantic_state.deliverable_contract != "generic_output":
            score += 0.1
        if semantic_state.workflow_shape and semantic_state.workflow_shape != "single_service":
            score += 0.05
        if semantic_state.ambiguities:
            score -= min(0.25, 0.04 * len(semantic_state.ambiguities))
        return int(round(max(0.0, min(1.0, score)) * 100))

    def _project_requirement_draft_from_semantic_state(
        self,
        *,
        draft: dict[str, Any],
        semantic_state: RequirementAuthoringSemanticStateResponse,
        session_id: str,
    ) -> dict[str, Any]:
        projected = dict(draft)
        goal = self._normalize_text(semantic_state.goal)
        capture_for_summary = {
            "goal": goal,
            "delivery_form": semantic_state.delivery_form,
            "target_users": list(semantic_state.target_users),
            "p0_features": list(semantic_state.p0_features),
        }
        projected["goal"] = goal
        projected["summary"] = self._build_summary(capture_for_summary)
        projected["title"] = self._derive_requirement_title(goal, projected["summary"], capture_for_summary)
        projected["target_users"] = ", ".join(semantic_state.target_users) if semantic_state.target_users else None
        projected["input_description"] = "; ".join(semantic_state.input_items) if semantic_state.input_items else None
        projected["output_description"] = "; ".join(semantic_state.output_items) if semantic_state.output_items else None
        projected["domain"] = self._normalize_text(semantic_state.domain)
        projected["interactivity_level"] = normalize_interactivity_level(semantic_state.interactivity_level)
        projected["human_review_required"] = bool(semantic_state.human_review_required)
        projected["recurrence_mode"] = normalize_recurrence_mode(semantic_state.recurrence_mode)
        projected["constraints_json"] = self._constraints_payload_from_items(list(semantic_state.constraints))
        projected["acceptance_criteria_json"] = self._acceptance_payload_from_items(list(semantic_state.acceptance_criteria))
        projected["external_tools_needed_json"] = normalize_external_tools_payload(list(semantic_state.external_tools))
        projected.setdefault("structured_spec_json", {})
        projected["structured_spec_json"].update(
            {
                "source": "requirement_session",
                "session_id": session_id,
                "delivery_form": self._normalize_text(semantic_state.delivery_form),
                "p0_features": list(semantic_state.p0_features),
                "p1_features": list(semantic_state.p1_features),
                "deployment": self._normalize_text(semantic_state.deployment),
                "target_users_list": list(semantic_state.target_users),
                "acceptance_criteria_list": list(semantic_state.acceptance_criteria),
                "conversation_summary": projected["summary"] or "",
                "authoring_semantic_state": semantic_state.model_dump(mode="python"),
                "draft_projection_meta": self._build_draft_projection_meta(semantic_state),
            }
        )
        return projected

    def _build_draft_projection_meta(
        self,
        semantic_state: RequirementAuthoringSemanticStateResponse,
    ) -> dict[str, Any]:
        return {
            "projection_source": "authoring_semantic_state",
            "semantic_confidence": semantic_state.confidence,
            "pending_semantic_fields": list(semantic_state.pending_semantic_fields),
            "field_provenance": {
                key: value.model_dump(mode="python")
                for key, value in (semantic_state.field_provenance or {}).items()
            },
        }

    def _apply_llm_patch_to_semantic_state(
        self,
        *,
        semantic_state: RequirementAuthoringSemanticStateResponse,
        patch: RequirementDraftPatchResponse,
        conversation: list[dict[str, str]],
    ) -> RequirementAuthoringSemanticStateResponse:
        state = semantic_state.model_copy(deep=True)
        user_turn_index = max(sum(1 for item in conversation if item.get("role") == "user") - 1, 0)
        provenance = dict(state.field_provenance)
        field_patches = patch.field_patches.model_dump(mode="python", exclude_none=True)
        mapping = {
            "goal": ("goal", "scalar"),
            "target_users": ("target_users", "list_from_text"),
            "input_description": ("input_items", "list_from_text"),
            "output_description": ("output_items", "list_from_text"),
            "domain": ("domain", "scalar"),
            "interactivity_level": ("interactivity_level", "scalar"),
            "recurrence_mode": ("recurrence_mode", "scalar"),
            "human_review_required": ("human_review_required", "bool"),
            "acceptance_criteria_json": ("acceptance_criteria", "acceptance_payload"),
            "constraints_json": ("constraints", "constraints_payload"),
            "external_tools_needed_json": ("external_tools", "external_tools_payload"),
        }
        for field_name, value in field_patches.items():
            mapped = mapping.get(field_name)
            if mapped is None:
                continue
            target_field, kind = mapped
            normalized_value: Any = None
            if kind == "scalar":
                normalized_value = self._normalize_text(value)
            elif kind == "bool":
                normalized_value = bool(value)
            elif kind == "list_from_text":
                normalized_value = self._split_text_list(value)
            elif kind == "acceptance_payload":
                items = normalize_acceptance_criteria_payload(value).items
                normalized_value = [item.description for item in items if item.description]
            elif kind == "constraints_payload":
                items = normalize_constraints_payload(value).items
                normalized_value = [item.rule for item in items if item.rule]
            elif kind == "external_tools_payload":
                normalized_value = normalize_external_tools_payload(value).get("items", [])
            if normalized_value in (None, "", [], {}):
                continue
            setattr(state, target_field, normalized_value)
            provenance[target_field] = RequirementSemanticFieldProvenanceResponse(
                source_turn_index=user_turn_index,
                source_role="assistant",
                derivation_method="llm_patch_projection",
                confidence=max(0.7, float(patch.confidence or 0.0)),
            )
        state.field_provenance = provenance
        state = self._infer_authoring_semantic_shape(state)
        state.pending_semantic_fields = self._derive_pending_semantic_fields(
            semantic_state=state,
            authoring_feedback=None,
        )
        state.ambiguities = self._derive_authoring_ambiguities(state)
        state.confidence = max(state.confidence, int(round(max(0.0, min(1.0, float(patch.confidence or 0.0))) * 100)))
        return state

    def _build_empty_capture(self) -> dict[str, Any]:
        return {
            "goal": None,
            "target_users": [],
            "delivery_form": None,
            "p0_features": [],
            "p1_features": [],
            "constraints": [],
            "input_items": [],
            "output_items": [],
            "deployment": None,
            "acceptance_criteria": [],
            "domain": None,
            "interactivity_level": None,
            "recurrence_mode": None,
            "external_tools": [],
            "human_review_required": False,
        }

    def _apply_explicit_fields(self, result: dict[str, Any], message: str) -> None:
        normalized_message = re.sub(r"(?<=\.)\s+(?=[A-Za-z][A-Za-z ]+[:：])", "\n", message)
        lines = normalized_message.splitlines()
        current_field = None

        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue

            matched_field = None
            for field_name, aliases in self.FIELD_ALIASES.items():
                for alias in aliases:
                    pattern = rf"^{re.escape(alias)}\s*[:：-]\s*(.*)$"
                    match = re.match(pattern, line, flags=re.IGNORECASE)
                    if match:
                        matched_field = field_name
                        current_field = field_name
                        self._assign_field(result, field_name, match.group(1).strip())
                        break
                if matched_field:
                    break

            if matched_field:
                continue

            if current_field and raw_line.lstrip().startswith(("-", "*")):
                self._assign_field(result, current_field, line)
            elif current_field and current_field in self.LIST_FIELDS and not re.match(r"^[A-Za-z][A-Za-z ]+[:：-]", line):
                self._assign_field(result, current_field, line)

    def _apply_heuristics(self, result: dict[str, Any], message: str) -> None:
        content = message.strip()
        lower = content.lower()

        if not result["goal"]:
            first_sentence = re.split(r"[.!?。\n]", content)[0].strip()
            if len(first_sentence) > 10:
                result["goal"] = first_sentence

        if not result["delivery_form"]:
            if "desktop" in lower:
                result["delivery_form"] = "desktop application"
            elif "web" in lower:
                result["delivery_form"] = "web application"
            elif "api" in lower:
                result["delivery_form"] = "api service"
            elif "mobile" in lower:
                result["delivery_form"] = "mobile application"

        if not result["deployment"]:
            if "docker" in lower:
                result["deployment"] = "dockerized deployment"
            elif "cloud" in lower:
                result["deployment"] = "cloud deployment"
            elif "local" in lower or "on-prem" in lower:
                result["deployment"] = "local or on-prem deployment"

        if not result["recurrence_mode"]:
            recurrence_match = re.search(r"\b(daily|weekly|scheduled|recurring|once)\b", lower)
            if recurrence_match:
                result["recurrence_mode"] = recurrence_match.group(1)

        if not result["domain"]:
            for token, domain in (
                ("research", "research"),
                ("digest", "research"),
                ("finance", "finance"),
                ("sales", "sales"),
                ("support", "support"),
                ("data", "data"),
                ("etl", "data"),
                ("marketing", "marketing"),
            ):
                if token in lower:
                    result["domain"] = domain
                    break

        if not result["interactivity_level"]:
            if any(token in lower for token in ("multi-agent", "multi agent", "handoff", "approval", "review")):
                result["interactivity_level"] = "collaborative"
            elif "workflow" in lower or "pipeline" in lower:
                result["interactivity_level"] = "high"

        if re.search(r"\b(review|approve|approval|manual)\b", lower):
            result["human_review_required"] = True

        if not result["target_users"]:
            users_match = re.search(r"for\s+([a-z0-9 ,/&-]+)", lower)
            if users_match:
                self._assign_field(result, "target_users", users_match.group(1))

        include_match = re.search(r"(include|need|must have|features?)[:\s]+(.+)", lower)
        if include_match:
            self._assign_field(result, "p0_features", include_match.group(2))

        for token in ("rss", "search", "slack", "notion", "jira", "email", "web", "api"):
            if token in lower:
                self._assign_field(result, "external_tools", token)

        if any(keyword in lower for keyword in ["performance", "latency", "fast", "reliable", "secure", "security", "audit"]):
            qualities = []
            if any(keyword in lower for keyword in ["performance", "latency", "fast"]):
                qualities.append("performance")
            if any(keyword in lower for keyword in ["reliable", "availability"]):
                qualities.append("reliability")
            if any(keyword in lower for keyword in ["secure", "security", "audit"]):
                qualities.append("security and auditability")
            for item in qualities:
                self._assign_field(result, "acceptance_criteria", item)

    def _apply_conversation_slot_patterns(self, result: dict[str, Any], message: str) -> None:
        content = (message or "").strip()
        lower = content.lower()

        direct_hits = {
            "input_items": False,
            "output_items": False,
            "acceptance_criteria": False,
        }
        direct_patterns = (
            (r"(?:最终)?输入(?:就是|是|为)\s*([^\n]+)", "input_items"),
            (r"(?:最终)?输出(?:就是|是|为)\s*([^\n]+)", "output_items"),
            (r"(?:最后)?交付\s*([^\n]+)", "output_items"),
            (r"(?:最终)?验收标准(?:就是|是|为)\s*([^\n]+)", "acceptance_criteria"),
            (r"(?:最终)?成功标准(?:就是|是|为)\s*([^\n]+)", "acceptance_criteria"),
            (r"(?:最终)?合格(?:的)?标准(?:就是|是|为)\s*([^\n]+)", "acceptance_criteria"),
            (r"(?:最终)?判断标准(?:就是|是|为)\s*([^\n]+)", "acceptance_criteria"),
        )
        for pattern, field_name in direct_patterns:
            for match in re.finditer(pattern, content, flags=re.IGNORECASE):
                self._assign_field(result, field_name, self._trim_direct_field_value(match.group(1).strip()))
                direct_hits[field_name] = True

        if not direct_hits["input_items"]:
            for value in self._extract_segment(
                content,
                lower,
                anchors=[
                    "inputs are",
                    "input is",
                    "inputs include",
                    "input includes",
                    "输入是",
                    "输入为",
                    "输入包括",
                    "最终输入是",
                    "最终输入为",
                    "需要的输入是",
                ],
                terminators=["outputs", "output", "输出", "constraints", "限制", "quality", "acceptance", "success criteria", "成功标准", "domain", "recurrence", "tools", "每周", "每天", "需要使用"],
            ):
                self._assign_field(result, "input_items", value)

        if not direct_hits["output_items"]:
            for value in self._extract_segment(
                content,
                lower,
                anchors=[
                    "outputs are",
                    "output is",
                    "outputs include",
                    "output includes",
                    "deliverable is",
                    "delivery is",
                    "输出是",
                    "输出为",
                    "输出包括",
                    "最终输出是",
                    "最终输出为",
                    "需要的输出是",
                    "最后交付",
                ],
                terminators=["constraints", "限制", "quality", "acceptance", "success criteria", "成功标准", "domain", "recurrence", "tools", "每周", "每天", "需要使用"],
            ):
                self._assign_field(result, "output_items", value)

        for value in self._extract_segment(
            content,
            lower,
            anchors=["target users are", "users are", "for ", "给", "面向"],
            terminators=["using", "使用", "who need", "需要", "inputs", "outputs", "constraints", "quality"],
        ):
            if len(value) >= 2:
                self._assign_field(result, "target_users", value)
        chinese_users_match = re.search(r"(?:给|面向)\s*([^。.!?\n]+?)(?:使用|提供|服务|$)", content)
        if chinese_users_match:
            self._assign_field(result, "target_users", chinese_users_match.group(1).strip())

        if not direct_hits["acceptance_criteria"]:
            for value in self._extract_segment(
                content,
                lower,
                anchors=[
                    "acceptance criteria are",
                    "success criteria are",
                    "quality requirements are",
                    "质量要求是",
                    "成功标准是",
                    "合格标准是",
                    "合格的标准是",
                    "判断标准是",
                    "要求是",
                ],
                terminators=["constraints", "限制", "domain", "recurrence", "tools"],
            ):
                self._assign_field(result, "acceptance_criteria", value)

        if any(token in lower for token in (" should ", " must ", "需要能够", "应该", "必须")) and not result["acceptance_criteria"]:
            inferred = self._infer_expectation_items(content)
            for item in inferred:
                self._assign_field(result, "acceptance_criteria", item)
        if not direct_hits["acceptance_criteria"]:
            requirements_match = re.search(r"要求\s*([^\n。！？]+)", content)
            if requirements_match:
                self._assign_field(result, "acceptance_criteria", requirements_match.group(1).strip())

        if any(token in lower for token in ("must not", "cannot", "can't", "不能", "不要", "限制")):
            inferred_constraints = self._infer_constraint_items(content)
            for item in inferred_constraints:
                self._assign_field(result, "constraints", item)

        if not result["recurrence_mode"]:
            recurrence_map = {
                "每天": "daily",
                "每日": "daily",
                "每周": "weekly",
                "每月": "monthly",
                "daily": "daily",
                "weekly": "weekly",
                "monthly": "monthly",
                "one-off": "once",
            }
            for token, normalized in recurrence_map.items():
                if token in lower or token in content:
                    result["recurrence_mode"] = normalized
                    break

    def _assign_field(self, result: dict[str, Any], field_name: str, raw_value: str) -> None:
        value = raw_value.strip().strip("-").strip()
        if not value:
            return
        if field_name in self.LIST_FIELDS:
            result[field_name].extend(self._split_items(value))
            return
        if field_name in self.BOOLEAN_FIELDS:
            lowered = value.lower()
            if lowered in {"yes", "true", "required"}:
                result[field_name] = True
            elif lowered in {"no", "false", "not required"}:
                result[field_name] = False
            return
        if result[field_name] is None or result[field_name] == "":
            result[field_name] = value

    def _split_items(self, raw_value: str) -> list[str]:
        return [item.strip(" -") for item in re.split(r"[,，;；\n、]+", raw_value) if item.strip(" -")]

    def _trim_direct_field_value(self, value: str) -> str:
        candidate = (value or "").strip()
        if not candidate:
            return candidate
        sentence_boundaries = [
            candidate.find(token)
            for token in ("\n", "。", "！", "？")
            if candidate.find(token) != -1
        ]
        if sentence_boundaries:
            candidate = candidate[: min(sentence_boundaries)].strip()
        candidate = re.split(
            r"\.\s*(?=(?:final\s+)?(?:input|output|acceptance criteria|success criteria|constraints|domain|tools)\b|(?:最终)?(?:输入|输出|验收标准|成功标准|合格(?:的)?标准|判断标准|业务领域|限制))",
            candidate,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        candidate = re.split(
            r"[，,]\s*(?=要求|acceptance criteria|success criteria|constraints|限制)",
            candidate,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        return candidate

    def _extract_segment(
        self,
        content: str,
        lower_content: str,
        *,
        anchors: list[str],
        terminators: list[str],
    ) -> list[str]:
        extracted: list[str] = []
        lower_terminators = [item.lower() for item in terminators]
        for anchor in anchors:
            lower_anchor = anchor.lower()
            pattern = re.compile(rf"(^|[。.!?\n])\s*{re.escape(lower_anchor)}", flags=re.IGNORECASE | re.MULTILINE)
            for match in pattern.finditer(lower_content):
                segment_start = match.end()
                segment = content[segment_start:]
                lower_segment = lower_content[segment_start:]
                end = len(segment)
                for terminator in lower_terminators:
                    terminator_index = lower_segment.find(terminator)
                    if terminator_index != -1:
                        end = min(end, terminator_index)
                for punctuation in ("\n", "。", "！", "？"):
                    punctuation_index = segment.find(punctuation)
                    if punctuation_index != -1:
                        end = min(end, punctuation_index)
                candidate = segment[:end].strip(" :,-，。")
                if candidate:
                    extracted.extend(self._split_items(candidate))
        return extracted

    def _infer_expectation_items(self, content: str) -> list[str]:
        candidates = re.split(r"[。.!?\n]", content)
        items: list[str] = []
        for candidate in candidates:
            normalized = candidate.strip(" -")
            lower = normalized.lower()
            if not normalized:
                continue
            if any(token in lower for token in ("should", "must", "需要", "应该", "必须", "要求")):
                items.append(normalized)
        return items

    def _message_explicitly_sets_goal(self, message: str) -> bool:
        compact = " ".join((message or "").split())
        if not compact:
            return False
        patterns = [
            r"(^|\n)\s*goal\s*[:：-]",
            r"(^|\n)\s*目标\s*[:：-]",
            r"(^|\n)\s*goal\s+is\b",
            r"(^|\n)\s*目标是",
            r"(^|\n)\s*需要做的是",
        ]
        return any(re.search(pattern, compact, flags=re.IGNORECASE) for pattern in patterns)

    def _infer_constraint_items(self, content: str) -> list[str]:
        candidates = re.split(r"[。.!?\n]", content)
        items: list[str] = []
        for candidate in candidates:
            normalized = candidate.strip(" -")
            lower = normalized.lower()
            if not normalized:
                continue
            if any(token in lower for token in ("must not", "cannot", "can't", "不能", "不要", "限制")):
                items.append(normalized)
        return items

    def _build_summary(self, capture: dict[str, Any]) -> str | None:
        parts = []
        if capture.get("goal"):
            parts.append(f"Goal: {capture['goal']}")
        if capture.get("delivery_form"):
            parts.append(f"Delivery: {capture['delivery_form']}")
        if capture.get("target_users"):
            parts.append(f"Users: {', '.join(capture['target_users'][:3])}")
        if capture.get("p0_features"):
            parts.append(f"P0: {', '.join(capture['p0_features'][:4])}")
        return " | ".join(parts) or None

    def _dedupe(self, items: list[str]) -> list[str]:
        seen = set()
        normalized = []
        for item in items:
            key = item.strip().lower()
            if key and key not in seen:
                seen.add(key)
                normalized.append(item.strip())
        return normalized

    def _derive_requirement_title(self, goal: str | None, summary: str | None, capture: dict[str, Any]) -> str:
        candidates = [goal, summary]
        for candidate in candidates:
            if not candidate:
                continue
            normalized = candidate.replace("Goal:", "").split("|")[0].strip()
            if normalized:
                return normalized[:255]
        if capture["p0_features"]:
            return f"{capture['p0_features'][0][:200]} Draft"
        return "Requirement Session Draft"

    def _constraints_payload_from_items(self, items: list[str]) -> dict[str, Any]:
        payload = empty_constraints_payload().model_dump(mode="python")
        payload["items"] = [
            {
                "id": f"constraint_{index + 1}",
                "type": "policy",
                "level": "soft",
                "rule": item,
                "target": None,
                "value": None,
            }
            for index, item in enumerate(items)
            if item
        ]
        return payload

    def _acceptance_payload_from_items(self, items: list[str]) -> dict[str, Any]:
        payload = empty_acceptance_criteria_payload().model_dump(mode="python")
        payload["items"] = [
            {
                "id": f"criterion_{index + 1}",
                "type": "quality",
                "level": "hard",
                "description": item,
                "measurement": None,
                "threshold": None,
            }
            for index, item in enumerate(items)
            if item
        ]
        return payload

    def _normalize_text(self, value: Any) -> str | None:
        return normalize_optional_text(value)

    def _split_text_list(self, value: Any) -> list[str]:
        text = self._normalize_text(value)
        if text is None:
            return []
        return [item.strip() for item in text.split(";") if item.strip()]

    def _merge_with_previous_draft(self, draft: dict[str, Any], previous_draft: dict[str, Any]) -> dict[str, Any]:
        merged = {**draft, **(previous_draft or {})}
        merged["constraints_json"] = previous_draft.get("constraints_json") or draft["constraints_json"]
        merged["acceptance_criteria_json"] = previous_draft.get("acceptance_criteria_json") or draft["acceptance_criteria_json"]
        merged["external_tools_needed_json"] = previous_draft.get("external_tools_needed_json") or draft["external_tools_needed_json"]
        merged["raw_conversation_json"] = draft["raw_conversation_json"]
        merged["structured_spec_json"] = {
            **draft["structured_spec_json"],
            **(previous_draft.get("structured_spec_json") or {}),
        }
        return merged

    def _should_attempt_llm_draft_patch(
        self,
        *,
        previous_draft: dict[str, Any] | None,
        deterministic_draft: dict[str, Any],
        latest_message: str,
    ) -> bool:
        if previous_draft is None:
            return self._draft_has_core_gaps(deterministic_draft)
        if not self._draft_has_meaningful_updates(previous_draft, deterministic_draft):
            return True
        if self._draft_has_core_gaps(deterministic_draft):
            return True
        return not self._message_supports_deterministic_only(latest_message)

    def _draft_has_core_gaps(self, draft: dict[str, Any]) -> bool:
        acceptance_items = ((draft.get("acceptance_criteria_json") or {}).get("items") or [])
        return any(
            not self._normalize_text(draft.get(field_name))
            for field_name in ("goal", "input_description", "output_description")
        ) or not acceptance_items

    def _draft_has_meaningful_updates(self, previous_draft: dict[str, Any], current_draft: dict[str, Any]) -> bool:
        scalar_fields = (
            "goal",
            "target_users",
            "input_description",
            "output_description",
            "domain",
            "interactivity_level",
            "recurrence_mode",
        )
        for field_name in scalar_fields:
            if self._normalize_text(previous_draft.get(field_name)) != self._normalize_text(current_draft.get(field_name)):
                return True
        if bool(previous_draft.get("human_review_required")) != bool(current_draft.get("human_review_required")):
            return True
        if (previous_draft.get("constraints_json") or {}) != (current_draft.get("constraints_json") or {}):
            return True
        if (previous_draft.get("acceptance_criteria_json") or {}) != (current_draft.get("acceptance_criteria_json") or {}):
            return True
        if (previous_draft.get("external_tools_needed_json") or {}) != (current_draft.get("external_tools_needed_json") or {}):
            return True
        previous_spec = previous_draft.get("structured_spec_json") or {}
        current_spec = current_draft.get("structured_spec_json") or {}
        for field_name in ("delivery_form", "deployment"):
            if self._normalize_text(previous_spec.get(field_name)) != self._normalize_text(current_spec.get(field_name)):
                return True
        for field_name in ("p0_features", "p1_features"):
            if list(previous_spec.get(field_name) or []) != list(current_spec.get(field_name) or []):
                return True
        return False

    def _message_supports_deterministic_only(self, message: str) -> bool:
        compact = " ".join((message or "").split())
        if not compact:
            return False
        patterns = [
            r"(^|\n)\s*(goal|target users|delivery form|inputs?|outputs?|constraints|quality requirements|acceptance criteria|success criteria|domain)\s*[:：-]",
            r"(^|\n)\s*(目标|目标用户|交付形式|输入|输出|限制|质量要求|验收标准|成功标准|业务领域)\s*[:：-]",
            r"(?:最终)?输入(?:就是|是|为)",
            r"(?:最终)?输出(?:就是|是|为)",
            r"(?:最终)?验收标准(?:就是|是|为)",
            r"(?:最终)?成功标准(?:就是|是|为)",
            r"(?:最终)?合格(?:的)?标准(?:就是|是|为)",
            r"(?:最终)?判断标准(?:就是|是|为)",
        ]
        return any(re.search(pattern, compact, flags=re.IGNORECASE) for pattern in patterns)

    def _draft_summary_text(self, requirement_draft: dict[str, Any]) -> str:
        summary = self._normalize_text(requirement_draft.get("summary"))
        if summary:
            return summary
        goal = self._normalize_text(requirement_draft.get("goal"))
        if goal:
            return f"Goal: {goal}"
        return "I have started capturing the requirement draft."

    def _default_patch_state(self, *, no_op_reason: str | None = None) -> RequirementDraftPatchStateResponse:
        return RequirementDraftPatchStateResponse(
            no_op_reason=no_op_reason,
            capability_gap_summary=self.capability_registry.summarize_capability_gaps([]),
        )

    def _authoring_semantic_refinement_prompt_contract(self) -> LLMPromptContract:
        return LLMPromptContract(
            contract_id="session.authoring_semantic_refinement.v5_9_6_1",
            purpose="Refine the persistent authoring semantic state from conversation into a stable SemanticCore-backed requirement understanding.",
            allowed_inputs=[
                "Previous authoring semantic state.",
                "Recent user-assistant conversation turns.",
                "Low-level deterministic hints such as paths, URLs, output formats, and review markers.",
                "Authoring feedback preview with current open semantic uncertainties.",
            ],
            forbidden_behavior=[
                "Do not output a draft JSON patch or a full requirement draft.",
                "Do not invent runtime graphs, build plans, or execution structure.",
                "Do not leave the SemanticCore fields in free-form text.",
                "Do not erase prior correct semantics unless the conversation clearly contradicts them.",
            ],
            output_schema=RequirementAuthoringSemanticStateResponse.model_json_schema(),
            uncertainty_handling=[
                'Preserve ambiguity explicitly in "ambiguities" instead of guessing.',
                'Use lower confidence when the conversation is incomplete or conflicting.',
                'Keep graph-critical unknowns visible in "pending_semantic_fields".',
            ],
        )

    def _draft_patch_prompt_contract(self) -> LLMPromptContract:
        return LLMPromptContract(
            contract_id="session.requirement_draft_patch.v4_1",
            purpose="Propose only a partial, schema-valid patch to the current requirement draft based on recent conversation.",
            allowed_inputs=[
                "Current requirement draft JSON.",
                "Recent conversation transcript.",
                "Current authoring feedback and clarification context.",
                "Allowed draft field vocabulary and registered capability vocabulary.",
            ],
            forbidden_behavior=[
                "Do not return a full replacement draft.",
                "Do not choose blueprint family, template, or runtime plan.",
                "Do not invent capabilities outside the registered vocabulary.",
                "Do not return prose, markdown, or any content outside one JSON object.",
            ],
            output_schema=RequirementDraftPatchResponse.model_json_schema(),
            uncertainty_handling=[
                'If information is ambiguous, keep the patch partial and explain ambiguity in "ambiguities".',
                'If nothing should be applied, leave field_patches empty and set "no_op_reason".',
                'If a tool or capability is mentioned but not in the registry, put it in "unsupported_capability_mentions".',
            ],
        )

    def _propose_and_apply_llm_draft_patch(
        self,
        *,
        current_draft: dict[str, Any],
        candidate_draft: dict[str, Any],
        conversation: list[dict[str, str]],
        authoring_feedback: dict[str, Any] | None,
        llm_client,
    ) -> tuple[RequirementDraftPatchResponse | None, RequirementDraftPatchStateResponse]:
        patch = self._propose_llm_draft_patch(
            current_draft=current_draft,
            conversation=conversation,
            authoring_feedback=authoring_feedback,
            llm_client=llm_client,
        )
        if patch is None:
            return None, self._default_patch_state(no_op_reason="llm_patch_unavailable_or_invalid")
        return patch, self._apply_llm_draft_patch(candidate_draft, patch)

    def _propose_llm_draft_patch(
        self,
        *,
        current_draft: dict[str, Any],
        conversation: list[dict[str, str]],
        authoring_feedback: dict[str, Any] | None,
        llm_client,
    ) -> RequirementDraftPatchResponse | None:
        capability_snapshot = self.capability_registry.snapshot(context="authoring", include_disabled=True)
        compact_capabilities = [
            {
                "capability_id": item.capability_id,
                "supported_step_kinds": item.supported_step_kinds,
                "enabled": item.enabled,
                "tags": item.tags,
            }
            for item in capability_snapshot.capabilities
        ]
        prompt_input = {
            "current_requirement_draft": current_draft,
            "recent_conversation": conversation[-8:],
            "authoring_feedback": {
                "completion_state_preview": (authoring_feedback or {}).get("completion_state_preview"),
                "gaps": (authoring_feedback or {}).get("gaps", []),
                "open_questions": (authoring_feedback or {}).get("open_questions", []),
                "clarification_items": (authoring_feedback or {}).get("clarification_items", []),
                "warnings": (authoring_feedback or {}).get("warnings", []),
            },
            "allowed_patch_fields": [
                "title",
                "summary",
                "goal",
                "target_users",
                "input_description",
                "output_description",
                "domain",
                "interactivity_level",
                "human_review_required",
                "recurrence_mode",
                "constraints_json",
                "acceptance_criteria_json",
                "external_tools_needed_json",
                "structured_spec_json.delivery_form",
                "structured_spec_json.p0_features",
                "structured_spec_json.p1_features",
                "structured_spec_json.deployment",
            ],
            "registered_authoring_capabilities": compact_capabilities,
        }
        system_prompt = (
            "You are a constrained requirement-draft patch assistant. "
            "Propose only partial updates to the existing requirement draft. "
            "Never emit a full draft replacement. "
            "Leave fields untouched unless the recent conversation materially updates them."
        )
        model, _, _, _, _ = llm_client.request_structured(
            stage="session_draft_patch",
            schema_model=RequirementDraftPatchResponse,
            system_prompt=system_prompt,
            prompt_contract=self._draft_patch_prompt_contract(),
            prompt_input=prompt_input,
        )
        return model if isinstance(model, RequirementDraftPatchResponse) else None

    def _apply_llm_draft_patch(
        self,
        draft: dict[str, Any],
        patch: RequirementDraftPatchResponse,
    ) -> RequirementDraftPatchStateResponse:
        applied_fields: list[str] = []
        rejected_fields: list[str] = []
        confidence = max(0.0, min(1.0, float(patch.confidence or 0.0)))
        ambiguities = list(patch.ambiguities or [])
        unsupported_capabilities = self._normalize_unsupported_capabilities(patch)
        capability_mentions = [
            capability_id
            for capability_id in (patch.capability_mentions or [])
            if self.capability_registry.get_capability(capability_id) is not None
        ]
        block_overwrite = confidence < 0.7 or bool(ambiguities)

        field_patches = patch.field_patches.model_dump(mode="python", exclude_none=True)
        for field_name, value in field_patches.items():
            if field_name == "structured_spec_json":
                structured_spec = draft.setdefault("structured_spec_json", {})
                for nested_field, nested_value in (value or {}).items():
                    normalized_value = self._normalize_patch_value(f"structured_spec_json.{nested_field}", nested_value)
                    current_value = structured_spec.get(nested_field)
                    if self._should_apply_patch_value(
                        current_value=current_value,
                        new_value=normalized_value,
                        block_overwrite=block_overwrite,
                    ):
                        structured_spec[nested_field] = normalized_value
                        applied_fields.append(f"structured_spec_json.{nested_field}")
                    else:
                        rejected_fields.append(f"structured_spec_json.{nested_field}")
                continue

            normalized_value = self._normalize_patch_value(field_name, value)
            current_value = draft.get(field_name)
            if self._should_apply_patch_value(
                current_value=current_value,
                new_value=normalized_value,
                block_overwrite=block_overwrite,
            ):
                draft[field_name] = normalized_value
                applied_fields.append(field_name)
            else:
                rejected_fields.append(field_name)

        for field_name in patch.cleared_fields or []:
            if field_name not in {"summary"}:
                rejected_fields.append(field_name)
                continue
            if confidence < 0.9:
                rejected_fields.append(field_name)
                continue
            draft[field_name] = None
            applied_fields.append(field_name)

        capability_gaps = self._build_authoring_capability_gaps(
            unsupported_capabilities=unsupported_capabilities,
            capability_mentions=capability_mentions,
        )

        return RequirementDraftPatchStateResponse(
            applied=bool(applied_fields),
            confidence=confidence,
            applied_fields=applied_fields,
            rejected_fields=self._dedupe(rejected_fields),
            ambiguities=ambiguities,
            unresolved_points=list(patch.unresolved_points or []),
            capability_mentions=self._dedupe(capability_mentions),
            unsupported_capability_mentions=unsupported_capabilities,
            capability_gaps=capability_gaps,
            capability_gap_summary=self.capability_registry.summarize_capability_gaps(capability_gaps),
            notes=list(patch.notes or []),
            no_op_reason=patch.no_op_reason if not applied_fields else None,
        )

    def _build_authoring_capability_gaps(
        self,
        *,
        unsupported_capabilities: list[str],
        capability_mentions: list[str],
    ) -> list:
        gaps = [
            self.capability_registry.build_gap_response(
                gap_kind="unsupported_mention",
                stage="authoring",
                reason="Capability mention is not present in the registered authoring/runtime vocabulary.",
                requested_capability_id_or_name=capability_name,
                review_required=True,
                notes=["Mention was captured during session authoring and left unresolved."],
            )
            for capability_name in unsupported_capabilities
        ]
        for capability_id in capability_mentions:
            capability = self.capability_registry.get_capability(capability_id)
            if capability is None or not capability.requires_review:
                continue
            gaps.append(
                self.capability_registry.build_gap_response(
                    gap_kind="review_needed",
                    stage="authoring",
                    logical_step_kind=capability.supported_step_kinds[0] if capability.supported_step_kinds else None,
                    requested_capability_id_or_name=capability.capability_id,
                    normalized_requested_capability_id=capability.capability_id,
                    reason="Capability is registered but still requires explicit review before relying on it operationally.",
                    review_required=True,
                    notes=["Registered capability mention surfaced during authoring."],
                )
            )
        return gaps

    def _normalize_patch_value(self, field_name: str, value: Any) -> Any:
        if field_name in {"title", "summary", "goal", "target_users", "input_description", "output_description", "domain"}:
            return normalize_optional_text(value)
        if field_name == "interactivity_level":
            return normalize_interactivity_level(value)
        if field_name == "recurrence_mode":
            return normalize_recurrence_mode(value)
        if field_name == "human_review_required":
            return bool(value)
        if field_name == "constraints_json":
            return normalize_constraints_payload(value).model_dump(mode="python")
        if field_name == "acceptance_criteria_json":
            return normalize_acceptance_criteria_payload(value).model_dump(mode="python")
        if field_name == "external_tools_needed_json":
            return normalize_external_tools_payload(value)
        if field_name.startswith("structured_spec_json."):
            nested = field_name.split(".", 1)[1]
            if nested in {"delivery_form", "deployment"}:
                return normalize_optional_text(value)
            if nested in {"p0_features", "p1_features"}:
                return self._dedupe([str(item).strip() for item in (value or []) if str(item).strip()])
        return value

    def _should_apply_patch_value(self, *, current_value: Any, new_value: Any, block_overwrite: bool) -> bool:
        if new_value in (None, "", [], {}):
            return False
        if current_value in (None, "", [], {}):
            return True
        return not block_overwrite

    def _normalize_unsupported_capabilities(self, patch: RequirementDraftPatchResponse) -> list[str]:
        unsupported = list(patch.unsupported_capability_mentions or [])
        for capability_id in patch.capability_mentions or []:
            if self.capability_registry.get_capability(capability_id) is None:
                unsupported.append(capability_id)
        return self._dedupe([item for item in unsupported if item])
