from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models import CapabilityRequest
from app.db.repositories.capability_lifecycle_repository import CapabilityLifecycleRepository
from app.db.repositories.capability_materialization_repository import CapabilityMaterializationRepository
from app.db.repositories.capability_proposal_repository import CapabilityProposalRepository
from app.db.repositories.capability_request_repository import CapabilityRequestRepository
from app.schemas.capability_registry import (
    CapabilityGapResponse,
    CapabilityRequestClassificationResponse,
    CapabilityRequestContextResponse,
    CapabilityRequestProjectSummaryResponse,
    CapabilityRequestResponse,
    CapabilityRequestReviewStateResponse,
    CapabilityRequestSummaryResponse,
)
from app.services.capabilities.registry import CapabilityRegistryService


class CapabilityRequestService:
    EXTERNAL_KEYWORDS = {
        "slack",
        "jira",
        "notion",
        "github",
        "gitlab",
        "salesforce",
        "zendesk",
        "hubspot",
        "stripe",
        "twilio",
        "gmail",
        "calendar",
        "webhook",
        "connector",
        "api",
        "service",
        "provider",
        "mcp",
    }

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = CapabilityRequestRepository(db)
        self.registry = CapabilityRegistryService()
        self.lifecycle_repository = CapabilityLifecycleRepository(db)
        self.materialization_repository = CapabilityMaterializationRepository(db)
        self.proposal_repository = CapabilityProposalRepository(db)

    def list_project_requests(self, project_id: str) -> list[CapabilityRequestResponse]:
        return [self._serialize(item) for item in self.repository.list_by_project(project_id)]

    def get_project_summary(self, project_id: str) -> CapabilityRequestProjectSummaryResponse:
        requests = self.list_project_requests(project_id)
        from app.services.capabilities.resolution_service import CapabilityResolutionService

        resolution = CapabilityResolutionService(self.db).build_role_views(
            project_id=project_id,
            capability_requests=requests,
        )
        refreshed_requests = self.list_project_requests(project_id)
        actionable = [
            item
            for item in refreshed_requests
            if item.review_state.review_status in {"proposed", "under_review"}
        ]
        return CapabilityRequestProjectSummaryResponse(
            project_id=project_id,
            summary=self.summarize_requests(refreshed_requests),
            latest_requests=refreshed_requests[:5],
            latest_actionable_requests=actionable[:5],
            resolution=resolution,
        )

    def get_request(self, request_id: str) -> CapabilityRequestResponse:
        request = self.repository.get_by_id(request_id)
        if request is None:
            raise AppException("Capability request not found", status_code=404)
        return self._serialize(request)

    def update_review_state(self, request_id: str, *, review_status: str, review_notes: list[str] | None = None) -> CapabilityRequestResponse:
        request = self.repository.get_by_id(request_id)
        if request is None:
            raise AppException("Capability request not found", status_code=404)
        request.review_status = review_status
        if review_notes is not None:
            request.review_notes_json = list(review_notes)
        self.db.commit()
        self.db.refresh(request)
        return self._serialize(request)

    def ensure_requests_for_gaps(
        self,
        *,
        project_id: str,
        gaps: list[CapabilityGapResponse],
        source_requirement_id: str | None = None,
        source_compile_record_id: str | None = None,
        source_session_id: str | None = None,
        source_blueprint_id: str | None = None,
        source_build_id: str | None = None,
    ) -> list[CapabilityRequestResponse]:
        created_or_existing: list[CapabilityRequestResponse] = []
        for gap in gaps:
            if not self._should_create_request(gap):
                continue
            existing = self.repository.get_by_source_gap(
                project_id=project_id,
                source_gap_id=gap.gap_id,
                source_stage=gap.stage,
            )
            if existing is not None:
                created_or_existing.append(self._serialize(existing))
                continue
            classification = self.classify_gap(gap)
            record = CapabilityRequest(
                project_id=project_id,
                source_gap_id=gap.gap_id,
                source_stage=gap.stage,
                source_requirement_id=source_requirement_id,
                source_compile_record_id=source_compile_record_id,
                source_session_id=source_session_id,
                source_blueprint_id=source_blueprint_id,
                source_build_id=source_build_id,
                request_kind=classification.request_kind,
                requested_logical_step_kind=gap.logical_step_kind,
                requested_capability_purpose=(
                    gap.suggested_request.requested_capability_purpose
                    if gap.suggested_request is not None
                    else gap.reason
                ),
                requested_capability_name_or_label=gap.requested_capability_id_or_name,
                suggested_alternatives_json=[item.model_dump(mode="python") for item in gap.alternative_capabilities],
                supporting_gap_summary_json=self.registry.summarize_capability_gaps([gap]).model_dump(mode="python"),
                example_context_json={
                    "context_ref": gap.context_ref or "",
                    "stage": gap.stage,
                    "logical_step_kind": gap.logical_step_kind or "",
                },
                review_reason=gap.reason,
                review_status="proposed",
                review_notes_json=[],
                implementation_route_hint=classification.implementation_route_hint,
                requires_human_owner=classification.requires_human_owner,
            )
            self.repository.create(record)
            created_or_existing.append(self._serialize(record))
        if created_or_existing:
            self.db.commit()
        return created_or_existing

    def summarize_requests(self, requests: list[CapabilityRequestResponse]) -> CapabilityRequestSummaryResponse:
        summary = CapabilityRequestSummaryResponse(total_count=len(requests))
        for request in requests:
            status = request.review_state.review_status
            summary.by_kind_counts[request.request_kind] = summary.by_kind_counts.get(request.request_kind, 0) + 1
            summary.by_stage_counts[request.source_stage] = summary.by_stage_counts.get(request.source_stage, 0) + 1
            summary.by_status_counts[status] = summary.by_status_counts.get(status, 0) + 1
            if status == "proposed":
                summary.proposed_count += 1
            elif status == "under_review":
                summary.under_review_count += 1
            elif status == "approved":
                summary.approved_count += 1
            elif status == "rejected":
                summary.rejected_count += 1
            elif status == "converted":
                summary.converted_count += 1
            if status in {"proposed", "under_review"}:
                summary.actionable_count += 1
        summary.headlines = [
            item.display_summary
            for item in requests[:3]
        ]
        return summary

    def classify_gap(self, gap: CapabilityGapResponse) -> CapabilityRequestClassificationResponse:
        requested = (gap.requested_capability_id_or_name or gap.normalized_requested_capability_id or "").lower()
        capability = None
        if gap.normalized_requested_capability_id:
            capability = self.registry.get_capability(gap.normalized_requested_capability_id)
        elif gap.requested_capability_id_or_name:
            capability = self.registry.get_capability(gap.requested_capability_id_or_name)
        if capability is not None:
            if capability.template_backed_only or gap.gap_kind == "template_capability_unrealized":
                return CapabilityRequestClassificationResponse(
                    request_kind="template_backed_internal",
                    implementation_route_hint="add_template_variant",
                    requires_human_owner=True,
                    classification_reason="Gap maps to a registered template-backed capability path that still requires review.",
                )
            if capability.capability_type in {"external_tool", "notification"}:
                return CapabilityRequestClassificationResponse(
                    request_kind="external_integration",
                    implementation_route_hint="integrate_external_service",
                    requires_human_owner=True,
                    classification_reason="Gap references an external/provider-backed capability path.",
                )
            if gap.gap_kind == "review_needed":
                return CapabilityRequestClassificationResponse(
                    request_kind="manual_review_only",
                    implementation_route_hint="manual_triage",
                    requires_human_owner=True,
                    classification_reason="Gap does not require a new capability yet, but does require explicit manual review.",
                )
            return CapabilityRequestClassificationResponse(
                request_kind="generated_internal",
                implementation_route_hint="propose_generated_helper",
                requires_human_owner=True,
                classification_reason="Gap points to an internal capability path that is not currently realized.",
            )
        if any(keyword in requested for keyword in self.EXTERNAL_KEYWORDS):
            return CapabilityRequestClassificationResponse(
                request_kind="external_integration",
                implementation_route_hint="integrate_external_service",
                requires_human_owner=True,
                classification_reason="Requested capability appears to describe an external integration or provider dependency.",
            )
        if gap.suggested_request is not None and gap.suggested_request.request_kind == "template_request":
            return CapabilityRequestClassificationResponse(
                request_kind="template_backed_internal",
                implementation_route_hint="add_template_variant",
                requires_human_owner=True,
                classification_reason="Gap aligns with a template-backed internal extension route.",
            )
        if gap.gap_kind == "review_needed":
            return CapabilityRequestClassificationResponse(
                request_kind="manual_review_only",
                implementation_route_hint="manual_triage",
                requires_human_owner=True,
                classification_reason="Gap is best handled as review-only until a clearer implementation path exists.",
            )
        return CapabilityRequestClassificationResponse(
            request_kind="generated_internal",
            implementation_route_hint="propose_generated_helper",
            requires_human_owner=True,
            classification_reason="Gap is internal but not currently covered by a registered capability or template path.",
        )

    def _should_create_request(self, gap: CapabilityGapResponse) -> bool:
        if gap.gap_kind in {"missing_capability", "missing_binding", "unsupported_binding", "template_capability_unrealized"}:
            return True
        if gap.gap_kind == "unsupported_mention":
            return True
        return gap.gap_kind == "review_needed" and gap.suggested_request is not None and not gap.alternative_capabilities

    def _serialize(self, request: CapabilityRequest) -> CapabilityRequestResponse:
        linked_entry = self.lifecycle_repository.get_by_source_request(request.id)
        linked_proposal = self.proposal_repository.get_by_source_request(request.id)
        linked_materialization = (
            self.materialization_repository.get_by_source_lifecycle_entry(linked_entry.id)
            if linked_entry is not None
            else None
        )
        classification = CapabilityRequestClassificationResponse(
            request_kind=request.request_kind,
            implementation_route_hint=request.implementation_route_hint,
            requires_human_owner=request.requires_human_owner,
            classification_reason=(
                f"Classified as {request.request_kind} with route hint {request.implementation_route_hint}."
            ),
        )
        context = CapabilityRequestContextResponse(
            source_gap_id=request.source_gap_id,
            source_stage=request.source_stage,
            source_project_id=request.project_id,
            source_requirement_id=request.source_requirement_id,
            source_compile_record_id=request.source_compile_record_id,
            source_session_id=request.source_session_id,
            source_blueprint_id=request.source_blueprint_id,
            source_build_id=request.source_build_id,
            example_context=dict(request.example_context_json or {}),
        )
        return CapabilityRequestResponse(
            request_id=request.id,
            request_kind=request.request_kind,
            source_stage=request.source_stage,
            implementation_route_hint=request.implementation_route_hint,
            requires_human_owner=request.requires_human_owner,
            display_summary=(
                f"{request.request_kind} from {request.source_stage}: "
                f"{request.requested_capability_name_or_label or request.requested_logical_step_kind or 'capability request'}"
            ),
            classification=classification,
            context=context,
            requested_logical_step_kind=request.requested_logical_step_kind,
            requested_capability_purpose=request.requested_capability_purpose,
            requested_capability_name_or_label=request.requested_capability_name_or_label,
            suggested_alternatives=list(request.suggested_alternatives_json or []),
            supporting_gap_summary=request.supporting_gap_summary_json or {},
            review_reason=request.review_reason,
            review_state=CapabilityRequestReviewStateResponse(
                review_status=request.review_status,
                review_notes=list(request.review_notes_json or []),
            ),
            linked_capability_entry_id=linked_entry.id if linked_entry is not None else None,
            linked_capability_lifecycle_status=linked_entry.lifecycle_status if linked_entry is not None else None,
            linked_proposal_id=linked_proposal.id if linked_proposal is not None else None,
            linked_proposal_status=linked_proposal.proposal_status if linked_proposal is not None else None,
            linked_materialization_id=linked_materialization.id if linked_materialization is not None else None,
            linked_materialization_status=linked_materialization.materialization_status if linked_materialization is not None else None,
            created_at=request.created_at.isoformat() if request.created_at else None,
            updated_at=request.updated_at.isoformat() if request.updated_at else None,
        )
