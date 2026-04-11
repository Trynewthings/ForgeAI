from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.core.config import settings
from app.core.enums import RequirementMessageRole, RequirementSessionStage, RequirementSessionStatus
from app.core.exceptions import AppException
from app.core.logging import log_event
from app.db.models.entities import RequirementMessage, RequirementSession
from app.db.repositories.requirement_repository import RequirementRepository
from app.repositories.agent_system_repository import AgentSystemRepository
from app.repositories.project_repository import ProjectRepository
from app.repositories.requirement_message_repository import RequirementMessageRepository
from app.repositories.requirement_session_repository import RequirementSessionRepository
from app.schemas.requirement import RequirementCreateRequest, RequirementResponse
from app.schemas.problem_formulation import ClarificationResolutionRecord
from app.schemas.requirement_compiler import CompilerPresentationItemResponse, CompilerPresentationSectionResponse
from app.schemas.requirement_session import (
    RequirementAuthoringSemanticStateResponse,
    RequirementFinalizeResponse,
    RequirementDraftPatchStateResponse,
    RequirementSessionFinalizeReasonResponse,
    RequirementSessionFinalizeStatusResponse,
    RequirementMessageCreateRequest,
    RequirementSessionResolutionRequest,
    RequirementMessageResponse,
    RequirementSessionCreateRequest,
    RequirementSessionResponse,
)
from app.services.requirement.requirement_compiler_service import RequirementCompilerService
from app.services.requirement.compiler import build_compiler_llm_client
from app.services.requirement.requirement_service import RequirementService
from app.services.capabilities import CapabilityRegistryService, CapabilityRequestService, CapabilityResolutionService
from app.services.requirement_clarifier_service import RequirementClarifierService
from app.services.provider_runtime import ProviderCallError, get_provider_adapter

logger = logging.getLogger(__name__)


def _iso(value) -> str:
    return value.isoformat() if value else ""


