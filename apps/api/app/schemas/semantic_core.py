from __future__ import annotations

from typing import Any, ClassVar, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_serializer, model_validator


SemanticTargetObject = Literal["repository", "web_resource", "local_workspace", "dataset", "software_project", "generic_request"]
SemanticPrimaryObjective = Literal["analyze", "collect", "transform", "synthesize", "validate", "deliver", "build"]
SemanticDeliverableContract = Literal[
    "architecture_report",
    "research_briefing",
    "dataset_summary",
    "dataset_summary_report",
    "dataset_validation_report",
    "dataset_comparison_report",
    "artifact_bundle",
    "generic_output",
]
SemanticDeliverableForm = Literal["report", "briefing", "artifact_bundle", "service", "dataset", "generic_output"]
SemanticAnalysisFrame = Literal["architecture", "assessment", "audit", "summary", "construction", "generic"]
SemanticDecisionPosture = Literal["descriptive", "evaluative", "comparative", "recommendation", "generic"]
SemanticWorkflowShape = Literal["single_service", "bounded_workflow", "review_gated_workflow"]
SemanticSourceModality = Literal["workspace_repo", "remote_repo", "web_source", "local_path", "mixed", "unknown"]
SemanticEvidenceBasis = Literal[
    "repo_structure",
    "repo_relationships",
    "repo_execution_surface",
    "web_sources",
    "local_documents",
    "artifact_payload",
    "structured_data",
    "implementation_spec",
]
SemanticCoreSource = Literal["authoring_semantic_state", "fallback_interpretation"]


class GlobalSemanticCore(BaseModel):
    CORE_PLANNING_AXIS_FIELDS: ClassVar[tuple[str, ...]] = (
        "target_object",
        "primary_objective",
        "workflow_shape",
        "source_modality",
        "deliverable_form",
        "analysis_frame",
        "decision_posture",
        "evidence_basis",
        "ambiguities",
        "confidence",
    )

    target_object: SemanticTargetObject = "generic_request"
    primary_objective: SemanticPrimaryObjective = "deliver"
    workflow_shape: SemanticWorkflowShape = "single_service"
    source_modality: SemanticSourceModality = "unknown"
    deliverable_form: SemanticDeliverableForm = "generic_output"
    analysis_frame: SemanticAnalysisFrame = "generic"
    decision_posture: SemanticDecisionPosture = "generic"
    evidence_basis: list[SemanticEvidenceBasis] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    confidence: int = 0

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, value: Any) -> Any:
        if value is None:
            return 0
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 0
        if 0.0 <= numeric <= 1.0:
            return int(round(numeric * 100))
        return int(round(numeric))

    def planning_axes(self) -> dict[str, Any]:
        payload = self.model_dump(mode="python")
        return {field_name: payload.get(field_name) for field_name in self.CORE_PLANNING_AXIS_FIELDS}


class RepositorySemanticPack(BaseModel):
    repo_scope: Optional[Literal["full_repo", "selected_paths"]] = None
    dependency_focus: Optional[Literal["low", "medium", "high"]] = None
    execution_focus: Optional[Literal["none", "entrypoints", "runtime_surface"]] = None
    module_focus: Optional[Literal["none", "high_level", "detailed"]] = None
    review_rigour: Optional[Literal["light", "standard", "strict"]] = None


class ResearchSemanticPack(BaseModel):
    source_scope: Optional[Literal["web_only", "mixed", "curated"]] = None
    recency_requirement: Optional[Literal["latest", "recent", "broad"]] = None
    credibility_requirement: Optional[Literal["standard", "high"]] = None
    comparison_need: Optional[Literal["none", "light", "explicit"]] = None
    monitoring_mode: Optional[Literal["oneoff", "recurring"]] = None


class DataSemanticPack(BaseModel):
    data_source_kind: Optional[Literal["csv", "xlsx", "table", "mixed"]] = None
    aggregation_mode: Optional[Literal["summarize", "group", "compare", "score"]] = None
    metric_focus: Optional[Literal["explicit", "inferred", "unknown"]] = None
    output_granularity: Optional[Literal["row_level", "grouped", "executive_summary"]] = None
    validation_need: Optional[Literal["none", "schema", "consistency", "anomaly_check"]] = None


