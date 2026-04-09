from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models import CapabilityLifecycleEntry, CapabilityProposal
from app.db.repositories.capability_lifecycle_repository import CapabilityLifecycleRepository
from app.db.repositories.capability_materialization_repository import CapabilityMaterializationRepository
from app.db.repositories.capability_proposal_repository import CapabilityProposalRepository
from app.schemas.capability_registry import (
    CapabilityActivationCheckResponse,
    CapabilityActivationEligibilityResponse,
    CapabilityActivationResultResponse,
    CapabilityProposalImplementationResponse,
    CapabilityProposalActivationRequest,
    CapabilityProposalProjectSummaryResponse,
    CapabilityProposalResponse,
    CapabilityProposalReviewStateResponse,
    CapabilityProposalReviewUpdateRequest,
    CapabilityProposalSummaryResponse,
    CapabilityType,
    CapabilityRequestResponse,
)
from app.services.capabilities.registry import CapabilityRegistryService
from app.services.capabilities.request_service import CapabilityRequestService


class CapabilityProposalService:
    VALID_CONTEXTS = {"authoring", "blueprint_planning", "build_planning", "runtime"}
    TEMPLATE_EXECUTION_KIND_BY_STEP_KIND = {
        "transform": "record_normalizer",
        "persist": "artifact_writer_template",
        "validate": "schema_guard_template",
    }

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = CapabilityProposalRepository(db)
        self.lifecycle_repository = CapabilityLifecycleRepository(db)
        self.materialization_repository = CapabilityMaterializationRepository(db)
        self.request_service = CapabilityRequestService(db)
        self.registry = CapabilityRegistryService()

    def list_project_proposals(self, project_id: str) -> list[CapabilityProposalResponse]:
        return [self._serialize(item) for item in self.repository.list_by_project(project_id)]

    def get_project_summary(self, project_id: str) -> CapabilityProposalProjectSummaryResponse:
        proposals = self.list_project_proposals(project_id)
        actionable = [item for item in proposals if item.proposal_status in {"proposed", "under_review"}]
        return CapabilityProposalProjectSummaryResponse(
            project_id=project_id,
            summary=self.summarize_proposals(proposals),
            latest_proposals=proposals[:5],
            latest_actionable_proposals=actionable[:5],
        )

    def get_proposal(self, proposal_id: str) -> CapabilityProposalResponse:
        proposal = self.repository.get_by_id(proposal_id)
        if proposal is None:
            raise AppException("Capability proposal not found", status_code=404)
        return self._serialize(proposal)

    def generate_for_request(self, request_id: str) -> CapabilityProposalResponse:
        request = self.request_service.get_request(request_id)
        self._ensure_eligible(request)
        existing = self.repository.get_by_source_request(request_id)
        if existing is not None:
            return self._serialize(existing)
        lifecycle_entry = self.lifecycle_repository.get_by_source_request(request_id)
        if request.review_state.review_status == "converted" and lifecycle_entry is None:
            raise AppException(
                "Capability request is marked converted but no lifecycle entry is linked.",
                status_code=409,
            )
        proposal = self.repository.create(self._build_proposal(request, lifecycle_entry))
        self.db.commit()
        self.db.refresh(proposal)
        return self._serialize(proposal)

    def update_review_state(self, proposal_id: str, payload: CapabilityProposalReviewUpdateRequest) -> CapabilityProposalResponse:
        proposal = self.repository.get_by_id(proposal_id)
        if proposal is None:
            raise AppException("Capability proposal not found", status_code=404)
        proposal.proposal_status = payload.proposal_status
        proposal.review_notes_json = list(payload.review_notes or [])
        self.db.commit()
        self.db.refresh(proposal)
        return self._serialize(proposal)

    def get_activation_eligibility(self, proposal_id: str) -> CapabilityActivationEligibilityResponse:
        proposal = self.repository.get_by_id(proposal_id)
        if proposal is None:
            raise AppException("Capability proposal not found", status_code=404)
        lifecycle_entry = self._require_lifecycle_entry(proposal)
        return self._build_activation_eligibility(proposal, lifecycle_entry)

    def activate_proposal(
        self,
        proposal_id: str,
        payload: CapabilityProposalActivationRequest | None = None,
    ) -> CapabilityActivationResultResponse:
        proposal = self.repository.get_by_id(proposal_id)
        if proposal is None:
            raise AppException("Capability proposal not found", status_code=404)
        lifecycle_entry = self._require_lifecycle_entry(proposal)
        eligibility = self._build_activation_eligibility(proposal, lifecycle_entry)
        if eligibility.activation_status == "already_active":
            if proposal.proposal_status != "converted":
                proposal.proposal_status = "converted"
                self.db.commit()
                self.db.refresh(proposal)
            return CapabilityActivationResultResponse(
                proposal=self._serialize(proposal),
                lifecycle_entry=self._serialize_lifecycle_entry(lifecycle_entry),
                eligibility=eligibility,
                activated=True,
                idempotent=True,
                activated_at=lifecycle_entry.activated_at.isoformat() if lifecycle_entry.activated_at else None,
                enabled_contexts_applied=list(lifecycle_entry.enabled_contexts_json or []),
                resulting_capability_id=lifecycle_entry.capability_id,
            )
        if not eligibility.eligible:
            return CapabilityActivationResultResponse(
                proposal=self._serialize(proposal),
                lifecycle_entry=self._serialize_lifecycle_entry(lifecycle_entry),
                eligibility=eligibility,
                activated=False,
                idempotent=False,
                activated_at=None,
                enabled_contexts_applied=[],
                resulting_capability_id=lifecycle_entry.capability_id,
            )

        metadata_draft = dict(proposal.capability_metadata_draft_json or {})
        enabled_contexts = [item for item in metadata_draft.get("allowed_contexts", []) if item in self.VALID_CONTEXTS]
        supported_step_kinds = list(metadata_draft.get("supported_step_kinds", []) or lifecycle_entry.supported_step_kinds_json or [])
        activation_notes = list(eligibility.activation_notes)
        if payload is not None:
            activation_notes.extend(note for note in payload.activation_notes if note)

        lifecycle_entry.lifecycle_status = "active"
        lifecycle_entry.capability_type = self._capability_type_value(metadata_draft.get("capability_type"), lifecycle_entry.capability_type)
        lifecycle_entry.name = str(metadata_draft.get("name") or lifecycle_entry.name)
        lifecycle_entry.description = str(metadata_draft.get("description") or lifecycle_entry.description)
        lifecycle_entry.enabled_contexts_json = enabled_contexts
        lifecycle_entry.supported_step_kinds_json = supported_step_kinds
        lifecycle_entry.input_contract_summary = str(metadata_draft.get("input_contract_summary") or lifecycle_entry.input_contract_summary)
        lifecycle_entry.output_contract_summary = str(metadata_draft.get("output_contract_summary") or lifecycle_entry.output_contract_summary)
        lifecycle_entry.template_backed_only = bool(metadata_draft.get("template_backed_only", lifecycle_entry.template_backed_only))
        lifecycle_entry.activation_notes_json = activation_notes
        lifecycle_entry.activated_at = datetime.now(timezone.utc)
        lifecycle_entry.review_notes_json = self._merge_notes(
            lifecycle_entry.review_notes_json,
            proposal.review_notes_json,
            activation_notes,
        )

        proposal.proposal_status = "converted"
        if activation_notes:
            proposal.review_notes_json = self._merge_notes(proposal.review_notes_json, activation_notes)

        self.db.commit()
        self.db.refresh(lifecycle_entry)
        self.db.refresh(proposal)
        eligibility = self._build_activation_eligibility(proposal, lifecycle_entry)
        return CapabilityActivationResultResponse(
            proposal=self._serialize(proposal),
            lifecycle_entry=self._serialize_lifecycle_entry(lifecycle_entry),
            eligibility=eligibility,
            activated=True,
            idempotent=False,
            activated_at=lifecycle_entry.activated_at.isoformat() if lifecycle_entry.activated_at else None,
            enabled_contexts_applied=list(lifecycle_entry.enabled_contexts_json or []),
            resulting_capability_id=lifecycle_entry.capability_id,
        )

    def summarize_proposals(self, proposals: list[CapabilityProposalResponse]) -> CapabilityProposalSummaryResponse:
        summary = CapabilityProposalSummaryResponse(total_count=len(proposals))
        for proposal in proposals:
            status = proposal.proposal_status
            summary.by_kind_counts[proposal.proposal_kind] = summary.by_kind_counts.get(proposal.proposal_kind, 0) + 1
            summary.by_status_counts[status] = summary.by_status_counts.get(status, 0) + 1
            if status == "proposed":
                summary.proposed_count += 1
            elif status == "under_review":
                summary.under_review_count += 1
            elif status == "approved":
                summary.approved_count += 1
            elif status == "rejected":
                summary.rejected_count += 1
            elif status == "superseded":
                summary.superseded_count += 1
            elif status == "converted":
                summary.converted_count += 1
            if status in {"proposed", "under_review"}:
                summary.actionable_count += 1
        summary.headlines = [item.display_summary for item in proposals[:3]]
        return summary

    def get_linked_proposal_for_request(self, request_id: str) -> CapabilityProposalResponse | None:
        proposal = self.repository.get_by_source_request(request_id)
        return self._serialize(proposal) if proposal is not None else None

    def get_linked_proposal_for_lifecycle_entry(self, entry_id: str) -> CapabilityProposalResponse | None:
        proposal = self.repository.get_by_source_lifecycle_entry(entry_id)
        return self._serialize(proposal) if proposal is not None else None

    def _ensure_eligible(self, request: CapabilityRequestResponse) -> None:
        if request.request_kind not in {"generated_internal", "template_backed_internal"}:
            raise AppException(
                "Capability proposal generation is only supported for generated_internal and template_backed_internal requests.",
                status_code=409,
            )
        if request.review_state.review_status not in {"approved", "converted"}:
            raise AppException(
                "Capability proposals can only be generated from approved or converted requests.",
                status_code=409,
            )

    def _build_proposal(
        self,
        request: CapabilityRequestResponse,
        lifecycle_entry: CapabilityLifecycleEntry | None,
    ) -> CapabilityProposal:
        capability_id_candidate = lifecycle_entry.capability_id if lifecycle_entry is not None else self._derive_capability_id(request)
        if request.request_kind == "template_backed_internal":
            implementation = self._build_template_backed_implementation(request, lifecycle_entry, capability_id_candidate)
            proposal_kind = "template_backed_proposal"
        else:
            implementation = self._build_generated_internal_implementation(request, lifecycle_entry, capability_id_candidate)
            proposal_kind = "generated_internal_proposal"
        return CapabilityProposal(
            project_id=request.context.source_project_id,
            source_request_id=request.request_id,
            source_lifecycle_entry_id=lifecycle_entry.id if lifecycle_entry is not None else None,
            proposal_kind=proposal_kind,
            proposal_status="proposed",
            request_kind_origin=request.request_kind,
            capability_id_candidate=capability_id_candidate,
            logical_step_kind=request.requested_logical_step_kind,
            capability_purpose=request.requested_capability_purpose,
            implementation_route=request.implementation_route_hint,
            io_contract_summary=implementation.io_contract_summary,
            configuration_contract_summary=implementation.configuration_contract_summary,
            review_notes_json=list(implementation.review_notes),
            test_suggestions_json=list(implementation.test_suggestions),
            capability_metadata_draft_json=dict(implementation.capability_metadata_draft),
            code_stub=implementation.code_stub,
            template_stub=implementation.template_stub,
            bounded_execution_kind=implementation.bounded_execution_kind,
            requires_human_review=True,
        )

    def _build_generated_internal_implementation(
        self,
        request: CapabilityRequestResponse,
        lifecycle_entry: CapabilityLifecycleEntry | None,
        capability_id_candidate: str,
    ) -> CapabilityProposalImplementationResponse:
        logical_step_kind = request.requested_logical_step_kind
        step_definition = self.registry.get_step_kind(logical_step_kind) if logical_step_kind else None
        io_contract_summary = self._io_contract_summary(request, lifecycle_entry)
        configuration_contract_summary = (
            "Keep configuration deterministic and reviewable. "
            "Limit configuration to bounded thresholds, routing knobs, or scoring rules without external credentials."
        )
        review_notes = self._proposal_review_notes(request, lifecycle_entry)
        test_suggestions = self._test_suggestions(request.request_kind, logical_step_kind)
        metadata_draft = self._metadata_draft(
            request=request,
            lifecycle_entry=lifecycle_entry,
            capability_id_candidate=capability_id_candidate,
            capability_type="deterministic",
            template_backed_only=False,
            io_contract_summary=io_contract_summary,
        )
        if logical_step_kind:
            metadata_draft["bounded_runtime_pattern"] = logical_step_kind
        function_name = capability_id_candidate
        code_stub = "\n".join(
            [
                "# Proposal stub only. Not active, registered, or executable.",
                f"def {function_name}(input_payload: dict, config: dict) -> dict:",
                '    """',
                f"    Purpose: {request.requested_capability_purpose}",
                f"    Step kind: {logical_step_kind or 'unspecified'}",
                f"    Inputs/outputs: {io_contract_summary}",
                '    """',
                '    raise NotImplementedError("Proposal artifact only; implementation requires separate review and activation.")',
            ]
        )
        if step_definition is not None and step_definition.step_kind == "rank":
            configuration_contract_summary = (
                "Accept deterministic ranking rubric configuration, weight definitions, and bounded tie-break rules. "
                "Do not assume external provider setup."
            )
        return CapabilityProposalImplementationResponse(
            io_contract_summary=io_contract_summary,
            configuration_contract_summary=configuration_contract_summary,
            review_notes=review_notes,
            test_suggestions=test_suggestions,
            capability_metadata_draft=metadata_draft,
            code_stub=code_stub,
        )

    def _build_template_backed_implementation(
        self,
        request: CapabilityRequestResponse,
        lifecycle_entry: CapabilityLifecycleEntry | None,
        capability_id_candidate: str,
    ) -> CapabilityProposalImplementationResponse:
        logical_step_kind = request.requested_logical_step_kind
        bounded_execution_kind = self.TEMPLATE_EXECUTION_KIND_BY_STEP_KIND.get(logical_step_kind or "", "manual_template_selection_required")
        io_contract_summary = self._io_contract_summary(request, lifecycle_entry)
        configuration_contract_summary = (
            "Propose a bounded template execution path only. "
            "Configuration must remain limited to reviewed template fields and must not become arbitrary executable code."
        )
        review_notes = self._proposal_review_notes(request, lifecycle_entry)
        test_suggestions = self._test_suggestions(request.request_kind, logical_step_kind)
        metadata_draft = self._metadata_draft(
            request=request,
            lifecycle_entry=lifecycle_entry,
            capability_id_candidate=capability_id_candidate,
            capability_type="code_template",
            template_backed_only=True,
            io_contract_summary=io_contract_summary,
        )
        metadata_draft["linked_static_registry_capability_id"] = (
            lifecycle_entry.linked_static_registry_capability_id
            if lifecycle_entry is not None
            else request.requested_capability_name_or_label
        )
        metadata_draft["bounded_template_runtime_kind"] = bounded_execution_kind
        template_stub = json.dumps(
            {
                "proposal_only": True,
                "template_execution_kind": bounded_execution_kind,
                "logical_step_kind": logical_step_kind,
                "required_configuration_fields": ["input_payload", "output_contract", "review_notes"],
                "implementation_constraints": [
                    "bounded_template_only",
                    "manual_review_required",
                    "not_active_until_lifecycle_activation",
                ],
            },
            indent=2,
            sort_keys=True,
        )
        return CapabilityProposalImplementationResponse(
            io_contract_summary=io_contract_summary,
            configuration_contract_summary=configuration_contract_summary,
            review_notes=review_notes,
            test_suggestions=test_suggestions,
            capability_metadata_draft=metadata_draft,
            template_stub=template_stub,
            bounded_execution_kind=bounded_execution_kind,
        )

    def _io_contract_summary(
        self,
        request: CapabilityRequestResponse,
        lifecycle_entry: CapabilityLifecycleEntry | None,
    ) -> str:
        if lifecycle_entry is not None and (lifecycle_entry.input_contract_summary or lifecycle_entry.output_contract_summary):
            return (
                f"Inputs: {lifecycle_entry.input_contract_summary or 'structured payload'}; "
                f"Outputs: {lifecycle_entry.output_contract_summary or 'structured result payload'}."
            )
        if request.requested_logical_step_kind:
            step_definition = self.registry.get_step_kind(request.requested_logical_step_kind)
            if step_definition is not None:
                return (
                    f"Inputs: {', '.join(step_definition.typical_inputs) or 'structured payload'}; "
                    f"Outputs: {', '.join(step_definition.typical_outputs) or 'structured result payload'}."
                )
        return "Inputs: structured payload; Outputs: deterministic structured result payload."

    def _proposal_review_notes(
        self,
        request: CapabilityRequestResponse,
        lifecycle_entry: CapabilityLifecycleEntry | None,
    ) -> list[str]:
        notes = [
            "Proposal artifact only. Review and implementation are required before any activation.",
            f"Source request review status when generated: {request.review_state.review_status}.",
        ]
        if lifecycle_entry is not None:
            notes.append(f"Linked lifecycle status at generation time: {lifecycle_entry.lifecycle_status}.")
        notes.extend(note for note in request.review_state.review_notes if note)
        return notes

    def _test_suggestions(self, request_kind: str, logical_step_kind: str | None) -> list[str]:
        suggestions = [
            "Add contract-validation tests for required input and output fields.",
            "Add regression tests showing the capability remains inactive until lifecycle activation is explicit.",
        ]
        if request_kind == "template_backed_internal":
            suggestions.append("Add template-boundary tests proving only the approved execution kind is accepted.")
        else:
            suggestions.append("Add deterministic behavior tests covering repeated inputs and bounded configuration changes.")
        if logical_step_kind == "rank":
            suggestions.append("Add ranking rubric tests for tie-breaking and weighted scoring edge cases.")
        elif logical_step_kind == "transform":
            suggestions.append("Add normalization tests for malformed input, alias handling, and output contract preservation.")
        elif logical_step_kind == "persist":
            suggestions.append("Add artifact contract tests for persistence metadata and output completeness.")
        return suggestions

    def _metadata_draft(
        self,
        *,
        request: CapabilityRequestResponse,
        lifecycle_entry: CapabilityLifecycleEntry | None,
        capability_id_candidate: str,
        capability_type: CapabilityType | str,
        template_backed_only: bool,
        io_contract_summary: str,
    ) -> dict[str, object]:
        name = (
            lifecycle_entry.name
            if lifecycle_entry is not None and lifecycle_entry.name
            else request.requested_capability_name_or_label or capability_id_candidate
        )
        description = (
            lifecycle_entry.description
            if lifecycle_entry is not None and lifecycle_entry.description
            else request.review_reason
        )
        allowed_contexts = (
            list(lifecycle_entry.enabled_contexts_json or [])
            if lifecycle_entry is not None and lifecycle_entry.enabled_contexts_json is not None
            else ["blueprint_planning", "build_planning", "runtime"]
        )
        return {
            "capability_id": capability_id_candidate,
            "name": name,
            "description": description,
            "supported_step_kinds": [request.requested_logical_step_kind] if request.requested_logical_step_kind else [],
            "capability_type": capability_type,
            "allowed_contexts": allowed_contexts,
            "input_contract_summary": self._extract_contract_half(io_contract_summary, prefix="Inputs:"),
            "output_contract_summary": self._extract_contract_half(io_contract_summary, prefix="Outputs:"),
            "requires_review": True,
            "enabled": False,
            "template_backed_only": template_backed_only,
            "request_kind_origin": request.request_kind,
            "implementation_route_hint": request.implementation_route_hint,
        }

    def _derive_capability_id(self, request: CapabilityRequestResponse) -> str:
        base = request.requested_capability_name_or_label or request.requested_logical_step_kind or request.request_id
        slug = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "capability"
        prefix = "tmpl" if request.request_kind == "template_backed_internal" else "gen"
        return f"{prefix}_{slug}"

    def _serialize(self, proposal: CapabilityProposal) -> CapabilityProposalResponse:
        lifecycle_entry = (
            self.lifecycle_repository.get_by_id(proposal.source_lifecycle_entry_id)
            if proposal.source_lifecycle_entry_id
            else None
        )
        linked_materialization = self.materialization_repository.get_by_source_proposal(proposal.id)
        return CapabilityProposalResponse(
            proposal_id=proposal.id,
            source_request_id=proposal.source_request_id,
            source_lifecycle_entry_id=proposal.source_lifecycle_entry_id,
            proposal_kind=proposal.proposal_kind,
            proposal_status=proposal.proposal_status,
            request_kind_origin=proposal.request_kind_origin,
            linked_lifecycle_status=lifecycle_entry.lifecycle_status if lifecycle_entry is not None else None,
            capability_id_candidate=proposal.capability_id_candidate,
            logical_step_kind=proposal.logical_step_kind,
            capability_purpose=proposal.capability_purpose,
            implementation_route=proposal.implementation_route,
            requires_human_review=proposal.requires_human_review,
            linked_materialization_id=linked_materialization.id if linked_materialization is not None else None,
            linked_materialization_status=linked_materialization.materialization_status if linked_materialization is not None else None,
            display_summary=f"{proposal.proposal_kind} for {proposal.capability_id_candidate} ({proposal.proposal_status})",
            implementation=CapabilityProposalImplementationResponse(
                io_contract_summary=proposal.io_contract_summary,
                configuration_contract_summary=proposal.configuration_contract_summary,
                review_notes=list(proposal.review_notes_json or []),
                test_suggestions=list(proposal.test_suggestions_json or []),
                capability_metadata_draft=dict(proposal.capability_metadata_draft_json or {}),
                code_stub=proposal.code_stub,
                template_stub=proposal.template_stub,
                bounded_execution_kind=proposal.bounded_execution_kind,
            ),
            review_state=CapabilityProposalReviewStateResponse(
                proposal_status=proposal.proposal_status,
                review_notes=list(proposal.review_notes_json or []),
            ),
            created_at=proposal.created_at.isoformat() if proposal.created_at else None,
            updated_at=proposal.updated_at.isoformat() if proposal.updated_at else None,
        )

    def _serialize_lifecycle_entry(self, lifecycle_entry: CapabilityLifecycleEntry):
        from app.services.capabilities.lifecycle_service import CapabilityLifecycleService

        return CapabilityLifecycleService(self.db)._serialize(lifecycle_entry)  # noqa: SLF001

    def _require_lifecycle_entry(self, proposal: CapabilityProposal) -> CapabilityLifecycleEntry:
        if not proposal.source_lifecycle_entry_id:
            raise AppException("Capability proposal is not linked to a lifecycle entry.", status_code=409)
        lifecycle_entry = self.lifecycle_repository.get_by_id(proposal.source_lifecycle_entry_id)
        if lifecycle_entry is None:
            raise AppException("Linked capability lifecycle entry was not found.", status_code=409)
        return lifecycle_entry

    def _build_activation_eligibility(
        self,
        proposal: CapabilityProposal,
        lifecycle_entry: CapabilityLifecycleEntry,
    ) -> CapabilityActivationEligibilityResponse:
        checks: list[CapabilityActivationCheckResponse] = []
        missing_requirements: list[str] = []
        activation_notes: list[str] = []

        def add_check(check_key: str, passed: bool, detail: str, *, missing_requirement: str | None = None) -> None:
            checks.append(CapabilityActivationCheckResponse(check_key=check_key, passed=passed, detail=detail))
            if not passed and missing_requirement is not None:
                missing_requirements.append(missing_requirement)

        add_check(
            "proposal_kind_supported",
            proposal.proposal_kind in {"generated_internal_proposal", "template_backed_proposal"},
            "Proposal kind is eligible for activation." if proposal.proposal_kind in {"generated_internal_proposal", "template_backed_proposal"} else "Proposal kind is not eligible for activation.",
            missing_requirement="Proposal kind is not eligible for activation." if proposal.proposal_kind not in {"generated_internal_proposal", "template_backed_proposal"} else None,
        )
        add_check(
            "request_kind_supported",
            proposal.request_kind_origin in {"generated_internal", "template_backed_internal"},
            "Request kind origin is eligible." if proposal.request_kind_origin in {"generated_internal", "template_backed_internal"} else "Only internal proposal kinds may be activated through this path.",
            missing_requirement="Only internal proposal kinds may be activated through this path." if proposal.request_kind_origin not in {"generated_internal", "template_backed_internal"} else None,
        )
        add_check(
            "proposal_status_approved",
            proposal.proposal_status in {"approved", "converted"},
            "Proposal is approved for activation." if proposal.proposal_status in {"approved", "converted"} else "Proposal must be approved before activation.",
            missing_requirement="Proposal must be approved before activation." if proposal.proposal_status not in {"approved", "converted"} else None,
        )
        add_check(
            "lifecycle_entry_linked",
            lifecycle_entry is not None,
            "Lifecycle entry is linked.",
            missing_requirement="A linked lifecycle entry is required for activation." if lifecycle_entry is None else None,
        )

        metadata_draft = dict(proposal.capability_metadata_draft_json or {})
        required_metadata_keys = {"capability_id", "name", "description", "supported_step_kinds", "capability_type", "allowed_contexts"}
        missing_metadata_keys = [key for key in required_metadata_keys if not metadata_draft.get(key)]
        add_check(
            "metadata_draft_complete",
            not missing_metadata_keys,
            "Proposal metadata draft is complete." if not missing_metadata_keys else f"Missing metadata keys: {', '.join(missing_metadata_keys)}.",
            missing_requirement=f"Missing metadata draft fields: {', '.join(missing_metadata_keys)}." if missing_metadata_keys else None,
        )

        candidate_contexts = [item for item in metadata_draft.get("allowed_contexts", []) if item]
        invalid_contexts = [item for item in candidate_contexts if item not in self.VALID_CONTEXTS]
        add_check(
            "enabled_contexts_present",
            bool(candidate_contexts),
            "Candidate enabled contexts are present." if candidate_contexts else "No candidate enabled contexts were provided.",
            missing_requirement="Candidate enabled contexts are required for activation." if not candidate_contexts else None,
        )
        add_check(
            "enabled_contexts_valid",
            not invalid_contexts,
            "Candidate enabled contexts are valid." if not invalid_contexts else f"Invalid contexts: {', '.join(invalid_contexts)}.",
            missing_requirement=f"Invalid enabled contexts: {', '.join(invalid_contexts)}." if invalid_contexts else None,
        )

        supported_step_kinds = list(metadata_draft.get("supported_step_kinds", []) or [])
        unknown_step_kinds = [item for item in supported_step_kinds if self.registry.get_step_kind(item) is None]
        add_check(
            "supported_step_kinds_valid",
            bool(supported_step_kinds) and not unknown_step_kinds,
            "Supported step kinds are registered." if supported_step_kinds and not unknown_step_kinds else "Supported step kinds are missing or unregistered.",
            missing_requirement=(
                f"Unsupported or missing step kinds: {', '.join(unknown_step_kinds or ['none'])}."
                if (not supported_step_kinds or unknown_step_kinds)
                else None
            ),
        )

        if lifecycle_entry.lifecycle_status == "active" and candidate_contexts:
            activation_status = "already_active"
            eligible = True
            activation_notes.append("Lifecycle entry is already active.")
        else:
            activation_status = "eligible"
            eligible = True

        if proposal.proposal_kind == "generated_internal_proposal":
            add_check(
                "generated_stub_present",
                bool((proposal.code_stub or "").strip()),
                "Generated proposal includes a bounded code stub." if (proposal.code_stub or "").strip() else "Generated internal proposal requires a bounded code stub.",
                missing_requirement="Generated internal proposal requires a bounded code stub." if not (proposal.code_stub or "").strip() else None,
            )
            add_check(
                "template_stub_absent_for_generated",
                not bool((proposal.template_stub or "").strip()),
                "Generated proposal does not rely on a template stub." if not (proposal.template_stub or "").strip() else "Generated internal proposal should not carry a template stub.",
                missing_requirement="Generated internal proposal should not carry a template stub." if (proposal.template_stub or "").strip() else None,
            )
        if proposal.proposal_kind == "template_backed_proposal":
            valid_template_kinds = set(self.TEMPLATE_EXECUTION_KIND_BY_STEP_KIND.values())
            bounded_execution_kind = (proposal.bounded_execution_kind or "").strip()
            add_check(
                "template_execution_kind_valid",
                bounded_execution_kind in valid_template_kinds,
                "Template execution kind is bounded and recognized." if bounded_execution_kind in valid_template_kinds else "Template-backed proposal requires a valid bounded execution kind.",
                missing_requirement="Template-backed proposal requires a valid bounded execution kind." if bounded_execution_kind not in valid_template_kinds else None,
            )
            add_check(
                "template_stub_present",
                bool((proposal.template_stub or "").strip()),
                "Template-backed proposal includes a bounded template stub." if (proposal.template_stub or "").strip() else "Template-backed proposal requires a template stub.",
                missing_requirement="Template-backed proposal requires a template stub." if not (proposal.template_stub or "").strip() else None,
            )
            linked_static = metadata_draft.get("linked_static_registry_capability_id") or lifecycle_entry.linked_static_registry_capability_id
            linked_static_capability = self.registry.get_capability(str(linked_static)) if linked_static else None
            add_check(
                "template_static_capability_safe",
                linked_static_capability is not None and linked_static_capability.template_backed_only,
                "Template-backed proposal is grounded in a registered template-backed capability."
                if linked_static_capability is not None and linked_static_capability.template_backed_only
                else "Template-backed proposal must link to a registered template-backed capability.",
                missing_requirement=(
                    "Template-backed proposal must link to a registered template-backed capability."
                    if not (linked_static_capability is not None and linked_static_capability.template_backed_only)
                    else None
                ),
            )
            if linked_static_capability is not None:
                context_mismatch = [item for item in candidate_contexts if item not in linked_static_capability.allowed_contexts]
                add_check(
                    "template_contexts_allowed",
                    not context_mismatch,
                    "Template proposal contexts are allowed by the linked static capability." if not context_mismatch else f"Template proposal contexts are not allowed: {', '.join(context_mismatch)}.",
                    missing_requirement=f"Template proposal contexts are not allowed: {', '.join(context_mismatch)}." if context_mismatch else None,
                )

        if lifecycle_entry.lifecycle_status == "deprecated":
            add_check(
                "lifecycle_not_deprecated",
                False,
                "Deprecated lifecycle entries cannot be activated.",
                missing_requirement="Deprecated lifecycle entries cannot be activated.",
            )

        if activation_status != "already_active":
            eligible = all(item.passed for item in checks)
            if not eligible:
                activation_status = "ineligible"
            else:
                activation_notes.append("Proposal passed deterministic activation checks.")

        return CapabilityActivationEligibilityResponse(
            proposal_id=proposal.id,
            lifecycle_entry_id=lifecycle_entry.id,
            proposal_status=proposal.proposal_status,
            lifecycle_status=lifecycle_entry.lifecycle_status,
            eligible=eligible,
            activation_status=activation_status,
            eligibility_checks=checks,
            missing_requirements=missing_requirements,
            activation_notes=activation_notes,
            enabled_contexts_candidate=[item for item in candidate_contexts if item in self.VALID_CONTEXTS],
            resulting_capability_id=lifecycle_entry.capability_id,
        )

    def _extract_contract_half(self, io_contract_summary: str, *, prefix: str) -> str:
        parts = io_contract_summary.split(";")
        for part in parts:
            normalized = part.strip()
            if normalized.startswith(prefix):
                return normalized.replace(prefix, "", 1).strip()
        return ""

    def _merge_notes(self, *note_groups: list[str] | None) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        for group in note_groups:
            for note in group or []:
                value = str(note).strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                merged.append(value)
        return merged

    def _capability_type_value(self, value: object, default: str) -> str:
        normalized = str(value or default).strip()
        allowed = {"deterministic", "llm_assisted", "external_tool", "code_template", "notification", "human_gate"}
        return normalized if normalized in allowed else default
