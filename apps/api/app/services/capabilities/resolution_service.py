from __future__ import annotations

from sqlalchemy.orm import Session

from app.db.repositories.capability_lifecycle_repository import CapabilityLifecycleRepository
from app.db.repositories.capability_proposal_repository import CapabilityProposalRepository
from app.schemas.capability_registry import (
    CapabilityActivationEligibilityResponse,
    CapabilityGapResponse,
    CapabilityLifecycleEntryResponse,
    CapabilityProposalResponse,
    CapabilityRequestResponse,
    CapabilityResolutionGovernanceRefResponse,
    CapabilityResolutionNextActionResponse,
    CapabilityResolutionRoleViewResponse,
    CapabilityResolutionRoleViewsResponse,
)
from app.services.capabilities.lifecycle_service import CapabilityLifecycleService
from app.services.capabilities.proposal_service import CapabilityProposalService
from app.services.capabilities.request_service import CapabilityRequestService


class CapabilityResolutionService:
    def __init__(self, db: Session | None) -> None:
        self.db = db
        self.request_service = CapabilityRequestService(db) if db is not None else None
        self.proposal_service = CapabilityProposalService(db) if db is not None else None
        self.lifecycle_service = CapabilityLifecycleService(db) if db is not None else None
        self.proposal_repository = CapabilityProposalRepository(db) if db is not None else None
        self.lifecycle_repository = CapabilityLifecycleRepository(db) if db is not None else None

    def build_role_views(
        self,
        *,
        project_id: str | None = None,
        capability_gaps: list[CapabilityGapResponse] | None = None,
        capability_requests: list[CapabilityRequestResponse] | None = None,
        capability_proposals: list[CapabilityProposalResponse] | None = None,
        lifecycle_entries: list[CapabilityLifecycleEntryResponse] | None = None,
    ) -> CapabilityResolutionRoleViewsResponse:
        requests = list(capability_requests or [])
        auto_progress_notes: list[str] = []

        if self.db is not None and requests:
            refreshed_requests, refreshed_proposals, refreshed_lifecycle_entries, auto_progress_notes = self._hydrate_related_objects(
                requests=requests,
            )
            requests = refreshed_requests
            proposals = refreshed_proposals
            lifecycles = refreshed_lifecycle_entries
        else:
            proposals = list(capability_proposals or [])
            lifecycles = list(lifecycle_entries or [])
            if self.db is not None and project_id is not None and not requests and not proposals and not lifecycles:
                requests = self.request_service.list_project_requests(project_id) if self.request_service is not None else []
                requests, proposals, lifecycles, auto_progress_notes = self._hydrate_related_objects(requests=requests)

        gaps = list(capability_gaps or [])
        eligibilities = self._activation_eligibilities(proposals)

        end_user = self._build_role_view(
            role="end_user",
            gaps=gaps,
            requests=requests,
            proposals=proposals,
            lifecycles=lifecycles,
            eligibilities=eligibilities,
            auto_progress_notes=auto_progress_notes,
        )
        operator = self._build_role_view(
            role="operator",
            gaps=gaps,
            requests=requests,
            proposals=proposals,
            lifecycles=lifecycles,
            eligibilities=eligibilities,
            auto_progress_notes=auto_progress_notes,
        )
        capability_owner = self._build_role_view(
            role="capability_owner",
            gaps=gaps,
            requests=requests,
            proposals=proposals,
            lifecycles=lifecycles,
            eligibilities=eligibilities,
            auto_progress_notes=auto_progress_notes,
        )
        return CapabilityResolutionRoleViewsResponse(
            default_role="end_user",
            end_user=end_user,
            operator=operator,
            capability_owner=capability_owner,
        )

    def _hydrate_related_objects(
        self,
        *,
        requests: list[CapabilityRequestResponse],
    ) -> tuple[list[CapabilityRequestResponse], list[CapabilityProposalResponse], list[CapabilityLifecycleEntryResponse], list[str]]:
        auto_progress_notes: list[str] = []
        if self.request_service is None or self.proposal_service is None or self.lifecycle_service is None:
            return requests, [], [], auto_progress_notes
        refreshed_requests = [self.request_service.get_request(item.request_id) for item in requests]
        for request in refreshed_requests:
            if (
                request.request_kind in {"generated_internal", "template_backed_internal"}
                and request.review_state.review_status in {"approved", "converted"}
                and request.linked_proposal_id is None
            ):
                proposal = self.proposal_service.generate_for_request(request.request_id)
                auto_progress_notes.append(
                    f"Auto-generated {proposal.proposal_kind} for request {request.request_id} after internal review approval."
                )
        if auto_progress_notes:
            refreshed_requests = [self.request_service.get_request(item.request_id) for item in refreshed_requests]

        proposals: list[CapabilityProposalResponse] = []
        lifecycles: list[CapabilityLifecycleEntryResponse] = []
        seen_proposals: set[str] = set()
        seen_lifecycles: set[str] = set()
        for request in refreshed_requests:
            if request.linked_proposal_id and request.linked_proposal_id not in seen_proposals:
                seen_proposals.add(request.linked_proposal_id)
                proposals.append(self.proposal_service.get_proposal(request.linked_proposal_id))
            if request.linked_capability_entry_id and request.linked_capability_entry_id not in seen_lifecycles:
                seen_lifecycles.add(request.linked_capability_entry_id)
                lifecycles.append(self.lifecycle_service.get_entry(request.linked_capability_entry_id))
        return refreshed_requests, proposals, lifecycles, auto_progress_notes

    def _activation_eligibilities(
        self,
        proposals: list[CapabilityProposalResponse],
    ) -> dict[str, CapabilityActivationEligibilityResponse]:
        if self.proposal_service is None:
            return {}
        eligibilities: dict[str, CapabilityActivationEligibilityResponse] = {}
        for proposal in proposals:
            if proposal.proposal_kind not in {"generated_internal_proposal", "template_backed_proposal"}:
                continue
            if proposal.source_lifecycle_entry_id is None:
                continue
            eligibilities[proposal.proposal_id] = self.proposal_service.get_activation_eligibility(proposal.proposal_id)
        return eligibilities

    def _build_role_view(
        self,
        *,
        role: str,
        gaps: list[CapabilityGapResponse],
        requests: list[CapabilityRequestResponse],
        proposals: list[CapabilityProposalResponse],
        lifecycles: list[CapabilityLifecycleEntryResponse],
        eligibilities: dict[str, CapabilityActivationEligibilityResponse],
        auto_progress_notes: list[str],
    ) -> CapabilityResolutionRoleViewResponse:
        active_lifecycles = [item for item in lifecycles if item.lifecycle_status == "active" and item.is_usable]
        alternatives_available = any(gap.alternative_capabilities for gap in gaps) or any(request.suggested_alternatives for request in requests)
        request_statuses = {item.review_state.review_status for item in requests}
        proposal_statuses = {item.proposal_status for item in proposals}
        external_pending = any(
            item.request_kind == "external_integration"
            and item.linked_capability_lifecycle_status != "active"
            for item in requests
        )
        ready_pending_activation = any(
            eligibility.eligible and eligibility.activation_status == "eligible"
            for eligibility in eligibilities.values()
        )
        proposal_review_pending = any(item.proposal_status in {"proposed", "under_review", "approved"} for item in proposals)
        platform_action_taken = bool(requests or proposals or active_lifecycles or auto_progress_notes)

        if active_lifecycles:
            resolution_state = "available"
            severity = "info"
            headline = "Platform support is available"
            short_reason = "At least one active capability is already usable for this need."
        elif ready_pending_activation:
            resolution_state = "ready_pending_activation"
            severity = "warning"
            headline = "Capability support is ready pending activation"
            short_reason = "A reviewed proposal passed activation checks but the capability is not active yet."
        elif proposal_review_pending:
            resolution_state = "proposal_ready_for_review"
            severity = "warning"
            headline = "Capability support is being prepared"
            short_reason = "A proposal exists and is moving through internal review before activation."
        elif external_pending:
            resolution_state = "blocked_requires_external_setup"
            severity = "blocking"
            headline = "Capability support depends on external setup"
            short_reason = "This need requires an external integration or setup step that the platform must complete."
        elif requests:
            if "under_review" in request_statuses or "under_review" in proposal_statuses:
                resolution_state = "under_platform_review"
                severity = "warning"
                headline = "Capability support is under platform review"
                short_reason = "The platform has taken action and capability work is being reviewed."
            else:
                resolution_state = "submitted_for_platform_resolution"
                severity = "warning"
                headline = "Capability support has been submitted to the platform"
                short_reason = "The platform recorded the capability need and created follow-up governance artifacts."
        elif alternatives_available:
            resolution_state = "alternative_available"
            severity = "info"
            headline = "A supported alternative is available"
            short_reason = "The requested capability is not available yet, but a registered alternative exists."
        elif gaps:
            resolution_state = "blocked_no_supported_resolution"
            severity = "blocking"
            headline = "No supported platform resolution is available yet"
            short_reason = "The platform detected a capability gap and no safe supported resolution is currently active."
        else:
            resolution_state = "available"
            severity = "info"
            headline = "No capability resolution work is currently needed"
            short_reason = "There are no unresolved capability gaps in this view."

        view = CapabilityResolutionRoleViewResponse(
            role=role,
            resolution_state=resolution_state,
            resolution_severity=severity,
            headline=headline,
            short_reason=short_reason,
            alternatives_available=alternatives_available,
            platform_action_taken=platform_action_taken,
            requires_user_action=False,
            requires_operator_action=resolution_state in {"submitted_for_platform_resolution", "under_platform_review", "blocked_requires_external_setup"},
            requires_capability_owner_action=resolution_state in {"proposal_ready_for_review", "ready_pending_activation", "blocked_requires_external_setup"},
            linked_request_count=len(requests),
            linked_active_capability_count=len(active_lifecycles),
            hidden_detail_count=len(requests) + len(proposals) + len(lifecycles),
            auto_progress_notes=list(auto_progress_notes if role != "end_user" else []),
        )
        view.next_actions = self._next_actions(
            role=role,
            resolution_state=resolution_state,
            alternatives_available=alternatives_available,
            requests=requests,
            proposals=proposals,
            eligibilities=eligibilities,
        )
        if role == "end_user":
            view.requires_user_action = resolution_state in {"alternative_available", "blocked_no_supported_resolution"}
            view.requires_operator_action = False
            view.requires_capability_owner_action = False
            view.hidden_detail_count = len(requests) + len(proposals) + len(lifecycles)
            view.detailed_governance_refs = []
        elif role == "operator":
            view.hidden_detail_count = len(proposals) + len(lifecycles)
            view.detailed_governance_refs = [
                CapabilityResolutionGovernanceRefResponse(
                    ref_kind="request",
                    ref_id=item.request_id,
                    status=item.review_state.review_status,
                    summary=item.display_summary,
                )
                for item in requests[:5]
            ]
        else:
            refs: list[CapabilityResolutionGovernanceRefResponse] = []
            refs.extend(
                CapabilityResolutionGovernanceRefResponse(
                    ref_kind="request",
                    ref_id=item.request_id,
                    status=item.review_state.review_status,
                    summary=item.display_summary,
                )
                for item in requests
            )
            refs.extend(
                CapabilityResolutionGovernanceRefResponse(
                    ref_kind="proposal",
                    ref_id=item.proposal_id,
                    status=item.proposal_status,
                    summary=item.display_summary,
                )
                for item in proposals
            )
            refs.extend(
                CapabilityResolutionGovernanceRefResponse(
                    ref_kind="lifecycle_entry",
                    ref_id=item.capability_entry_id,
                    status=item.lifecycle_status,
                    summary=item.display_summary,
                )
                for item in lifecycles
            )
            refs.extend(
                CapabilityResolutionGovernanceRefResponse(
                    ref_kind="activation",
                    ref_id=proposal_id,
                    status=eligibility.activation_status,
                    summary=", ".join(eligibility.missing_requirements) or "Activation checks passed.",
                )
                for proposal_id, eligibility in eligibilities.items()
            )
            view.detailed_governance_refs = refs
            view.hidden_detail_count = 0
        return view

    def _next_actions(
        self,
        *,
        role: str,
        resolution_state: str,
        alternatives_available: bool,
        requests: list[CapabilityRequestResponse],
        proposals: list[CapabilityProposalResponse],
        eligibilities: dict[str, CapabilityActivationEligibilityResponse],
    ) -> list[CapabilityResolutionNextActionResponse]:
        actions: list[CapabilityResolutionNextActionResponse] = []
        if resolution_state == "available":
            actions.append(
                CapabilityResolutionNextActionResponse(
                    action_key="use_available_capability",
                    title="Use the available platform capability",
                    detail="An active capability is already usable for this path.",
                    owner="platform" if role == "end_user" else "operator",
                    priority=1,
                )
            )
            return actions
        if resolution_state == "alternative_available":
            actions.append(
                CapabilityResolutionNextActionResponse(
                    action_key="use_alternative",
                    title="Use a supported alternative",
                    detail="A registered alternative exists and avoids additional platform follow-up.",
                    owner="user" if role == "end_user" else "operator",
                    priority=1,
                )
            )
        if resolution_state in {"submitted_for_platform_resolution", "under_platform_review"}:
            actions.append(
                CapabilityResolutionNextActionResponse(
                    action_key="monitor_platform_resolution",
                    title="Track platform follow-up",
                    detail="The capability need has already been recorded and is moving through platform review.",
                    owner="operator" if role != "end_user" else "platform",
                    priority=2,
                )
            )
        if resolution_state == "proposal_ready_for_review":
            actions.append(
                CapabilityResolutionNextActionResponse(
                    action_key="review_internal_proposal",
                    title="Review internal capability proposal",
                    detail="A proposal is available and needs capability-owner review before activation.",
                    owner="capability_owner" if role != "end_user" else "platform",
                    priority=1,
                )
            )
        if resolution_state == "ready_pending_activation":
            actions.append(
                CapabilityResolutionNextActionResponse(
                    action_key="activate_capability",
                    title="Run explicit capability activation",
                    detail="Activation checks pass, but the lifecycle entry is still not active.",
                    owner="capability_owner" if role != "end_user" else "platform",
                    priority=1,
                )
            )
        if resolution_state == "blocked_requires_external_setup":
            actions.append(
                CapabilityResolutionNextActionResponse(
                    action_key="complete_external_setup",
                    title="Complete external setup and policy review",
                    detail="External integration requirements still block activation.",
                    owner="operator" if role != "end_user" else "platform",
                    priority=1,
                )
            )
        if resolution_state == "blocked_no_supported_resolution":
            actions.append(
                CapabilityResolutionNextActionResponse(
                    action_key="request_platform_support",
                    title="Request platform support or revise scope",
                    detail="There is no active supported resolution for this capability path yet.",
                    owner="user" if role == "end_user" else "operator",
                    priority=1,
                )
            )
        if role == "capability_owner":
            for proposal in proposals[:3]:
                if proposal.proposal_id in eligibilities and eligibilities[proposal.proposal_id].activation_status == "eligible":
                    actions.append(
                        CapabilityResolutionNextActionResponse(
                            action_key=f"activate:{proposal.proposal_id}",
                            title=f"Activate {proposal.capability_id_candidate}",
                            detail="Deterministic activation checks pass for this proposal.",
                            owner="capability_owner",
                            priority=1,
                        )
                    )
            if requests:
                actions.append(
                    CapabilityResolutionNextActionResponse(
                        action_key="review_request_chain",
                        title="Inspect full governance chain",
                        detail="Request, proposal, lifecycle, and activation details remain available for audit.",
                        owner="capability_owner",
                        priority=3,
                    )
                )
        return actions[:4]