class SoftwareSemanticPack(BaseModel):
    artifact_type: Optional[Literal["webpage", "service", "cli", "library", "app_feature"]] = None
    interaction_mode: Optional[Literal["static", "interactive", "multi_page"]] = None
    implementation_scope: Optional[Literal["prototype", "usable", "production_like"]] = None
    deployment_target: Optional[Literal["local", "preview", "hosted"]] = None
    iteration_expectation: Optional[Literal["one_shot", "iterative"]] = None
    acceptance_mode: Optional[Literal["visual", "functional", "test_based"]] = None


class AutomationSemanticPack(BaseModel):
    cadence: Optional[Literal["oneoff", "recurring"]] = None
    coordination_need: Optional[Literal["low", "medium", "high"]] = None
    interactivity_level: Optional[Literal["none", "light", "heavy"]] = None
    external_dependency_profile: Optional[Literal["low", "medium", "high"]] = None
    operator_touchpoints: Optional[Literal["none", "optional", "required"]] = None


class DomainSemanticPackSet(BaseModel):
    repository_pack: Optional[RepositorySemanticPack] = None
    research_pack: Optional[ResearchSemanticPack] = None
    data_pack: Optional[DataSemanticPack] = None
    software_pack: Optional[SoftwareSemanticPack] = None
    automation_pack: Optional[AutomationSemanticPack] = None

    def trace_summary(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for field_name, value in self.model_dump(mode="python").items():
            if isinstance(value, dict) and any(item is not None for item in value.values()):
                payload[field_name] = value
        return payload


class DerivedIntentResolution(BaseModel):
    deliverable_contract: SemanticDeliverableContract = "generic_output"
    canonical_intent_aliases: list[str] = Field(default_factory=list)
    known_workflow_labels: list[str] = Field(default_factory=list)
    derivation_reason: str = "No canonical deliverable contract was derived from the current semantic axes."


class SemanticCore(BaseModel):
    LEGACY_CORE_FIELDS: ClassVar[tuple[str, ...]] = (
        "target_object",
        "primary_objective",
        "deliverable_form",
        "analysis_frame",
        "decision_posture",
        "workflow_shape",
        "source_modality",
        "evidence_basis",
        "ambiguities",
        "confidence",
    )
    DERIVED_INTENT_FIELDS: ClassVar[tuple[str, ...]] = ("deliverable_contract",)

    global_core: GlobalSemanticCore = Field(default_factory=GlobalSemanticCore)
    domain_packs: DomainSemanticPackSet = Field(default_factory=DomainSemanticPackSet)
    derived_intent: DerivedIntentResolution = Field(default_factory=DerivedIntentResolution)
    evidence_requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _lift_legacy_payload(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if "global_core" in value or "derived_intent" in value:
            return value
        passthrough_keys = {
            key
            for key in value.keys()
            if key
            not in {
                *cls.LEGACY_CORE_FIELDS,
                *cls.DERIVED_INTENT_FIELDS,
                "canonical_intent_aliases",
                "known_workflow_labels",
                "derivation_reason",
                "evidence_requirements",
            }
        }
        global_core = {
            "target_object": value.get("target_object", "generic_request"),
            "primary_objective": value.get("primary_objective", "deliver"),
            "workflow_shape": value.get("workflow_shape", "single_service"),
            "source_modality": value.get("source_modality", "unknown"),
            "deliverable_form": value.get("deliverable_form", "generic_output"),
            "analysis_frame": value.get("analysis_frame", "generic"),
            "decision_posture": value.get("decision_posture", "generic"),
            "evidence_basis": list(value.get("evidence_basis") or []),
            "ambiguities": list(value.get("ambiguities") or []),
            "confidence": value.get("confidence", 0),
        }
        derived_intent = {
            "deliverable_contract": value.get("deliverable_contract", "generic_output"),
            "canonical_intent_aliases": list(value.get("canonical_intent_aliases") or []),
            "known_workflow_labels": list(value.get("known_workflow_labels") or []),
            "derivation_reason": value.get("derivation_reason")
            or "No canonical deliverable contract was derived from the current semantic axes.",
        }
        return {
            "global_core": global_core,
            "domain_packs": value.get("domain_packs") or {},
            "derived_intent": derived_intent,
            "evidence_requirements": list(value.get("evidence_requirements") or []),
            **{key: value[key] for key in passthrough_keys},
        }

    @model_validator(mode="after")
    def _synchronize_derived_semantics(self) -> "SemanticCore":
        core = self.global_core
        derived = self.derived_intent
        data_pack = self.domain_packs.data_pack
        if derived.deliverable_contract == "architecture_report":
            if core.deliverable_form == "generic_output":
                core.deliverable_form = "report"
            if core.analysis_frame == "generic":
                core.analysis_frame = "architecture"
            if core.decision_posture == "generic":
                core.decision_posture = "descriptive"
            if not core.evidence_basis:
                core.evidence_basis = ["repo_structure", "repo_relationships", "repo_execution_surface"]
            if not self.evidence_requirements:
                self.evidence_requirements = ["repo.inventory", "repo.structure", "repo.evidence_bundle"]
            derived.derivation_reason = "Derived from repository target + report form + architecture analysis frame."
            derived.known_workflow_labels = ["repository_architecture_report"]
        elif derived.deliverable_contract == "research_briefing":
            if core.deliverable_form == "generic_output":
                core.deliverable_form = "briefing"
            if core.analysis_frame == "generic":
                core.analysis_frame = "summary"
            if core.decision_posture == "generic":
                core.decision_posture = "descriptive"
            if not core.evidence_basis:
                core.evidence_basis = ["local_documents"] if core.target_object == "local_workspace" else ["web_sources"]
            if not self.evidence_requirements:
                self.evidence_requirements = ["research.sources", "research.brief.draft"]
            derived.derivation_reason = (
                "Derived from summary analysis frame plus compatible briefing/report form over web or local-document evidence."
            )
            derived.known_workflow_labels = ["research_briefing"]
        elif derived.deliverable_contract == "dataset_summary":
            if core.deliverable_form == "generic_output":
                core.deliverable_form = "report"
            if core.analysis_frame == "generic":
                core.analysis_frame = "summary"
            if core.decision_posture == "generic":
                core.decision_posture = "descriptive"
            if not core.evidence_basis:
                core.evidence_basis = ["structured_data"]
            derived.derivation_reason = (
                "Derived from structured-data evidence over a dataset/local workspace with summary or assessment framing."
            )
            derived.known_workflow_labels = ["dataset_summary"]
        elif derived.deliverable_contract == "dataset_summary_report":
            if core.deliverable_form == "generic_output":
                core.deliverable_form = "report"
            if core.analysis_frame == "generic":
                core.analysis_frame = "summary"
            if core.decision_posture == "generic":
                core.decision_posture = "descriptive"
            if not core.evidence_basis:
                core.evidence_basis = ["structured_data"]
            derived.derivation_reason = (
                "Derived from structured-data evidence with a summary framing and a report delivery form."
            )
            derived.known_workflow_labels = ["dataset_summary_report"]
        elif derived.deliverable_contract == "dataset_validation_report":
            if core.deliverable_form == "generic_output":
                core.deliverable_form = "report"
            if core.analysis_frame == "generic":
                core.analysis_frame = "audit"
            if core.decision_posture == "generic":
                core.decision_posture = "descriptive"
            if not core.evidence_basis:
                core.evidence_basis = ["structured_data"]
            derived.derivation_reason = (
                "Derived from structured-data evidence with validation-oriented framing or schema-quality requirements."
            )
            derived.known_workflow_labels = ["dataset_validation_report"]
        elif derived.deliverable_contract == "dataset_comparison_report":
            if core.deliverable_form == "generic_output":
                core.deliverable_form = "report"
            if core.analysis_frame == "generic":
                core.analysis_frame = "summary"
            if core.decision_posture == "generic":
                core.decision_posture = "comparative"
            if not core.evidence_basis:
                core.evidence_basis = ["structured_data"]
            derived.derivation_reason = (
                "Derived from structured-data evidence with an explicit comparison posture and a report delivery form."
            )
            derived.known_workflow_labels = ["dataset_comparison_report"]
        elif derived.deliverable_contract == "artifact_bundle":
            if core.deliverable_form == "generic_output":
                core.deliverable_form = "artifact_bundle"
            if not core.evidence_basis:
                core.evidence_basis = ["artifact_payload"]
            if not self.evidence_requirements:
                self.evidence_requirements = ["artifact.saved"]
            derived.derivation_reason = "Derived from artifact-bundle delivery form."
            derived.known_workflow_labels = ["artifact_bundle_delivery"]

        if derived.deliverable_contract == "generic_output":
            if core.deliverable_form == "artifact_bundle":
                derived.deliverable_contract = "artifact_bundle"
                derived.derivation_reason = "Derived from artifact-bundle delivery form."
                derived.known_workflow_labels = ["artifact_bundle_delivery"]
            elif (
                core.target_object == "repository"
                and core.deliverable_form == "report"
                and core.analysis_frame == "architecture"
            ):
                derived.deliverable_contract = "architecture_report"
                derived.derivation_reason = "Derived from repository target + report form + architecture analysis frame."
                derived.known_workflow_labels = ["repository_architecture_report"]
            elif (
                core.analysis_frame == "summary"
                and (
                    (core.target_object == "web_resource" and core.deliverable_form in {"briefing", "report"})
                    or (core.target_object == "local_workspace" and core.deliverable_form == "briefing")
                )
            ):
                derived.deliverable_contract = "research_briefing"
                derived.derivation_reason = (
                    "Derived from summary analysis frame plus compatible briefing/report form over web or local-document evidence."
                )
                derived.known_workflow_labels = ["research_briefing"]
            elif (
                core.target_object in {"dataset", "local_workspace"}
                and "structured_data" in core.evidence_basis
                and core.deliverable_form == "report"
                and (
                    core.primary_objective == "validate"
                    or core.analysis_frame in {"audit", "assessment"}
                    or (data_pack is not None and data_pack.validation_need not in {None, "none"})
                )
            ):
                derived.deliverable_contract = "dataset_validation_report"
                derived.derivation_reason = (
                    "Derived from structured-data evidence plus validation-oriented framing or schema-quality requirements."
                )
                derived.known_workflow_labels = ["dataset_validation_report"]
            elif (
                core.target_object in {"dataset", "local_workspace"}
                and "structured_data" in core.evidence_basis
                and core.deliverable_form == "report"
                and (
                    core.decision_posture == "comparative"
                    or (data_pack is not None and data_pack.aggregation_mode == "compare")
                )
            ):
                derived.deliverable_contract = "dataset_comparison_report"
                derived.derivation_reason = (
                    "Derived from structured-data evidence with explicit comparison posture over a report workflow."
                )
                derived.known_workflow_labels = ["dataset_comparison_report"]
            elif (
                core.target_object in {"dataset", "local_workspace"}
                and "structured_data" in core.evidence_basis
                and core.deliverable_form == "report"
                and core.analysis_frame == "summary"
            ):
                derived.deliverable_contract = "dataset_summary_report"
                derived.derivation_reason = (
                    "Derived from structured-data evidence with summary framing and report delivery."
                )
                derived.known_workflow_labels = ["dataset_summary_report"]
            elif (
                core.target_object in {"dataset", "local_workspace"}
                and "structured_data" in core.evidence_basis
                and core.analysis_frame in {"summary", "assessment", "audit"}
                and core.deliverable_form in {"report", "artifact_bundle", "dataset"}
            ):
                derived.deliverable_contract = "dataset_summary"
                derived.derivation_reason = (
                    "Derived from structured-data evidence over a dataset/local workspace with summary or assessment framing."
                )
                derived.known_workflow_labels = ["dataset_summary"]
            else:
                derived.derivation_reason = "No canonical deliverable contract was derived from the current semantic axes."
                derived.known_workflow_labels = []
        if derived.deliverable_contract != "generic_output" and derived.deliverable_contract not in derived.canonical_intent_aliases:
            derived.canonical_intent_aliases = [derived.deliverable_contract, *derived.canonical_intent_aliases]
        return self

    @property
    def target_object(self) -> SemanticTargetObject:
        return self.global_core.target_object

    @target_object.setter
    def target_object(self, value: SemanticTargetObject) -> None:
        self.global_core.target_object = value

    @property
    def primary_objective(self) -> SemanticPrimaryObjective:
        return self.global_core.primary_objective

    @primary_objective.setter
    def primary_objective(self, value: SemanticPrimaryObjective) -> None:
        self.global_core.primary_objective = value

    @property
    def deliverable_contract(self) -> SemanticDeliverableContract:
        return self.derived_intent.deliverable_contract

    @deliverable_contract.setter
    def deliverable_contract(self, value: SemanticDeliverableContract) -> None:
        self.derived_intent.deliverable_contract = value

    @property
    def deliverable_form(self) -> SemanticDeliverableForm:
        return self.global_core.deliverable_form

    @deliverable_form.setter
    def deliverable_form(self, value: SemanticDeliverableForm) -> None:
        self.global_core.deliverable_form = value

    @property
    def analysis_frame(self) -> SemanticAnalysisFrame:
        return self.global_core.analysis_frame

    @analysis_frame.setter
    def analysis_frame(self, value: SemanticAnalysisFrame) -> None:
        self.global_core.analysis_frame = value

    @property
    def decision_posture(self) -> SemanticDecisionPosture:
        return self.global_core.decision_posture

    @decision_posture.setter
    def decision_posture(self, value: SemanticDecisionPosture) -> None:
        self.global_core.decision_posture = value

    @property
    def evidence_basis(self) -> list[SemanticEvidenceBasis]:
        return self.global_core.evidence_basis

    @evidence_basis.setter
    def evidence_basis(self, value: list[SemanticEvidenceBasis]) -> None:
        self.global_core.evidence_basis = value

    @property
    def workflow_shape(self) -> SemanticWorkflowShape:
        return self.global_core.workflow_shape

    @workflow_shape.setter
    def workflow_shape(self, value: SemanticWorkflowShape) -> None:
        self.global_core.workflow_shape = value

    @property
    def source_modality(self) -> SemanticSourceModality:
        return self.global_core.source_modality

    @source_modality.setter
    def source_modality(self, value: SemanticSourceModality) -> None:
        self.global_core.source_modality = value

    @property
    def ambiguities(self) -> list[str]:
        return self.global_core.ambiguities

    @ambiguities.setter
    def ambiguities(self, value: list[str]) -> None:
        self.global_core.ambiguities = value

    @property
    def confidence(self) -> int:
        return self.global_core.confidence

    @confidence.setter
    def confidence(self, value: int) -> None:
        self.global_core.confidence = value

    def trace_summary(self) -> dict[str, Any]:
        payload = {
            **self.global_core.model_dump(mode="python"),
            "deliverable_contract": self.derived_intent.deliverable_contract,
            "evidence_requirements": list(self.evidence_requirements),
            "global_core": self.global_core.model_dump(mode="python"),
            "domain_packs": self.domain_packs.trace_summary(),
            "derived_intent": self.derived_intent.model_dump(mode="python"),
        }
        return payload

    def planning_axes(self) -> dict[str, Any]:
        return self.global_core.planning_axes()

    def domain_pack_summary(self) -> dict[str, Any]:
        return self.domain_packs.trace_summary()

    def derived_intent_labels(self) -> dict[str, Any]:
        return self.derived_intent.model_dump(mode="python")

    def derived_contract_reason(self) -> str:
        return self.derived_intent.derivation_reason

    @model_serializer(mode="wrap")
    def _serialize_model(self, handler):
        payload = handler(self)
        payload.update(self.global_core.model_dump(mode="python"))
        payload["deliverable_contract"] = self.derived_intent.deliverable_contract
        payload["canonical_intent_aliases"] = list(self.derived_intent.canonical_intent_aliases)
        payload["known_workflow_labels"] = list(self.derived_intent.known_workflow_labels)
        payload["derivation_reason"] = self.derived_intent.derivation_reason
        return payload