class RequirementSessionService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.project_repository = ProjectRepository(db)
        self.session_repository = RequirementSessionRepository(db)
        self.message_repository = RequirementMessageRepository(db)
        self.agent_system_repository = AgentSystemRepository(db)
        self.requirement_repository = RequirementRepository(db)
        self.requirement_service = RequirementService(db)
        self.compiler_service = RequirementCompilerService(db)
        self.capability_registry = CapabilityRegistryService()
        self.capability_request_service = CapabilityRequestService(db)
        self.capability_resolution_service = CapabilityResolutionService(db)
        self.clarifier = RequirementClarifierService()

    def create_session(self, payload: RequirementSessionCreateRequest) -> RequirementSessionResponse:
        project = self.project_repository.get_by_id(payload.project_id)
        if project is None:
            raise AppException("Project not found", status_code=404)

        session = RequirementSession(
            project_id=project.id,
            current_stage=RequirementSessionStage.DISCOVERY.value,
            status=RequirementSessionStatus.ACTIVE.value,
            requirement_draft_json={},
        )
        self.session_repository.create(session)
        draft = self.clarifier.build_empty_requirement_draft(
            project_id=project.id,
            session_id=session.id,
            raw_user_input="",
            messages=[],
        )
        session.requirement_draft_json = draft

        assistant_message = RequirementMessage(
            requirement_session_id=session.id,
            role=RequirementMessageRole.ASSISTANT.value,
            content=(
                "Let's build the requirement draft. Start with the project goal, target users, "
                "delivery form, and core P0 features."
            ),
            structured_snapshot_json=draft,
        )
        self.message_repository.create(assistant_message)
        self.db.commit()
        return self.get_session(session.id)

    def send_message(self, session_id: str, payload: RequirementMessageCreateRequest) -> RequirementSessionResponse:
        session = self.session_repository.get_by_id(session_id, with_messages=True)
        if session is None:
            raise AppException("Requirement session not found", status_code=404)
        if session.status == RequirementSessionStatus.FINALIZED.value:
            raise AppException("Requirement session is already finalized", status_code=400)

        user_message = RequirementMessage(
            requirement_session_id=session.id,
            role=RequirementMessageRole.USER.value,
            content=payload.content.strip(),
        )
        self.message_repository.create(user_message)

        user_messages = [message.content for message in session.messages if message.role == RequirementMessageRole.USER.value]
        user_messages.append(user_message.content)
        raw_user_input = "\n".join(user_messages)
        requirement_draft = self._update_session_draft(
            session=session,
            raw_user_input=raw_user_input,
            messages=list(session.messages) + [user_message],
        )
        # --- Explicit Clarification Resolution: detect user confirmations ---
        self._detect_and_store_resolutions(
            requirement_draft=requirement_draft,
            user_content=payload.content.strip(),
        )

        authoring_feedback, finalize_status = self._build_feedback_for_draft(session=session, requirement_draft=requirement_draft)
        session.requirement_draft_json = requirement_draft
        session.raw_user_input = raw_user_input
        session.current_stage = self._session_stage_from_feedback(authoring_feedback)
        session.status = self._session_status_from_feedback(authoring_feedback)

        conversation = [
            {
                "role": message.role,
                "content": message.content,
            }
            for message in list(session.messages) + [user_message]
        ]
        assistant_content = self._build_runtime_assistant_reply(
            project_id=session.project_id,
            requirement_draft=requirement_draft,
            authoring_feedback=authoring_feedback,
            finalize_status=finalize_status,
            conversation=conversation,
        )

        assistant_message = RequirementMessage(
            requirement_session_id=session.id,
            role=RequirementMessageRole.ASSISTANT.value,
            content=assistant_content,
            structured_snapshot_json=requirement_draft,
        )
        self.message_repository.create(assistant_message)
        self.db.commit()
        return self.get_session(session.id)

    def get_session(self, session_id: str) -> RequirementSessionResponse:
        session = self.session_repository.get_by_id(session_id, with_messages=True)
        if session is None:
            raise AppException("Requirement session not found", status_code=404)
        return self._serialize_session(session)

    def resolve_clarification(self, session_id: str, payload: RequirementSessionResolutionRequest) -> RequirementSessionResponse:
        session = self.session_repository.get_by_id(session_id, with_messages=True)
        if session is None:
            raise AppException("Requirement session not found", status_code=404)
        if session.status == RequirementSessionStatus.FINALIZED.value:
            raise AppException("Requirement session is already finalized", status_code=400)

        requirement_draft = self._load_session_draft(session)
        self._store_explicit_resolution(
            requirement_draft=requirement_draft,
            axis=payload.axis,
            resolved_value=payload.resolved_value,
            resolution_type=payload.resolution_type,
        )

        authoring_feedback, finalize_status = self._build_feedback_for_draft(session=session, requirement_draft=requirement_draft)
        session.requirement_draft_json = requirement_draft
        session.current_stage = self._session_stage_from_feedback(authoring_feedback)
        session.status = self._session_status_from_feedback(authoring_feedback)
        self.db.commit()
        return self.get_session(session.id)

    def finalize(self, session_id: str) -> RequirementFinalizeResponse:
        session = self.session_repository.get_by_id(session_id, with_messages=True)
        if session is None:
            raise AppException("Requirement session not found", status_code=404)

        project = self.project_repository.get_by_id(session.project_id)
        if project is None:
            raise AppException("Project not found", status_code=404)

        user_messages = [message.content for message in session.messages if message.role == RequirementMessageRole.USER.value]
        raw_user_input = "\n".join(user_messages)
        requirement_draft = self._load_session_draft(session)
        authoring_feedback, finalize_status = self._build_feedback_for_draft(session=session, requirement_draft=requirement_draft)
        if not finalize_status.can_finalize:
            blocking_targets = [item.target_field for item in finalize_status.blocking_reasons if item.target_field]
            missing = blocking_targets or [item.code for item in finalize_status.blocking_reasons]
            raise AppException(
                f"Requirement session still needs clarification before finalize: {', '.join(missing)}",
                status_code=400,
                error={
                    "code": "session_finalize_blocked",
                    "finalize_status": finalize_status.model_dump(mode="json"),
                    "requirement_draft_json": requirement_draft,
                },
            )

        markdown_document = self.clarifier.build_markdown_projection(project.name, requirement_draft, user_messages)
        session.requirement_draft_json = requirement_draft
        session.markdown_document = markdown_document
        session.raw_user_input = raw_user_input
        session.status = RequirementSessionStatus.FINALIZED.value
        session.current_stage = RequirementSessionStage.FINALIZED.value

        assistant_message = RequirementMessage(
            requirement_session_id=session.id,
            role=RequirementMessageRole.ASSISTANT.value,
            content=(
                "The requirement draft is finalized. Recommended next step: export it to a structured "
                "Requirement, then use compile/build/run from the structured path. The direct session-to-build "
                "path has been removed."
            ),
            structured_snapshot_json=requirement_draft,
        )
        self.message_repository.create(assistant_message)

        self.db.commit()
        refreshed = self.get_session(session.id)
        return RequirementFinalizeResponse(
            session=refreshed,
            markdown_document=markdown_document,
            requirement_draft_json=requirement_draft,
        )

    def export_to_requirement(self, session_id: str) -> RequirementResponse:
        session = self.session_repository.get_by_id(session_id, with_messages=True)
        if session is None:
            raise AppException("Requirement session not found", status_code=404)
        if session.status != RequirementSessionStatus.FINALIZED.value:
            raise AppException("Requirement session must be finalized before export", status_code=400)
        if session.exported_requirement_id:
            existing_requirement = self.requirement_repository.get_by_id(session.exported_requirement_id)
            if existing_requirement is not None:
                session.export_status = "exported"
                if session.exported_at is None:
                    session.exported_at = datetime.now(timezone.utc)
                    self.db.commit()
                    self.db.refresh(session)
                return self.requirement_service.get_requirement(existing_requirement.id)

        requirement_draft = self._load_session_draft(session)
        export_payload = self._build_export_payload(requirement_draft)
        request = RequirementCreateRequest.model_validate(export_payload)
        requirement = self.requirement_service.create_requirement(request)
        session.requirement_draft_json = requirement_draft
        session.exported_requirement_id = requirement.id
        session.exported_at = datetime.now(timezone.utc)
        session.export_status = "exported"
        self.db.commit()
        self.db.refresh(session)
        return requirement

    def _serialize_session(self, session: RequirementSession) -> RequirementSessionResponse:
        requirement_draft = self._load_session_draft(session)
        authoring_feedback, finalize_status = self._build_feedback_for_draft(session=session, requirement_draft=requirement_draft)
        missing_fields = self._session_missing_fields(finalize_status)
        draft_patch_state = self._load_draft_patch_state(requirement_draft)
        authoring_semantic_state = self._load_authoring_semantic_state(requirement_draft)
        return RequirementSessionResponse(
            id=session.id,
            project_id=session.project_id,
            current_stage=session.current_stage,
            status=session.status,
            raw_user_input=session.raw_user_input,
            requirement_draft_json=requirement_draft,
            markdown_document=session.markdown_document,
            authoring_semantic_state=authoring_semantic_state,
            can_finalize=finalize_status.can_finalize,
            missing_fields=missing_fields,
            finalize_status=finalize_status,
            draft_patch_state=draft_patch_state,
            export_status=session.export_status or "not_exported",
            exported_requirement_id=session.exported_requirement_id,
            exported_at=_iso(session.exported_at),
            structured_path_hint=self._structured_path_hint(session),
            legacy_build_path_removed=True,
            authoring_feedback=authoring_feedback,
            messages=[
                RequirementMessageResponse(
                    id=message.id,
                    role=message.role,
                    content=message.content,
                    created_at=_iso(message.created_at),
                )
                for message in session.messages
            ],
            created_at=_iso(session.created_at),
            updated_at=_iso(session.updated_at),
        )

    def _structured_path_hint(self, session: RequirementSession) -> str:
        if session.exported_requirement_id:
            return (
                f"Structured path default: continue from exported Requirement {session.exported_requirement_id} "
                "using compile/build/run. The direct legacy session->build path has been removed."
            )
        if session.status == RequirementSessionStatus.FINALIZED.value:
            return (
                "Structured path default: export this finalized session to a Requirement, then use "
                "compile/build/run. The direct legacy session->build path has been removed."
            )
        return (
            "Structured path default: refine the session, finalize it, export to a Requirement, then use "
            "compile/build/run."
        )

    def _build_runtime_assistant_reply(
        self,
        *,
        project_id: str,
        requirement_draft: dict,
        authoring_feedback,
        finalize_status,
        conversation: list[dict[str, str]],
    ) -> str:
        feedback_payload = authoring_feedback.model_dump(mode="python") if authoring_feedback is not None else None
        fallback_reply = self.clarifier.build_assistant_reply(requirement_draft, authoring_feedback=feedback_payload)
        if settings.llm_mode != "real":
            return fallback_reply

        model_config, source = self._resolve_requirement_analyst_model_config(project_id)
        messages = self.clarifier.build_provider_messages(
            requirement_draft,
            conversation,
            authoring_feedback=feedback_payload,
        )
        try:
            adapter, runtime_config = get_provider_adapter(model_config)
            response = adapter.chat(messages)
            log_event(
                logger,
                logging.INFO,
                "requirement.reply.generated",
                project_id=project_id,
                provider=runtime_config.provider,
                model_name=runtime_config.model_name,
                source=source,
                total_tokens=response.total_tokens,
            )
            provider_reply = response.content.strip()
            if not provider_reply:
                return fallback_reply
            return f"{provider_reply}\n\n{self._build_runtime_status_footer(finalize_status)}"
        except ProviderCallError as exc:
            log_event(
                logger,
                logging.WARNING,
                "requirement.reply.provider_failed",
                project_id=project_id,
                source=source,
                error=str(exc),
                status_code=exc.status_code,
                raw_metadata=exc.raw_metadata,
            )
            return f"[provider-fallback] Real model request failed: {str(exc)}\n\n{fallback_reply}"

    def _resolve_requirement_analyst_model_config(self, project_id: str):
        agent_system = self.agent_system_repository.get_by_project_id(project_id, with_nodes=True)
        if agent_system is None:
            return None, "env-defaults"
        for node in agent_system.agent_nodes:
            if node.role == "requirement_analyst" and node.model_config is not None:
                return node.model_config, f"agent-node:{node.id}"
        return None, "env-defaults"

    def _update_session_draft(
        self,
        *,
        session: RequirementSession,
        raw_user_input: str | None,
        messages,
    ) -> dict:
        previous_draft = self._load_session_draft(session)
        current_feedback = self.compiler_service.build_authoring_feedback_from_payload(
            previous_draft,
            source_kind="session",
            source_id=session.id,
            llm_client=self._build_requirement_session_llm_client(project_id=session.project_id),
        ).model_dump(mode="python")
        llm_client = self._build_requirement_session_llm_client(project_id=session.project_id)
        conversation = [
            {"role": message.role, "content": message.content}
            for message in messages
        ]
        user_messages = [message["content"] for message in conversation if message["role"] == RequirementMessageRole.USER.value]
        payload = RequirementCreateRequest.model_validate(
            self.clarifier.update_requirement_draft_from_messages(
                project_id=session.project_id,
                session_id=session.id,
                messages=user_messages,
                raw_user_input=raw_user_input,
                conversation=conversation,
                previous_draft=previous_draft,
                authoring_feedback=current_feedback,
                llm_client=llm_client,
            )
        ).model_dump(mode="python")
        return payload

    def _load_session_draft(self, session: RequirementSession) -> dict:
        if session.requirement_draft_json:
            try:
                return RequirementCreateRequest.model_validate(session.requirement_draft_json).model_dump(mode="python")
            except ValidationError:
                rebuilt_draft = self._rebuild_session_draft(session)
                session.requirement_draft_json = rebuilt_draft
                return rebuilt_draft

        rebuilt_draft = self._rebuild_session_draft(session)
        session.requirement_draft_json = rebuilt_draft
        return rebuilt_draft

    def _rebuild_session_draft(self, session: RequirementSession) -> dict:
        conversation = [
            {"role": message.role, "content": message.content}
            for message in session.messages
        ]
        user_messages = [message["content"] for message in conversation if message["role"] == RequirementMessageRole.USER.value]
        return RequirementCreateRequest.model_validate(
            self.clarifier.update_requirement_draft_from_messages(
                project_id=session.project_id,
                session_id=session.id,
                messages=user_messages,
                raw_user_input=session.raw_user_input,
                conversation=conversation,
                previous_draft=None,
            )
        ).model_dump(mode="python")

    def _build_feedback_for_draft(self, *, session: RequirementSession, requirement_draft: dict):
        authoring_feedback = self.compiler_service.build_authoring_feedback_from_payload(
            requirement_draft,
            source_kind="session",
            source_id=session.id,
            llm_client=self._build_requirement_session_llm_client(project_id=session.project_id),
        )
        self._apply_patch_state_notes(authoring_feedback, requirement_draft)
        patch_state = self._load_draft_patch_state(requirement_draft)
        self._persist_problem_closure_contracts(requirement_draft, authoring_feedback=authoring_feedback)
        capability_requests = self.capability_request_service.ensure_requests_for_gaps(
            project_id=session.project_id,
            gaps=list(authoring_feedback.explanation.capability_gaps if authoring_feedback.explanation is not None else []),
            source_session_id=session.id,
        )
        if authoring_feedback.explanation is not None:
            authoring_feedback.explanation.capability_requests = capability_requests
            authoring_feedback.explanation.capability_request_summary = self.capability_request_service.summarize_requests(capability_requests)
            authoring_feedback.explanation.capability_resolution = self.capability_resolution_service.build_role_views(
                project_id=session.project_id,
                capability_gaps=list(authoring_feedback.explanation.capability_gaps),
                capability_requests=capability_requests,
            )
            self._apply_capability_request_presentation(authoring_feedback)
            self._apply_capability_resolution_presentation(authoring_feedback)
        patch_state.capability_requests = capability_requests
        patch_state.capability_request_summary = self.capability_request_service.summarize_requests(capability_requests)
        patch_state.capability_resolution = self.capability_resolution_service.build_role_views(
            project_id=session.project_id,
            capability_gaps=list(patch_state.capability_gaps),
            capability_requests=capability_requests,
        )
        requirement_draft.setdefault("structured_spec_json", {})["draft_patch_state"] = patch_state.model_dump(mode="python")
        resolutions = self._load_clarification_resolutions(requirement_draft)
        finalize_status = self._build_finalize_status(authoring_feedback, resolutions=resolutions)
        return authoring_feedback, finalize_status

    def _build_export_payload(self, requirement_draft: dict) -> dict:
        payload = dict(requirement_draft)
        raw_conversation_json = dict(requirement_draft.get("raw_conversation_json") or {})
        raw_conversation_json["source"] = "requirement_session_export"
        structured_spec_json = dict(requirement_draft.get("structured_spec_json") or {})
        structured_spec_json["source"] = "requirement_session_export"
        structured_spec_json.pop("draft_patch_state", None)
        payload["raw_conversation_json"] = raw_conversation_json
        payload["structured_spec_json"] = structured_spec_json
        payload.pop("_clarification_resolutions", None)
        return payload

    def _session_stage_from_feedback(self, authoring_feedback) -> str:
        if authoring_feedback.completion_state_preview == "clarification_required":
            return RequirementSessionStage.REFINEMENT.value
        return RequirementSessionStage.READY.value

    def _session_status_from_feedback(self, authoring_feedback) -> str:
        if authoring_feedback.completion_state_preview == "clarification_required":
            return RequirementSessionStatus.ACTIVE.value
        return RequirementSessionStatus.READY.value

    def _session_missing_fields(self, finalize_status: RequirementSessionFinalizeStatusResponse) -> list[str]:
        values = [item.target_field or item.code for item in finalize_status.blocking_reasons]
        deduped: list[str] = []
        seen: set[str] = set()
        for item in values:
            if item and item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped

    def _build_finalize_status(self, authoring_feedback, resolutions: dict | None = None) -> RequirementSessionFinalizeStatusResponse:
        blocking_reasons: list[RequirementSessionFinalizeReasonResponse] = []
        warning_reasons: list[RequirementSessionFinalizeReasonResponse] = []
        seen_blocking: set[str] = set()
        seen_warning: set[tuple[str, str | None]] = set()
        resolved_axes = set((resolutions or {}).keys())

        for item in authoring_feedback.clarification_items:
            if item.answer:
                continue
            # Skip items whose blocking axis has been explicitly resolved
            target = item.target_field or ""
            item_axes = [item.blocking_semantic_axis] if item.blocking_semantic_axis else self._axis_for_target_field(target)
            if target in resolved_axes or any(axis in resolved_axes for axis in item_axes):
                continue
            severity = "blocking" if item.required else "warning"
            reason = RequirementSessionFinalizeReasonResponse(
                code=item.id,
                severity=severity,
                source="clarification_item",
                message=item.question,
                target_field=item.target_field,
            )
            key = self._blocking_reason_key(reason)
            if severity == "blocking":
                if key not in seen_blocking:
                    seen_blocking.add(key)
                    blocking_reasons.append(reason)
            elif key not in seen_warning:
                seen_warning.add(key)
                warning_reasons.append(reason)

        for warning in authoring_feedback.warnings or []:
            key = (warning, None)
            if key in seen_warning:
                continue
            seen_warning.add(key)
            warning_reasons.append(
                RequirementSessionFinalizeReasonResponse(
                    code="warning",
                    severity="warning",
                    source="authoring_warning",
                    message=warning,
                    target_field=None,
                )
            )

        if authoring_feedback.normalized_field_notes:
            for note in authoring_feedback.normalized_field_notes:
                key = (note, None)
                if key in seen_warning:
                    continue
                seen_warning.add(key)
                warning_reasons.append(
                    RequirementSessionFinalizeReasonResponse(
                        code="draft_patch_note",
                        severity="warning",
                        source="draft_patch",
                        message=note,
                        target_field=None,
                    )
                )

        explanation = authoring_feedback.explanation
        if explanation is not None and explanation.solvability is not None:
            solvability = explanation.solvability
            if solvability.solvability_status in {"not_solvable", "solvable_with_clarification"}:
                reasons = [*solvability.unsatisfied_requirements, *solvability.unsafe_requirements]
                if not reasons and solvability.reasoning_summary:
                    reasons = [solvability.reasoning_summary]
                for reason in reasons:
                    reason_key = ("solvability", "problem_solvability", reason)
                    if reason_key in seen_blocking:
                        continue
                    seen_blocking.add(reason_key)
                    blocking_reasons.append(
                        RequirementSessionFinalizeReasonResponse(
                            code="solvability",
                            severity="blocking",
                            source="solvability",
                            message=reason,
                            target_field="problem_solvability",
                        )
                    )
            elif solvability.solvability_status == "solvable_with_review":
                for reason in solvability.required_reviews:
                    reason_key = (reason, "problem_solvability")
                    if reason_key in seen_warning:
                        continue
                    seen_warning.add(reason_key)
                    warning_reasons.append(
                        RequirementSessionFinalizeReasonResponse(
                            code="solvability_review",
                            severity="warning",
                            source="solvability",
                            message=reason,
                            target_field="problem_solvability",
                        )
                    )

        next_action_keys = [action.action_key for action in (authoring_feedback.explanation.next_actions if authoring_feedback.explanation else [])]
        completion_state = authoring_feedback.completion_state_preview
        if completion_state == "ready_to_compile":
            summary = "Session can be finalized and exported into the structured Requirement flow."
        elif completion_state == "compilable_with_warnings":
            summary = "Session can be finalized and exported, but warning-level review is still recommended."
        else:
            summary = "Session cannot be finalized until blocking clarification work is resolved."

        return RequirementSessionFinalizeStatusResponse(
            can_finalize=completion_state != "clarification_required",
            completion_state_preview=completion_state,
            summary=summary,
            blocking_reasons=blocking_reasons,
            warning_reasons=warning_reasons,
            next_action_keys=next_action_keys,
        )

    # ------------------------------------------------------------------
    # Explicit Clarification Resolution Layer (v1)
    # ------------------------------------------------------------------
    # Converts user confirmations (e.g. "确认", "可以", "就这样") into
    # structural resolution records that the finalize gate can consume,
    # without relying on LLM semantic refinement to update fields.

    _CONFIRMATION_SIGNALS = {
        "确认", "确定", "可以", "好的", "就这样", "没问题", "同意",
        "对", "是的", "ok", "yes", "confirm", "agreed", "fine",
        "按这个来", "就按这个",
    }
    _URL_RE = re.compile(r"(https?://[^\s]+|git@[^\s]+|github\.com/[^\s]+|ssh://[^\s]+)", re.IGNORECASE)
    _PATH_RE = re.compile(r"(?:(?:\.\.?/|/|~/)[^\s]+)")

    def _detect_and_store_resolutions(self, *, requirement_draft: dict, user_content: str) -> None:
        """Detect explicit or confirmatory clarification resolutions from user input."""
        text = user_content.lower().strip().rstrip("。.!！")
        spec = requirement_draft.setdefault("structured_spec_json", {})
        sem = spec.get("authoring_semantic_state", {})
        pending = {
            str(item).strip()
            for item in (sem.get("pending_semantic_fields", []) or [])
            if str(item).strip()
        }
        pending.update(
            str(item).strip()
            for item in (sem.get("ambiguities", []) or [])
            if str(item).strip()
        )
        resolutions = self._load_clarification_resolutions(requirement_draft)
        unresolved_axes = [
            axis
            for axis in ("source_locator", "source_modality", "deliverable_form", "analysis_frame", "workflow_shape")
            if axis in pending and axis not in resolutions
        ]
        if not unresolved_axes:
            return

        detected: dict[str, dict] = {}
        for axis in unresolved_axes:
            record = self._detect_explicit_resolution(
                axis=axis,
                requirement_draft=requirement_draft,
                user_content=user_content,
            )
            if record is not None:
                detected[axis] = record

        is_confirmation = text in self._CONFIRMATION_SIGNALS or any(
            signal in text for signal in self._CONFIRMATION_SIGNALS if len(signal) > 2
        )
        if is_confirmation:
            for axis in unresolved_axes:
                if axis in detected:
                    continue
                record = self._detect_confirmation_resolution(
                    axis=axis,
                    requirement_draft=requirement_draft,
                    user_content=user_content,
                )
                if record is not None:
                    detected[axis] = record

        if not detected:
            return

        for axis, record in detected.items():
            resolutions[axis] = record
            self._apply_resolution_to_draft(
                requirement_draft=requirement_draft,
                axis=axis,
                resolved_value=record.get("resolved_value"),
            )
        spec["_clarification_resolutions"] = resolutions

    def _detect_explicit_resolution(self, *, axis: str, requirement_draft: dict, user_content: str) -> dict | None:
        sem = ((requirement_draft.get("structured_spec_json") or {}).get("authoring_semantic_state") or {})
        text = user_content.strip()
        lowered = text.lower()

        if axis == "source_locator":
            match = self._URL_RE.search(text) or self._PATH_RE.search(text)
            if match:
                value = match.group(0).rstrip(")。,.!！]")
                return self._build_resolution_record(
                    axis=axis,
                    resolution_type="user_provided_explicit_value",
                    resolved_value=value,
                    user_content=user_content,
                )
            return None

        if axis == "source_modality":
            target_object = str(sem.get("target_object") or "").strip()
            if any(token in lowered for token in ("github", "gitlab", "remote", "远程", "仓库地址", "url", "链接", "网址", "http://", "https://")):
                value = "remote_repo" if target_object == "repository" else "web_source"
                return self._build_resolution_record(axis=axis, resolution_type="user_provided_explicit_value", resolved_value=value, user_content=user_content)
            if any(token in lowered for token in ("本地", "workspace", "工作区", "路径", "本机", "local", "目录")):
                value = "workspace_repo" if target_object == "repository" else "local_path"
                return self._build_resolution_record(axis=axis, resolution_type="user_provided_explicit_value", resolved_value=value, user_content=user_content)
            return None

        if axis == "deliverable_form":
            if any(token in lowered for token in ("briefing", "brief", "简报", "digest", "摘要")):
                return self._build_resolution_record(axis=axis, resolution_type="user_provided_explicit_value", resolved_value="briefing", user_content=user_content)
            if any(token in lowered for token in ("artifact bundle", "artifact", "bundle", "package", "zip", "制品", "产物包")):
                return self._build_resolution_record(axis=axis, resolution_type="user_provided_explicit_value", resolved_value="artifact_bundle", user_content=user_content)
            if any(token in lowered for token in ("report", "报告")):
                return self._build_resolution_record(axis=axis, resolution_type="user_provided_explicit_value", resolved_value="report", user_content=user_content)
            return None

        if axis == "analysis_frame":
            if any(token in lowered for token in ("architecture", "架构")):
                return self._build_resolution_record(axis=axis, resolution_type="user_provided_explicit_value", resolved_value="architecture", user_content=user_content)
            if any(token in lowered for token in ("audit", "审计")):
                return self._build_resolution_record(axis=axis, resolution_type="user_provided_explicit_value", resolved_value="audit", user_content=user_content)
            if any(token in lowered for token in ("assessment", "assess", "evaluate", "评估")):
                return self._build_resolution_record(axis=axis, resolution_type="user_provided_explicit_value", resolved_value="assessment", user_content=user_content)
            if any(token in lowered for token in ("summary", "summarize", "总结", "概览", "综述")):
                return self._build_resolution_record(axis=axis, resolution_type="user_provided_explicit_value", resolved_value="summary", user_content=user_content)
            return None

        if axis == "workflow_shape":
            if any(token in lowered for token in ("一次性", "单次", "one time", "one-time", "once")):
                return self._build_resolution_record(axis=axis, resolution_type="user_provided_explicit_value", resolved_value="single_service", user_content=user_content)
            if any(token in lowered for token in ("多步", "workflow", "工作流", "分步", "bounded")):
                return self._build_resolution_record(axis=axis, resolution_type="user_provided_explicit_value", resolved_value="bounded_workflow", user_content=user_content)
        return None

    def _detect_confirmation_resolution(self, *, axis: str, requirement_draft: dict, user_content: str) -> dict | None:
        sem = ((requirement_draft.get("structured_spec_json") or {}).get("authoring_semantic_state") or {})
        if axis == "workflow_shape":
            deliverable_contract = sem.get("deliverable_contract", "")
            suggested = "bounded_workflow" if deliverable_contract in ("architecture_report", "research_briefing") else "single_service"
            return self._build_resolution_record(
                axis=axis,
                resolution_type="user_confirmed_suggested_value",
                resolved_value=suggested,
                user_content=user_content,
            )
        return None

    def _build_resolution_record(self, *, axis: str, resolution_type: str, resolved_value, user_content: str) -> dict:
        return ClarificationResolutionRecord(
            axis=axis,
            resolution_type=resolution_type,
            resolved_value=resolved_value,
            source_text=user_content,
            accepted=True,
        ).model_dump(mode="python")

    def _store_explicit_resolution(
        self,
        *,
        requirement_draft: dict,
        axis: str,
        resolved_value,
        resolution_type: str,
    ) -> None:
        spec = requirement_draft.setdefault("structured_spec_json", {})
        resolutions = self._load_clarification_resolutions(requirement_draft)
        record = ClarificationResolutionRecord(
            axis=axis,
            resolution_type=resolution_type,
            resolved_value=resolved_value,
            accepted=True,
        ).model_dump(mode="python")
        resolutions[axis] = record
        self._apply_resolution_to_draft(
            requirement_draft=requirement_draft,
            axis=axis,
            resolved_value=resolved_value,
        )

        target_object = str(((spec.get("authoring_semantic_state") or {}).get("target_object") or "")).strip()
        for companion_axis, companion_value in self._infer_companion_resolutions(
            axis=axis,
            resolved_value=resolved_value,
            target_object=target_object,
        ):
            resolutions.setdefault(
                companion_axis,
                ClarificationResolutionRecord(
                    axis=companion_axis,
                    resolution_type="system_inferred_and_confirmed",
                    resolved_value=companion_value,
                    accepted=True,
                ).model_dump(mode="python"),
            )
            self._apply_resolution_to_draft(
                requirement_draft=requirement_draft,
                axis=companion_axis,
                resolved_value=companion_value,
            )

        spec["_clarification_resolutions"] = resolutions

    def _infer_companion_resolutions(self, *, axis: str, resolved_value, target_object: str) -> list[tuple[str, str]]:
        if axis != "source_locator" or not isinstance(resolved_value, str):
            return []
        lowered = resolved_value.strip().lower()
        if re.match(r"^(https?://|ssh://|git@|github\.com/)", lowered):
            if target_object == "repository":
                return [("source_modality", "remote_repo")]
            return [("source_modality", "web_source")]
        if lowered.startswith(("/", "./", "../", "~/")):
            return [("source_modality", "workspace_repo" if target_object == "repository" else "local_path")]
        return []

    def _apply_resolution_to_draft(self, *, requirement_draft: dict, axis: str, resolved_value) -> None:
        spec = requirement_draft.setdefault("structured_spec_json", {})
        sem = spec.setdefault("authoring_semantic_state", {})
        global_core = sem.get("global_core") if isinstance(sem.get("global_core"), dict) else None

        if axis in {"workflow_shape", "source_modality", "deliverable_form", "analysis_frame"}:
            sem[axis] = resolved_value
            if global_core is not None:
                global_core[axis] = resolved_value
        elif axis == "source_locator" and isinstance(resolved_value, str) and resolved_value.strip():
            normalized_value = resolved_value.strip()
            requirement_draft["input_description"] = normalized_value
            if isinstance(sem.get("input_items"), list):
                if sem["input_items"]:
                    sem["input_items"][0] = normalized_value
                else:
                    sem["input_items"].append(normalized_value)
        elif axis == "delivery_target" and isinstance(resolved_value, str) and resolved_value.strip():
            normalized_value = resolved_value.strip()
            # Output closure axis: project into the draft's output_description
            # so downstream closure services see a concrete delivery target
            # and can flip output_closure_status to "output_closed".
            requirement_draft["output_description"] = normalized_value
            if isinstance(sem.get("output_items"), list):
                if sem["output_items"]:
                    sem["output_items"][0] = normalized_value
                else:
                    sem["output_items"].append(normalized_value)
        elif axis == "output_modality" and isinstance(resolved_value, str) and resolved_value.strip():
            normalized_value = resolved_value.strip().lower()
            sem["output_modality"] = normalized_value
            if global_core is not None:
                global_core["output_modality"] = normalized_value
        elif axis == "target_object" and isinstance(resolved_value, str) and resolved_value.strip():
            normalized_value = resolved_value.strip().lower()
            sem["target_object"] = normalized_value
            if global_core is not None:
                global_core["target_object"] = normalized_value

        if axis in sem.get("pending_semantic_fields", []):
            sem["pending_semantic_fields"] = [field for field in sem["pending_semantic_fields"] if field != axis]
        if axis in sem.get("ambiguities", []):
            sem["ambiguities"] = [item for item in sem["ambiguities"] if item != axis]

    def _build_runtime_status_footer(self, finalize_status: RequirementSessionFinalizeStatusResponse) -> str:
        if finalize_status.can_finalize:
            return f"Deterministic session status: {finalize_status.summary}"
        blocking = "\n".join(
            f"- {reason.target_field or reason.code}: {reason.message}"
            for reason in finalize_status.blocking_reasons[:3]
        )
        if not blocking:
            blocking = "- Resolve the remaining blocking clarification items shown in the preview."
        return (
            "Deterministic session status: finalize is still blocked.\n"
            f"{blocking}"
        )

    def _blocking_reason_key(self, reason: RequirementSessionFinalizeReasonResponse) -> str:
        return reason.target_field or reason.code

    @staticmethod
    def _axis_for_target_field(target_field: str) -> list[str]:
        """Map clarification target_field to the semantic axes it covers."""
        mapping = {
            "recurrence_mode": ["workflow_shape"],
            "input_description": ["target_object", "source_locator", "source_modality", "evidence_basis"],
            "output_description": ["delivery_target", "deliverable_form", "analysis_frame"],
            "acceptance_criteria_json": ["acceptance_criteria"],
        }
        return mapping.get(target_field, [])

    def _load_clarification_resolutions(self, requirement_draft: dict) -> dict:
        spec = requirement_draft.get("structured_spec_json", {}) or {}
        nested = spec.get("_clarification_resolutions")
        if isinstance(nested, dict):
            return dict(nested)
        legacy = requirement_draft.get("_clarification_resolutions")
        if isinstance(legacy, dict):
            return dict(legacy)
        return {}

    def _persist_problem_closure_contracts(self, requirement_draft: dict, *, authoring_feedback) -> None:
        explanation = authoring_feedback.explanation
        if explanation is None:
            return
        spec = requirement_draft.setdefault("structured_spec_json", {})
        if explanation.problem_formulation is not None:
            spec["problem_formulation"] = explanation.problem_formulation.model_dump(mode="python")
        if explanation.input_closure is not None:
            spec["input_closure"] = explanation.input_closure.model_dump(mode="python")
        if explanation.output_closure is not None:
            spec["output_closure"] = explanation.output_closure.model_dump(mode="python")
        if explanation.solvability is not None:
            spec["solvability"] = explanation.solvability.model_dump(mode="python")

    def _build_requirement_session_llm_client(self, *, project_id: str):
        model_config, _ = self._resolve_requirement_analyst_model_config(project_id)
        disabled_reason = None
        enabled = settings.llm_mode == "real"
        if not enabled:
            disabled_reason = "llm_mode_disabled"
        _, runtime_config = get_provider_adapter(model_config)
        return build_compiler_llm_client(
            model_config=model_config,
            runtime_config=runtime_config,
            enabled=enabled,
            disabled_reason=disabled_reason,
        )

    def _load_draft_patch_state(self, requirement_draft: dict):
        raw_state = ((requirement_draft.get("structured_spec_json") or {}).get("draft_patch_state") or {})
        try:
            return RequirementDraftPatchStateResponse.model_validate(raw_state)
        except ValidationError:
            return RequirementDraftPatchStateResponse()

    def _load_authoring_semantic_state(self, requirement_draft: dict):
        raw_state = ((requirement_draft.get("structured_spec_json") or {}).get("authoring_semantic_state") or {})
        try:
            return RequirementAuthoringSemanticStateResponse.model_validate(raw_state)
        except ValidationError:
            return RequirementAuthoringSemanticStateResponse()

    def _apply_patch_state_notes(self, authoring_feedback, requirement_draft: dict) -> None:
        patch_state = self._load_draft_patch_state(requirement_draft)
        notes: list[str] = []
        if patch_state.ambiguities:
            notes.extend(f"Draft patch ambiguity: {item}" for item in patch_state.ambiguities)
        if patch_state.unsupported_capability_mentions:
            notes.extend(
                f"Unsupported capability mention: {item}"
                for item in patch_state.unsupported_capability_mentions
            )
        if notes:
            authoring_feedback.warnings = list(authoring_feedback.warnings) + notes
            authoring_feedback.normalized_field_notes = list(authoring_feedback.normalized_field_notes) + notes
            if authoring_feedback.explanation is not None:
                authoring_feedback.explanation.warnings.warnings = (
                    list(authoring_feedback.explanation.warnings.warnings) + notes
                )
                authoring_feedback.explanation.warnings.warning_count = len(authoring_feedback.explanation.warnings.warnings)
        if authoring_feedback.explanation is None or not patch_state.capability_gaps:
            return
        combined_gaps = list(authoring_feedback.explanation.capability_gaps) + list(patch_state.capability_gaps)
        deduped_gaps = self._dedupe_capability_gaps(combined_gaps)
        authoring_feedback.explanation.capability_gaps = deduped_gaps
        authoring_feedback.explanation.capability_gap_summary = self.capability_registry.summarize_capability_gaps(deduped_gaps)
        presentation = authoring_feedback.explanation.presentation
        if presentation is not None:
            sections = [section for section in presentation.sections if section.section_key != "capability_gaps"]
            sections.append(
                CompilerPresentationSectionResponse(
                    section_key="capability_gaps",
                    title="Capability gaps",
                    summary="Some mentioned capabilities are unsupported or still require explicit review.",
                    severity="warning",
                    priority=4,
                    count=len(deduped_gaps),
                    items=[
                        CompilerPresentationItemResponse(
                            key=gap.gap_id,
                            label=f"{gap.gap_kind}: {gap.requested_capability_id_or_name or gap.logical_step_kind or 'capability gap'}",
                            detail=gap.reason,
                            severity=gap.severity,
                            priority=4,
                            count=len(gap.alternative_capabilities),
                        )
                        for gap in deduped_gaps
                    ],
                )
            )
            presentation.sections = sorted(sections, key=lambda item: item.priority)
            presentation.summary.warning_count += len(deduped_gaps)

    def _dedupe_capability_gaps(self, gaps: list) -> list:
        seen: set[str] = set()
        ordered: list = []
        for gap in gaps:
            if gap.gap_id in seen:
                continue
            seen.add(gap.gap_id)
            ordered.append(gap)
        return ordered

    def _apply_capability_request_presentation(self, authoring_feedback) -> None:
        explanation = authoring_feedback.explanation
        if explanation is None or explanation.presentation is None or not explanation.capability_requests:
            return
        presentation = explanation.presentation
        sections = [section for section in presentation.sections if section.section_key != "capability_requests"]
        sections.append(
            CompilerPresentationSectionResponse(
                section_key="capability_requests",
                title="Capability requests",
                summary="Formal review requests were created for capability gaps that need follow-up.",
                severity="warning",
                priority=5,
                count=len(explanation.capability_requests),
                items=[
                    CompilerPresentationItemResponse(
                        key=item.request_id,
                        label=f"{item.request_kind}: {item.requested_capability_name_or_label or item.requested_logical_step_kind or 'capability request'}",
                        detail=item.display_summary,
                        severity="warning",
                        priority=5,
                    )
                    for item in explanation.capability_requests
                ],
            )
        )
        presentation.sections = sorted(sections, key=lambda item: item.priority)

    def _apply_capability_resolution_presentation(self, authoring_feedback) -> None:
        explanation = authoring_feedback.explanation
        if explanation is None or explanation.presentation is None or explanation.capability_resolution is None:
            return
        resolution = explanation.capability_resolution
        presentation = explanation.presentation
        sections = [section for section in presentation.sections if section.section_key != "capability_resolution"]
        sections.append(
            CompilerPresentationSectionResponse(
                section_key="capability_resolution",
                title="Capability resolution",
                summary=resolution.end_user.headline,
                severity=resolution.end_user.resolution_severity,
                priority=4,
                count=resolution.end_user.linked_request_count,
                items=[
                    CompilerPresentationItemResponse(
                        key="end_user",
                        label=f"End user: {resolution.end_user.resolution_state}",
                        detail=resolution.end_user.short_reason,
                        severity=resolution.end_user.resolution_severity,
                        priority=4,
                    ),
                    CompilerPresentationItemResponse(
                        key="operator",
                        label=f"Operator: {resolution.operator.resolution_state}",
                        detail=resolution.operator.short_reason,
                        severity=resolution.operator.resolution_severity,
                        priority=5,
                    ),
                ],
            )
        )
        presentation.sections = sorted(sections, key=lambda item: item.priority)
