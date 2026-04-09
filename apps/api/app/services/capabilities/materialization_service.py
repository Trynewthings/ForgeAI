from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.exceptions import AppException
from app.db.models import CapabilityLifecycleEntry, CapabilityMaterialization, CapabilityProposal
from app.db.repositories.capability_lifecycle_repository import CapabilityLifecycleRepository
from app.db.repositories.capability_materialization_repository import CapabilityMaterializationRepository
from app.db.repositories.capability_proposal_repository import CapabilityProposalRepository
from app.db.repositories.capability_request_repository import CapabilityRequestRepository
from app.schemas.capability_registry import (
    CapabilityActivationCheckResponse,
    CapabilityLifecycleEntryResponse,
    CapabilityMaterializationAsset,
    CapabilityMaterializationEligibilityResponse,
    CapabilityMaterializationProjectSummaryResponse,
    CapabilityMaterializationResponse,
    CapabilityMaterializationResultResponse,
    CapabilityMaterializationSummaryResponse,
    CapabilityProposalResponse,
    CapabilityReusableCandidateProjectSummaryResponse,
    CapabilityReusableCandidateResponse,
    CapabilityReusableCandidateSummaryResponse,
    CapabilityRuntimeRealizationSummary,
)


class CapabilityMaterializationService:
    GENERATED_PATTERN_MAP = {
        "rank": ("python_function", "materialized_internal_python", "rank_candidates", "rank_runtime"),
        "validate": ("python_function", "materialized_internal_python", "validate_schema_contract", "validate_runtime"),
        "transform": ("python_function", "materialized_internal_python", "deduplicate_items", "transform_runtime"),
        "persist": ("artifact_writer", "materialized_internal_artifact", "write_artifact", "persist_runtime"),
    }
    TEMPLATE_RUNTIME_MAP = {
        "record_normalizer": ("template_runtime", "materialized_template_runtime", "record_normalizer"),
        "schema_guard_template": ("template_runtime", "materialized_template_runtime", "schema_guard_template"),
        "artifact_writer_template": ("template_runtime", "materialized_template_runtime", "artifact_writer_template"),
    }

    def __init__(self, db: Session) -> None:
        self.db = db
        self.repository = CapabilityMaterializationRepository(db)
        self.lifecycle_repository = CapabilityLifecycleRepository(db)
        self.proposal_repository = CapabilityProposalRepository(db)
        self.request_repository = CapabilityRequestRepository(db)

    def list_project_materializations(self, project_id: str) -> list[CapabilityMaterializationResponse]:
        return [self._serialize(item) for item in self.repository.list_by_project(project_id)]

    def get_project_summary(self, project_id: str) -> CapabilityMaterializationProjectSummaryResponse:
        materializations = self.list_project_materializations(project_id)
        summary = CapabilityMaterializationSummaryResponse(total_count=len(materializations))
        for item in materializations:
            summary.by_kind_counts[item.materialization_kind] = summary.by_kind_counts.get(item.materialization_kind, 0) + 1
            summary.by_status_counts[item.materialization_status] = summary.by_status_counts.get(item.materialization_status, 0) + 1
            if item.materialization_status == "materialized":
                summary.materialized_count += 1
            elif item.materialization_status == "blocked":
                summary.blocked_count += 1
            elif item.materialization_status == "eligible":
                summary.eligible_count += 1
        summary.headlines = [item.display_summary for item in materializations[:3]]
        return CapabilityMaterializationProjectSummaryResponse(
            project_id=project_id,
            summary=summary,
            latest_materializations=materializations[:5],
        )

    def get_by_proposal(self, proposal_id: str) -> CapabilityMaterializationResponse | None:
        item = self.repository.get_by_source_proposal(proposal_id)
        return self._serialize(item) if item is not None else None

    def get_by_lifecycle_entry(self, entry_id: str) -> CapabilityMaterializationResponse | None:
        item = self.repository.get_by_source_lifecycle_entry(entry_id)
        return self._serialize(item) if item is not None else None

    def get_runtime_materialization(self, project_id: str, capability_id: str) -> CapabilityMaterializationResponse | None:
        item = self.repository.get_by_capability_id(project_id, capability_id)
        if item is None or item.materialization_status != "materialized":
            return None
        lifecycle_entry = self.lifecycle_repository.get_by_id(item.source_lifecycle_entry_id)
        if lifecycle_entry is None or lifecycle_entry.lifecycle_status != "active":
            return None
        return self._serialize(item)

    def list_reusable_candidates(self, project_id: str) -> list[CapabilityReusableCandidateResponse]:
        candidates: list[CapabilityReusableCandidateResponse] = []
        for item in self.repository.list_by_project(project_id):
            lifecycle_entry = self.lifecycle_repository.get_by_id(item.source_lifecycle_entry_id)
            proposal = self.proposal_repository.get_by_id(item.source_proposal_id)
            request = self.request_repository.get_by_id(item.source_request_id)
            if lifecycle_entry is None or proposal is None or request is None:
                continue
            candidate = self._build_reusable_candidate(
                item=item,
                lifecycle_entry=lifecycle_entry,
                proposal=proposal,
                request=request,
            )
            if candidate is not None:
                candidates.append(candidate)
        return sorted(
            candidates,
            key=lambda item: (
                0 if item.reusable else 1,
                -item.selection_priority,
                item.capability_id,
            ),
        )

    def get_reusable_candidate(self, project_id: str, capability_id: str) -> CapabilityReusableCandidateResponse | None:
        for item in self.list_reusable_candidates(project_id):
            if item.capability_id == capability_id:
                return item
            if capability_id in item.alias_ids:
                return item
        return None

    def get_reusable_summary(self, project_id: str) -> CapabilityReusableCandidateProjectSummaryResponse:
        candidates = self.list_reusable_candidates(project_id)
        summary = CapabilityReusableCandidateSummaryResponse(total_count=len(candidates))
        for item in candidates:
            if item.reusable:
                summary.reusable_count += 1
            if item.runtime_ready:
                summary.runtime_ready_count += 1
            summary.by_request_kind_counts[item.origin_request_kind] = (
                summary.by_request_kind_counts.get(item.origin_request_kind, 0) + 1
            )
            runtime_shape_key = item.runtime_shape.execution_kind or "unknown"
            summary.by_runtime_shape_counts[runtime_shape_key] = (
                summary.by_runtime_shape_counts.get(runtime_shape_key, 0) + 1
            )
        summary.headlines = [item.selection_explanation or item.display_name for item in candidates[:3]]
        return CapabilityReusableCandidateProjectSummaryResponse(
            project_id=project_id,
            summary=summary,
            reusable_candidates=candidates,
        )

    def get_eligibility(self, proposal_id: str) -> CapabilityMaterializationEligibilityResponse:
        proposal = self._require_proposal(proposal_id)
        lifecycle_entry = self._require_lifecycle_entry(proposal)
        return self._build_eligibility(proposal, lifecycle_entry)

    def materialize_proposal(self, proposal_id: str) -> CapabilityMaterializationResultResponse:
        proposal = self._require_proposal(proposal_id)
        lifecycle_entry = self._require_lifecycle_entry(proposal)
        eligibility = self._build_eligibility(proposal, lifecycle_entry)
        existing = self.repository.get_by_source_proposal(proposal_id)
        if existing is not None and existing.materialization_status == "materialized" and eligibility.eligible:
            return CapabilityMaterializationResultResponse(
                proposal=self._serialize_proposal(proposal),
                lifecycle_entry=self._serialize_lifecycle(lifecycle_entry),
                eligibility=eligibility,
                materialization=self._serialize(existing),
                materialized=True,
                idempotent=True,
            )

        if not eligibility.eligible:
            blocked = self._upsert_materialization(
                existing=existing,
                proposal=proposal,
                lifecycle_entry=lifecycle_entry,
                eligibility=eligibility,
                status="blocked",
            )
            self.db.commit()
            self.db.refresh(blocked)
            return CapabilityMaterializationResultResponse(
                proposal=self._serialize_proposal(proposal),
                lifecycle_entry=self._serialize_lifecycle(lifecycle_entry),
                eligibility=self._build_eligibility(proposal, lifecycle_entry),
                materialization=self._serialize(blocked),
                materialized=False,
                idempotent=False,
            )

        materialized = self._upsert_materialization(
            existing=existing,
            proposal=proposal,
            lifecycle_entry=lifecycle_entry,
            eligibility=eligibility,
            status="materialized",
        )
        self.db.commit()
        self.db.refresh(materialized)
        return CapabilityMaterializationResultResponse(
            proposal=self._serialize_proposal(proposal),
            lifecycle_entry=self._serialize_lifecycle(lifecycle_entry),
            eligibility=self._build_eligibility(proposal, lifecycle_entry),
            materialization=self._serialize(materialized),
            materialized=True,
            idempotent=False,
        )

    def _upsert_materialization(
        self,
        *,
        existing: CapabilityMaterialization | None,
        proposal: CapabilityProposal,
        lifecycle_entry: CapabilityLifecycleEntry,
        eligibility: CapabilityMaterializationEligibilityResponse,
        status: str,
    ) -> CapabilityMaterialization:
        payload = {
            "project_id": lifecycle_entry.project_id,
            "source_request_id": proposal.source_request_id,
            "source_lifecycle_entry_id": lifecycle_entry.id,
            "source_proposal_id": proposal.id,
            "materialization_kind": eligibility.materialization_kind or (
                "template_backed_runtime" if proposal.proposal_kind == "template_backed_proposal" else "generated_internal_runtime"
            ),
            "materialization_status": status,
            "resulting_handler_ref": eligibility.resulting_handler_ref,
            "resulting_execution_kind": eligibility.resulting_execution_kind,
            "resulting_runtime_adapter": eligibility.resulting_runtime_adapter,
            "materialized_asset_refs_json": [item.model_dump(mode="python") for item in eligibility.materialized_asset_refs],
            "runtime_constraints_json": dict(eligibility.runtime_constraints),
            "review_notes_json": list(eligibility.review_notes),
            "blocked_reason": eligibility.blocked_reason if status != "materialized" else None,
            "metadata_json": {
                "request_kind_origin": proposal.request_kind_origin,
                "proposal_kind": proposal.proposal_kind,
                "capability_id_candidate": proposal.capability_id_candidate,
            },
        }
        if existing is None:
            return self.repository.create(CapabilityMaterialization(**payload))
        existing.materialization_kind = payload["materialization_kind"]
        existing.materialization_status = payload["materialization_status"]
        existing.resulting_handler_ref = payload["resulting_handler_ref"]
        existing.resulting_execution_kind = payload["resulting_execution_kind"]
        existing.resulting_runtime_adapter = payload["resulting_runtime_adapter"]
        existing.materialized_asset_refs_json = payload["materialized_asset_refs_json"]
        existing.runtime_constraints_json = payload["runtime_constraints_json"]
        existing.review_notes_json = payload["review_notes_json"]
        existing.blocked_reason = payload["blocked_reason"]
        existing.metadata_json = payload["metadata_json"]
        self.db.flush()
        return existing

    def _build_eligibility(
        self,
        proposal: CapabilityProposal,
        lifecycle_entry: CapabilityLifecycleEntry,
    ) -> CapabilityMaterializationEligibilityResponse:
        checks: list[CapabilityActivationCheckResponse] = []
        missing_requirements: list[str] = []
        review_notes: list[str] = []

        def add_check(check_key: str, passed: bool, detail: str, *, missing_requirement: str | None = None) -> None:
            checks.append(CapabilityActivationCheckResponse(check_key=check_key, passed=passed, detail=detail))
            if not passed and missing_requirement is not None:
                missing_requirements.append(missing_requirement)

        metadata = dict(proposal.capability_metadata_draft_json or {})
        runtime_constraints: dict[str, object] = {}
        assets: list[CapabilityMaterializationAsset] = []
        materialization_kind = "template_backed_runtime" if proposal.proposal_kind == "template_backed_proposal" else "generated_internal_runtime"
        resulting_execution_kind: str | None = None
        resulting_runtime_adapter: str | None = None
        resulting_handler_ref: str | None = None
        blocked_reason: str | None = None

        add_check(
            "lifecycle_active",
            lifecycle_entry.lifecycle_status == "active",
            "Lifecycle entry is active." if lifecycle_entry.lifecycle_status == "active" else "Lifecycle entry must be active before runtime materialization.",
            missing_requirement="Lifecycle entry must be active before runtime materialization."
            if lifecycle_entry.lifecycle_status != "active"
            else None,
        )
        add_check(
            "proposal_reviewed",
            proposal.proposal_status in {"approved", "converted"},
            "Proposal has been approved for runtime materialization."
            if proposal.proposal_status in {"approved", "converted"}
            else "Proposal must be approved before runtime materialization.",
            missing_requirement="Proposal must be approved before runtime materialization."
            if proposal.proposal_status not in {"approved", "converted"}
            else None,
        )
        add_check(
            "request_kind_internal",
            proposal.request_kind_origin in {"generated_internal", "template_backed_internal"},
            "Internal request kind is eligible for materialization."
            if proposal.request_kind_origin in {"generated_internal", "template_backed_internal"}
            else "Only internal capability proposals are eligible for runtime materialization.",
            missing_requirement="Only internal capability proposals are eligible for runtime materialization."
            if proposal.request_kind_origin not in {"generated_internal", "template_backed_internal"}
            else None,
        )
        add_check(
            "runtime_context_enabled",
            "runtime" in list(lifecycle_entry.enabled_contexts_json or []),
            "Runtime context is enabled."
            if "runtime" in list(lifecycle_entry.enabled_contexts_json or [])
            else "Runtime context must be enabled on the active lifecycle entry.",
            missing_requirement="Runtime context must be enabled on the active lifecycle entry."
            if "runtime" not in list(lifecycle_entry.enabled_contexts_json or [])
            else None,
        )
        supported_step_kinds = list(metadata.get("supported_step_kinds") or lifecycle_entry.supported_step_kinds_json or [])
        add_check(
            "supported_step_kind_present",
            bool(supported_step_kinds),
            "Supported step kinds are present." if supported_step_kinds else "Supported step kinds are required for runtime materialization.",
            missing_requirement="Supported step kinds are required for runtime materialization." if not supported_step_kinds else None,
        )

        if proposal.proposal_kind == "generated_internal_proposal":
            logical_step_kind = proposal.logical_step_kind or (supported_step_kinds[0] if supported_step_kinds else None)
            pattern_key = str(metadata.get("bounded_runtime_pattern") or logical_step_kind or "").strip()
            pattern = self.GENERATED_PATTERN_MAP.get(pattern_key)
            add_check(
                "generated_pattern_supported",
                pattern is not None,
                "Generated internal proposal maps to a bounded runtime pattern."
                if pattern is not None
                else "Generated internal proposal must map to a supported bounded runtime pattern.",
                missing_requirement="Generated internal proposal must map to a supported bounded runtime pattern."
                if pattern is None
                else None,
            )
            add_check(
                "generated_stub_present",
                bool((proposal.code_stub or "").strip()),
                "Generated internal proposal includes a reviewed bounded stub."
                if (proposal.code_stub or "").strip()
                else "Generated internal proposal requires a reviewed bounded stub.",
                missing_requirement="Generated internal proposal requires a reviewed bounded stub."
                if not (proposal.code_stub or "").strip()
                else None,
            )
            if pattern is not None:
                resulting_execution_kind, resulting_runtime_adapter, resulting_handler_ref, pattern_name = pattern
                runtime_constraints = {
                    "bounded_runtime_pattern": pattern_name,
                    "allowed_contexts": list(lifecycle_entry.enabled_contexts_json or []),
                    "supported_step_kinds": supported_step_kinds,
                }
                assets.append(
                    CapabilityMaterializationAsset(
                        path=f"assets/materialized_capabilities/{proposal.capability_id_candidate}.json",
                        kind="generated_runtime_stub",
                        description="Bounded generated-internal runtime realization metadata.",
                        review_required=False,
                        payload={
                            "capability_id": proposal.capability_id_candidate,
                            "handler_ref": resulting_handler_ref,
                            "execution_kind": resulting_execution_kind,
                            "runtime_adapter": resulting_runtime_adapter,
                            "bounded_runtime_pattern": pattern_name,
                            "code_stub": proposal.code_stub or "",
                        },
                    )
                )
                review_notes.append(f"Bounded generated runtime pattern selected: {pattern_name}.")
        else:
            bounded_kind = str(proposal.bounded_execution_kind or metadata.get("bounded_template_runtime_kind") or "").strip()
            pattern = self.TEMPLATE_RUNTIME_MAP.get(bounded_kind)
            add_check(
                "template_runtime_kind_supported",
                pattern is not None,
                "Template-backed proposal maps to a bounded template runtime."
                if pattern is not None
                else "Template-backed proposal requires a supported bounded template runtime kind.",
                missing_requirement="Template-backed proposal requires a supported bounded template runtime kind."
                if pattern is None
                else None,
            )
            add_check(
                "template_stub_present",
                bool((proposal.template_stub or "").strip()),
                "Template-backed proposal includes a reviewed bounded template stub."
                if (proposal.template_stub or "").strip()
                else "Template-backed proposal requires a reviewed template stub.",
                missing_requirement="Template-backed proposal requires a reviewed template stub."
                if not (proposal.template_stub or "").strip()
                else None,
            )
            if pattern is not None:
                resulting_execution_kind, resulting_runtime_adapter, resulting_handler_ref = pattern
                runtime_constraints = {
                    "bounded_template_runtime_kind": bounded_kind,
                    "allowed_contexts": list(lifecycle_entry.enabled_contexts_json or []),
                    "supported_step_kinds": supported_step_kinds,
                }
                assets.append(
                    CapabilityMaterializationAsset(
                        path=f"assets/materialized_templates/{proposal.capability_id_candidate}.json",
                        kind="template_runtime_stub",
                        description="Bounded template-backed runtime realization metadata.",
                        review_required=False,
                        payload={
                            "capability_id": proposal.capability_id_candidate,
                            "handler_ref": resulting_handler_ref,
                            "execution_kind": resulting_execution_kind,
                            "runtime_adapter": resulting_runtime_adapter,
                            "bounded_execution_kind": bounded_kind,
                            "template_stub": proposal.template_stub or "",
                        },
                    )
                )
                review_notes.append(f"Bounded template runtime selected: {bounded_kind}.")

        eligible = all(item.passed for item in checks)
        status = "eligible" if eligible else "blocked"
        if not eligible and missing_requirements:
            blocked_reason = missing_requirements[0]
        return CapabilityMaterializationEligibilityResponse(
            proposal_id=proposal.id,
            lifecycle_entry_id=lifecycle_entry.id,
            proposal_kind=proposal.proposal_kind,
            request_kind_origin=proposal.request_kind_origin,
            eligible=eligible,
            materialization_kind=materialization_kind,
            materialization_status=status,
            resulting_handler_ref=resulting_handler_ref,
            resulting_execution_kind=resulting_execution_kind,
            resulting_runtime_adapter=resulting_runtime_adapter,
            eligibility_checks=checks,
            missing_requirements=missing_requirements,
            blocked_reason=blocked_reason,
            activation_dependency_met=lifecycle_entry.lifecycle_status == "active",
            runtime_constraints=runtime_constraints,
            materialized_asset_refs=assets,
            review_notes=review_notes,
        )

    def _require_proposal(self, proposal_id: str) -> CapabilityProposal:
        proposal = self.proposal_repository.get_by_id(proposal_id)
        if proposal is None:
            raise AppException("Capability proposal not found", status_code=404)
        return proposal

    def _require_lifecycle_entry(self, proposal: CapabilityProposal) -> CapabilityLifecycleEntry:
        if not proposal.source_lifecycle_entry_id:
            raise AppException("Capability proposal is not linked to a lifecycle entry.", status_code=409)
        lifecycle_entry = self.lifecycle_repository.get_by_id(proposal.source_lifecycle_entry_id)
        if lifecycle_entry is None:
            raise AppException("Linked capability lifecycle entry was not found.", status_code=409)
        return lifecycle_entry

    def _serialize(self, item: CapabilityMaterialization) -> CapabilityMaterializationResponse:
        asset_refs = [
            CapabilityMaterializationAsset.model_validate(payload)
            for payload in list(item.materialized_asset_refs_json or [])
            if isinstance(payload, dict)
        ]
        runtime_constraints = dict(item.runtime_constraints_json or {})
        runnable = item.materialization_status == "materialized" and bool(item.resulting_handler_ref and item.resulting_execution_kind)
        return CapabilityMaterializationResponse(
            materialization_id=item.id,
            source_lifecycle_entry_id=item.source_lifecycle_entry_id,
            source_proposal_id=item.source_proposal_id,
            source_request_id=item.source_request_id,
            materialization_kind=item.materialization_kind,
            materialization_status=item.materialization_status,
            resulting_handler_ref=item.resulting_handler_ref,
            resulting_execution_kind=item.resulting_execution_kind,
            resulting_runtime_adapter=item.resulting_runtime_adapter,
            materialized_asset_refs=asset_refs,
            runtime_constraints=runtime_constraints,
            review_notes=list(item.review_notes_json or []),
            blocked_reason=item.blocked_reason,
            display_summary=f"{item.materialization_kind} for {item.source_lifecycle_entry_id} ({item.materialization_status})",
            runtime_realization=CapabilityRuntimeRealizationSummary(
                handler_ref=item.resulting_handler_ref,
                execution_kind=item.resulting_execution_kind,
                runtime_adapter=item.resulting_runtime_adapter,
                runtime_constraints=runtime_constraints,
                asset_count=len(asset_refs),
                runnable=runnable,
                notes=list(item.review_notes_json or []),
            ),
            created_at=item.created_at.isoformat() if item.created_at else None,
            updated_at=item.updated_at.isoformat() if item.updated_at else None,
        )

    def _build_reusable_candidate(
        self,
        *,
        item: CapabilityMaterialization,
        lifecycle_entry: CapabilityLifecycleEntry,
        proposal: CapabilityProposal,
        request,
    ) -> CapabilityReusableCandidateResponse | None:
        runtime_constraints = dict(item.runtime_constraints_json or {})
        runtime_ready = (
            item.materialization_status == "materialized"
            and lifecycle_entry.lifecycle_status == "active"
            and "runtime" in list(lifecycle_entry.enabled_contexts_json or [])
            and bool(item.resulting_execution_kind and item.resulting_runtime_adapter and item.resulting_handler_ref)
        )
        supported_step_kinds = list(lifecycle_entry.supported_step_kinds_json or [])
        if not supported_step_kinds:
            supported_step_kinds = [value for value in [proposal.logical_step_kind] if value]
        alias_ids = [
            value
            for value in {
                lifecycle_entry.capability_id,
                proposal.capability_id_candidate,
                request.requested_capability_name_or_label,
                lifecycle_entry.linked_static_registry_capability_id,
            }
            if value
        ]
        reusable = runtime_ready and bool(supported_step_kinds) and bool(lifecycle_entry.enabled_contexts_json)
        reusable_reason = (
            "runtime_materialized_project_scoped_candidate"
            if reusable
            else "materialization_or_runtime_constraints_incomplete"
        )
        selection_constraints = []
        if request.request_kind == "generated_internal":
            selection_constraints.append("competes only when no higher-priority exact static canonical match exists")
        if request.request_kind == "template_backed_internal":
            selection_constraints.append("template-backed reuse requires template/config contract compatibility")
        selection_constraints.append("project-scoped only")
        selection_priority = 180 if request.request_kind == "template_backed_internal" else 140
        selection_explanation = (
            f"Reusable {request.request_kind} candidate for step kinds {', '.join(supported_step_kinds)} "
            f"through {item.resulting_execution_kind or 'runtime'}."
        )
        if request.request_kind == "template_backed_internal":
            selection_explanation = (
                f"Reusable bounded template runtime candidate for {', '.join(supported_step_kinds)} "
                f"using {item.resulting_handler_ref or 'template_runtime'}."
            )
        return CapabilityReusableCandidateResponse(
            capability_id=lifecycle_entry.capability_id,
            display_name=lifecycle_entry.name or proposal.capability_id_candidate,
            source_materialization_id=item.id,
            source_lifecycle_entry_id=lifecycle_entry.id,
            source_request_id=request.id,
            source_proposal_id=proposal.id,
            origin_request_kind=request.request_kind,
            runtime_ready=runtime_ready,
            reusable=reusable,
            supported_step_kinds=supported_step_kinds,
            enabled_contexts=list(lifecycle_entry.enabled_contexts_json or []),
            purpose_summary=proposal.capability_purpose or request.review_reason,
            config_contract_summary=proposal.configuration_contract_summary or proposal.io_contract_summary or lifecycle_entry.input_contract_summary,
            runtime_shape=CapabilityRuntimeRealizationSummary(
                handler_ref=item.resulting_handler_ref,
                execution_kind=item.resulting_execution_kind,
                runtime_adapter=item.resulting_runtime_adapter,
                runtime_constraints=runtime_constraints,
                asset_count=len(list(item.materialized_asset_refs_json or [])),
                runnable=runtime_ready,
                notes=list(item.review_notes_json or []),
            ),
            selection_priority=selection_priority,
            selection_constraints=selection_constraints,
            selection_explanation=selection_explanation,
            reusable_reason=reusable_reason,
            availability_explanation=(
                "Available as a project-scoped reusable internal capability."
                if reusable
                else "Lifecycle or runtime materialization is incomplete, so this internal capability is not reusable yet."
            ),
            current_stage_visibility=["project", "build", "runtime"],
            recommended_for_current_step_kind=False,
            alias_ids=alias_ids,
            linked_static_registry_capability_id=lifecycle_entry.linked_static_registry_capability_id,
        )

    def _serialize_proposal(self, proposal: CapabilityProposal) -> CapabilityProposalResponse:
        from app.services.capabilities.proposal_service import CapabilityProposalService

        return CapabilityProposalService(self.db)._serialize(proposal)  # noqa: SLF001

    def _serialize_lifecycle(self, lifecycle_entry: CapabilityLifecycleEntry) -> CapabilityLifecycleEntryResponse:
        from app.services.capabilities.lifecycle_service import CapabilityLifecycleService

        return CapabilityLifecycleService(self.db)._serialize(lifecycle_entry)  # noqa: SLF001
