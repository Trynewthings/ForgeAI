from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models import CapabilityLifecycleEntry
from app.db.repositories.capability_lifecycle_repository import CapabilityLifecycleRepository
from app.db.repositories.capability_materialization_repository import CapabilityMaterializationRepository
from app.db.repositories.capability_proposal_repository import CapabilityProposalRepository
from app.schemas.capability_registry import (
    CapabilityLifecycleEntryResponse,
    CapabilityLifecycleSummaryResponse,
    CapabilityLifecycleUpdateRequest,
    CapabilityRequestConversionResponse,
    CapabilityRequestResponse,
)
from app.services.capabilities.registry import CapabilityRegistryService
from app.services.capabilities.request_service import CapabilityRequestService


class CapabilityLifecycleService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = CapabilityLifecycleRepository(db)
        self.proposal_repository = CapabilityProposalRepository(db)
        self.materialization_repository = CapabilityMaterializationRepository(db)
        self.request_service = CapabilityRequestService(db)
        self.registry = CapabilityRegistryService()

    def list_project_entries(self, project_id: str) -> list[CapabilityLifecycleEntryResponse]:
        return [self._serialize(item) for item in self.repository.list_by_project(project_id)]

    def summarize_project_entries(self, project_id: str) -> CapabilityLifecycleSummaryResponse:
        entries = self.list_project_entries(project_id)
        summary = CapabilityLifecycleSummaryResponse(total_count=len(entries))
        for entry in entries:
            summary.by_status_counts[entry.lifecycle_status] = summary.by_status_counts.get(entry.lifecycle_status, 0) + 1
            summary.by_request_kind_counts[entry.request_kind_origin] = summary.by_request_kind_counts.get(entry.request_kind_origin, 0) + 1
            if entry.lifecycle_status == "active":
                summary.active_count += 1
            elif entry.lifecycle_status == "proposed":
                summary.proposed_count += 1
            elif entry.lifecycle_status == "disabled":
                summary.disabled_count += 1
            elif entry.lifecycle_status == "deprecated":
                summary.deprecated_count += 1
            if entry.is_usable:
                summary.usable_count += 1
        summary.headlines = [entry.display_summary for entry in entries[:3]]
        return summary

    def get_entry(self, entry_id: str) -> CapabilityLifecycleEntryResponse:
        entry = self.repository.get_by_id(entry_id)
        if entry is None:
            raise AppException("Capability lifecycle entry not found", status_code=404)
        return self._serialize(entry)

    def convert_request(self, request_id: str) -> CapabilityRequestConversionResponse:
        request = self.request_service.get_request(request_id)
        if request.review_state.review_status == "converted":
            existing = self.repository.get_by_source_request(request_id)
            if existing is None:
                raise AppException("Capability request is marked converted but no lifecycle entry was found", status_code=409)
            return CapabilityRequestConversionResponse(
                request=self.request_service.get_request(request_id),
                lifecycle_entry=self._serialize(existing),
                converted=True,
                idempotent=True,
            )
        if request.review_state.review_status != "approved":
            raise AppException("Only approved capability requests can be converted into lifecycle entries", status_code=409)
        if request.request_kind == "manual_review_only":
            raise AppException("manual_review_only requests cannot be converted into lifecycle entries without further review resolution", status_code=409)
        existing = self.repository.get_by_source_request(request_id)
        if existing is not None:
            updated_request = self.request_service.update_review_state(request_id, review_status="converted")
            return CapabilityRequestConversionResponse(
                request=updated_request,
                lifecycle_entry=self._serialize(existing),
                converted=True,
                idempotent=True,
            )
        entry = self.repository.create(self._build_entry(request))
        updated_request = self.request_service.update_review_state(request_id, review_status="converted")
        self.db.commit()
        self.db.refresh(entry)
        return CapabilityRequestConversionResponse(
            request=updated_request,
            lifecycle_entry=self._serialize(entry),
            converted=True,
            idempotent=False,
        )

    def update_entry(self, entry_id: str, payload: CapabilityLifecycleUpdateRequest) -> CapabilityLifecycleEntryResponse:
        entry = self.repository.get_by_id(entry_id)
        if entry is None:
            raise AppException("Capability lifecycle entry not found", status_code=404)
        entry.lifecycle_status = payload.lifecycle_status
        entry.enabled_contexts_json = list(payload.enabled_contexts or [])
        entry.review_notes_json = list(payload.review_notes or [])
        self.db.commit()
        self.db.refresh(entry)
        return self._serialize(entry)

    def get_active_capability_definitions(self, project_id: str):
        return [self._to_capability_definition(item) for item in self.repository.list_active_by_project(project_id)]

    def get_linked_entry(self, request_id: str) -> CapabilityLifecycleEntryResponse | None:
        entry = self.repository.get_by_source_request(request_id)
        if entry is None:
            return None
        return self._serialize(entry)

    def _build_entry(self, request: CapabilityRequestResponse) -> CapabilityLifecycleEntry:
        linked_static = None
        static_capability = None
        if request.requested_capability_name_or_label:
            static_capability = self.registry.get_capability(request.requested_capability_name_or_label)
        if static_capability is not None:
            linked_static = static_capability.capability_id
        capability_type = self._capability_type_for_request(request)
        lifecycle_status = self._initial_lifecycle_status(request)
        capability_id = self._derive_capability_id(request)
        activation_requirements = self._activation_requirements(request)
        enabled_contexts = self._intended_contexts(request)
        supported_step_kinds = [request.requested_logical_step_kind] if request.requested_logical_step_kind else []
        name = request.requested_capability_name_or_label or capability_id
        description = request.review_reason
        input_summary = static_capability.input_contract_summary if static_capability is not None else ""
        output_summary = static_capability.output_contract_summary if static_capability is not None else ""
        runtime_requirements = list(static_capability.runtime_requirements) if static_capability is not None else []
        provider_requirements = list(static_capability.provider_requirements) if static_capability is not None else []
        return CapabilityLifecycleEntry(
            project_id=request.context.source_project_id,
            source_request_id=request.request_id,
            capability_id=capability_id,
            lifecycle_status=lifecycle_status,
            capability_type=capability_type,
            request_kind_origin=request.request_kind,
            implementation_route=request.implementation_route_hint,
            activation_requirements_json=activation_requirements,
            activation_notes_json=[],
            review_notes_json=list(request.review_state.review_notes or []),
            enabled_contexts_json=enabled_contexts,
            supported_step_kinds_json=supported_step_kinds,
            linked_static_registry_capability_id=linked_static,
            name=name,
            description=description,
            input_contract_summary=input_summary,
            output_contract_summary=output_summary,
            runtime_requirements_json=runtime_requirements,
            provider_requirements_json=provider_requirements,
            requires_review=True,
            template_backed_only=request.request_kind == "template_backed_internal",
            tags_json=[request.request_kind, request.source_stage],
            metadata_json={
                "source_stage": request.source_stage,
                "request_kind_origin": request.request_kind,
                "display_summary": request.display_summary,
            },
        )

    def _initial_lifecycle_status(self, request: CapabilityRequestResponse) -> str:
        if request.request_kind == "generated_internal":
            return "proposed"
        return "disabled"

    def _capability_type_for_request(self, request: CapabilityRequestResponse) -> str:
        if request.request_kind == "external_integration":
            return "external_tool"
        if request.request_kind == "template_backed_internal":
            return "code_template"
        if request.request_kind == "manual_review_only":
            return "human_gate"
        return "deterministic"

    def _activation_requirements(self, request: CapabilityRequestResponse) -> list[str]:
        if request.request_kind == "external_integration":
            return ["credentials_setup", "policy_review", "network_access_validation", "registry_activation_review"]
        if request.request_kind == "template_backed_internal":
            return ["template_variant_review", "bounded_execution_validation", "registry_activation_review"]
        return ["implementation_review", "registry_activation_review"]

    def _intended_contexts(self, request: CapabilityRequestResponse) -> list[str]:
        if request.request_kind == "generated_internal":
            return ["blueprint_planning", "build_planning", "runtime"]
        if request.request_kind == "template_backed_internal":
            return ["blueprint_planning", "build_planning", "runtime"]
        if request.request_kind == "external_integration":
            return ["blueprint_planning", "build_planning", "runtime"]
        return []

    def _derive_capability_id(self, request: CapabilityRequestResponse) -> str:
        base = request.requested_capability_name_or_label or request.requested_logical_step_kind or request.request_id
        slug = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_") or "capability"
        prefix = {
            "external_integration": "ext",
            "template_backed_internal": "tmpl",
            "generated_internal": "gen",
            "manual_review_only": "review",
        }[request.request_kind]
        return f"{prefix}_{slug}"

    def _serialize(self, entry: CapabilityLifecycleEntry) -> CapabilityLifecycleEntryResponse:
        is_usable = entry.lifecycle_status == "active" and bool(entry.enabled_contexts_json)
        linked_proposal = self.proposal_repository.get_by_source_lifecycle_entry(entry.id)
        linked_materialization = self.materialization_repository.get_by_source_lifecycle_entry(entry.id)
        return CapabilityLifecycleEntryResponse(
            capability_entry_id=entry.id,
            capability_id=entry.capability_id,
            source_request_id=entry.source_request_id,
            lifecycle_status=entry.lifecycle_status,
            capability_type=entry.capability_type,
            request_kind_origin=entry.request_kind_origin,
            implementation_route=entry.implementation_route,
            activation_requirements=list(entry.activation_requirements_json or []),
            activation_notes=list(entry.activation_notes_json or []),
            review_notes=list(entry.review_notes_json or []),
            enabled_contexts=list(entry.enabled_contexts_json or []),
            supported_step_kinds=list(entry.supported_step_kinds_json or []),
            linked_static_registry_capability_id=entry.linked_static_registry_capability_id,
            linked_proposal_id=linked_proposal.id if linked_proposal is not None else None,
            linked_proposal_status=linked_proposal.proposal_status if linked_proposal is not None else None,
            linked_materialization_id=linked_materialization.id if linked_materialization is not None else None,
            linked_materialization_status=linked_materialization.materialization_status if linked_materialization is not None else None,
            display_summary=f"{entry.lifecycle_status} capability entry {entry.capability_id} from {entry.request_kind_origin}",
            is_usable=is_usable,
            activated_at=entry.activated_at.isoformat() if entry.activated_at else None,
            created_at=entry.created_at.isoformat() if entry.created_at else None,
            updated_at=entry.updated_at.isoformat() if entry.updated_at else None,
        )

    def _to_capability_definition(self, entry: CapabilityLifecycleEntry):
        from app.schemas.capability_registry import CapabilityDefinition

        if entry.linked_static_registry_capability_id:
            static = self.registry.get_capability(entry.linked_static_registry_capability_id)
            if static is not None:
                payload = static.model_dump(mode="python")
                payload.update(
                    {
                        "capability_id": entry.capability_id,
                        "name": entry.name,
                        "description": entry.description or static.description,
                        "allowed_contexts": list(entry.enabled_contexts_json or []),
                        "supported_step_kinds": list(entry.supported_step_kinds_json or static.supported_step_kinds),
                        "enabled": entry.lifecycle_status == "active",
                        "requires_review": entry.requires_review,
                        "template_backed_only": entry.template_backed_only,
                        "runtime_requirements": list(entry.runtime_requirements_json or static.runtime_requirements),
                        "provider_requirements": list(entry.provider_requirements_json or static.provider_requirements),
                        "tags": list(entry.tags_json or static.tags),
                    }
                )
                return CapabilityDefinition.model_validate(payload)
        return CapabilityDefinition(
            capability_id=entry.capability_id,
            name=entry.name,
            description=entry.description,
            supported_step_kinds=list(entry.supported_step_kinds_json or []),
            capability_type=entry.capability_type,
            input_contract_summary=entry.input_contract_summary,
            output_contract_summary=entry.output_contract_summary,
            runtime_requirements=list(entry.runtime_requirements_json or []),
            provider_requirements=list(entry.provider_requirements_json or []),
            allowed_contexts=list(entry.enabled_contexts_json or []),
            requires_review=entry.requires_review,
            enabled=entry.lifecycle_status == "active",
            template_backed_only=entry.template_backed_only,
            tags=list(entry.tags_json or []),
        )
