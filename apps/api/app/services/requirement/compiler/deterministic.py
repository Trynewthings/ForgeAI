from __future__ import annotations

import re
from typing import Any

from app.core.enums import BlueprintStatus, BlueprintType, RequirementCompilerMode
from app.db.models import Requirement
from app.schemas.blueprint import BlueprintCreateRequest
from app.schemas.requirement_payloads import (
    normalize_acceptance_criteria_payload,
    normalize_constraints_payload,
)
from app.services.requirement.compiler.contracts import (
    DeliverableIntentCandidate,
    CompilerMetadata,
    FamilyRecommendation,
    GraphClosureAssessment,
    HeuristicSignal,
    LLMStepPlanStep,
    LLMSemanticJudgementPayload,
    NormalizedRequirement,
    RequirementAnalysis,
    SemanticJudgementSummary,
    SkeletonDraft,
    StepPlanSummary,
    TemplateSelectionResult,
)
from app.services.capabilities.registry import CapabilityRegistryService
from app.schemas.capability_registry import CapabilityTypedWorkflowProposalResponse
from app.schemas.semantic_core import SemanticCore


class DeterministicRequirementAnalyzer:
    mode = RequirementCompilerMode.DETERMINISTIC

    def __init__(self, registry: CapabilityRegistryService | None = None) -> None:
        self.registry = registry or CapabilityRegistryService()

    def analyze(self, requirement: Requirement) -> RequirementAnalysis:
        normalized = self._normalize_requirement(requirement)
        gaps, open_questions = self._detect_gaps(normalized)
        semantic_interpretation, semantic_core_source, semantic_core_fallback_used, semantic_pending_fields = self._resolve_semantic_core(
            requirement=requirement,
            normalized=normalized,
        )
        return RequirementAnalysis(
            normalized=normalized,
            semantic_interpretation=semantic_interpretation,
            semantic_core_source=semantic_core_source,
            semantic_core_fallback_used=semantic_core_fallback_used,
            semantic_pending_fields=semantic_pending_fields,
            gaps=gaps,
            open_questions=open_questions,
            completeness_score=self._completeness_score(normalized, gaps),
        )

    def _normalize_requirement(self, requirement: Requirement) -> NormalizedRequirement:
        tools_payload = requirement.external_tools_needed_json or {}
        if isinstance(tools_payload, dict):
            tool_items = tools_payload.get("items") if isinstance(tools_payload.get("items"), list) else list(tools_payload.keys())
        elif isinstance(tools_payload, list):
            tool_items = tools_payload
        else:
            tool_items = []

        return NormalizedRequirement(
            title=(requirement.title or "").strip(),
            summary=(requirement.summary or "").strip(),
            goal=(requirement.goal or "").strip(),
            input_description=(requirement.input_description or "").strip(),
            output_description=(requirement.output_description or "").strip(),
            target_users=(requirement.target_users or "").strip(),
            constraints=normalize_constraints_payload(requirement.constraints_json),
            acceptance_criteria=normalize_acceptance_criteria_payload(requirement.acceptance_criteria_json),
            non_goals=requirement.non_goals_json or {},
            priority_features=requirement.priority_features_json or {},
            metadata=CompilerMetadata(
                domain=(requirement.domain or "").strip() or None,
                interactivity_level=(requirement.interactivity_level or "").strip() or None,
                external_tools_needed=[str(item).strip() for item in tool_items if str(item).strip()],
                human_review_required=bool(requirement.human_review_required),
                recurrence_mode=(requirement.recurrence_mode or "").strip() or None,
            ),
        )

    def _detect_gaps(self, normalized: NormalizedRequirement) -> tuple[list[str], list[str]]:
        gaps: list[str] = []
        open_questions: list[str] = []

        if not normalized.goal and not normalized.summary:
            gaps.append("goal_or_summary")
            open_questions.append("What primary outcome should this blueprint deliver?")
        if not normalized.input_description:
            gaps.append("input_description")
            open_questions.append("What input does the system receive?")
        if not normalized.output_description:
            gaps.append("output_description")
            open_questions.append("What output should the system produce?")
        if not normalized.acceptance_criteria.items:
            gaps.append("acceptance_criteria")
            open_questions.append("How will you decide whether the output is acceptable?")
        monitoring_text = " ".join(
            part
            for part in [
                normalized.title,
                normalized.summary,
                normalized.goal,
                normalized.input_description,
                normalized.output_description,
            ]
            if part
        ).lower()
        if (
            any(token in monitoring_text for token in ("monitor", "monitoring", "news"))
            and not normalized.metadata.recurrence_mode
            and not normalized.metadata.external_tools_needed
        ):
            gaps.append("monitoring_scope")
            open_questions.append("What recurrence and source/tool scope should this monitoring flow use?")
        if not normalized.metadata.domain:
            open_questions.append("Which domain or business context should the blueprint target?")
        return gaps, open_questions

    def _completeness_score(self, normalized: NormalizedRequirement, gaps: list[str]) -> int:
        base_fields = [
            normalized.title,
            normalized.goal or normalized.summary,
            normalized.input_description,
            normalized.output_description,
            normalized.acceptance_criteria.items,
        ]
        present = sum(1 for item in base_fields if item)
        score = int((present / len(base_fields)) * 100)
        return max(0, score - (len(gaps) * 5))

    def _resolve_semantic_core(
        self,
        *,
        requirement: Requirement,
        normalized: NormalizedRequirement,
    ) -> tuple[SemanticCore, str, bool, list[str]]:
        raw_semantic_core = ((requirement.structured_spec_json or {}).get("authoring_semantic_state") or {})
        semantic_core = self._load_semantic_core(raw_semantic_core)
        if semantic_core is not None:
            pending_fields = [
                str(item).strip()
                for item in (raw_semantic_core.get("pending_semantic_fields") or [])
                if str(item).strip()
            ]
            return semantic_core, "authoring_semantic_state", False, pending_fields
        fallback = self._infer_semantic_core_fallback(normalized)
        return fallback, "fallback_interpretation", True, []

    def _load_semantic_core(self, raw_semantic_core: dict[str, Any] | None) -> SemanticCore | None:
        if not isinstance(raw_semantic_core, dict) or not raw_semantic_core:
            return None
        try:
            semantic_core = SemanticCore.model_validate(raw_semantic_core)
        except Exception:
            return None
        return semantic_core

    def _semantic_core_is_complete(self, semantic_core: SemanticCore) -> bool:
        return bool(
            semantic_core.target_object
            and semantic_core.primary_objective
            and semantic_core.deliverable_form
            and semantic_core.analysis_frame
            and semantic_core.decision_posture
            and semantic_core.workflow_shape
            and semantic_core.source_modality
            and semantic_core.confidence > 0
        )

    def _infer_semantic_core_fallback(self, normalized: NormalizedRequirement):
        trace = normalized.trace_summary()
        combined = " ".join(
            str(trace.get(key) or "").strip()
            for key in ("title", "summary", "goal", "input_description", "output_description", "target_users", "domain")
            if str(trace.get(key) or "").strip()
        ).lower()
        source_plan = self.registry.infer_repository_source_plan(trace)
        has_repository_language = bool(re.search(r"\b(repository|repo|codebase)\b", combined)) or any(
            token in combined for token in ("仓库", "代码库", "代码仓库", "软件仓库")
        )
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
                "dataset",
                "dataframe",
                "metrics",
                "kpi",
                "数据表",
                "表格",
                "指标",
                "数据集",
                "电子表格",
            )
        )

        target_object = "generic_request"
        if has_repository_language:
            target_object = "repository"
        elif structured_data_language:
            target_object = "dataset"
        elif any(
            token in combined
            for token in (
                "web",
                "网页",
                "page",
                "search",
                "source",
                "research",
                "internet",
                "news",
                "article",
                "articles",
                "blog",
                "rss",
                "slack",
                "notion",
                "api",
            )
        ):
            target_object = "web_resource"
        elif any(token in combined for token in ("file", "文件", "path", "路径", "workspace", "目录")):
            target_object = "local_workspace"

        deliverable_form = "generic_output"
        if any(token in combined for token in ("briefing", "brief", "简报")):
            deliverable_form = "briefing"
        elif any(token in combined for token in ("report", "报告", "说明")):
            deliverable_form = "report"
        elif any(token in combined for token in ("artifact", "markdown", "文档", "文件", ".md", ".json", ".pdf")):
            deliverable_form = "artifact_bundle"

        analysis_frame = "generic"
        if any(token in combined for token in ("architecture report", "architectural", "architecture", "架构报告", "整体架构", "模块结构", "运行入口", "entrypoint", "entry point")):
            analysis_frame = "architecture"
        elif target_object == "dataset" and any(
            token in combined for token in ("compare", "comparison", "against", "versus", "对比", "比较", "across")
        ):
            analysis_frame = "summary"
        elif target_object == "dataset" and any(
            token in combined for token in ("validate", "validation", "schema", "missing required columns", "required columns", "校验", "验证", "缺失列", "必填列")
        ):
            analysis_frame = "audit"
        elif any(token in combined for token in ("audit", "risk", "hotspot", "审计", "风险", "热点")):
            analysis_frame = "audit"
        elif any(token in combined for token in ("whether this project is good", "is this project good", "评估", "evaluate", "assessment", "goodness", "是否好", "好不好")):
            analysis_frame = "assessment"
        elif any(token in combined for token in ("briefing", "summary", "summarize", "summarise", "digest", "概述", "总结", "说明", "简报", "摘要")):
            analysis_frame = "summary"

        decision_posture = "generic"
        if any(token in combined for token in ("whether this project is good", "is this project good", "评估", "evaluate", "assessment", "judge", "是否好", "好不好")):
            decision_posture = "evaluative"
        elif target_object == "dataset" and any(
            token in combined for token in ("compare", "comparison", "against", "versus", "对比", "比较", "across")
        ):
            decision_posture = "comparative"
        elif any(token in combined for token in ("recommend", "suggest", "改进建议", "建议", "improve")):
            decision_posture = "recommendation"
        elif deliverable_form in {"report", "briefing"} or analysis_frame != "generic":
            decision_posture = "descriptive"

        deliverable_contract = "generic_output"
        if target_object == "repository" and deliverable_form == "report" and analysis_frame == "architecture":
            deliverable_contract = "architecture_report"
        elif (
            analysis_frame == "summary"
            and (
                (target_object == "web_resource" and deliverable_form in {"briefing", "report"})
                or (
                    target_object == "local_workspace"
                    and deliverable_form == "briefing"
                    and any(token in combined for token in ("briefing", "brief", "简报", "notes", "notes", "document", "docs", "manual"))
                )
            )
        ):
            deliverable_contract = "research_briefing"
        elif (
            target_object in {"dataset", "local_workspace"}
            and structured_data_language
            and deliverable_form == "report"
            and analysis_frame == "summary"
            and decision_posture == "comparative"
        ):
            deliverable_contract = "dataset_comparison_report"
        elif (
            target_object in {"dataset", "local_workspace"}
            and structured_data_language
            and deliverable_form == "report"
            and analysis_frame in {"assessment", "audit"}
        ):
            deliverable_contract = "dataset_validation_report"
        elif (
            target_object in {"dataset", "local_workspace"}
            and structured_data_language
            and deliverable_form == "report"
            and analysis_frame == "summary"
        ):
            deliverable_contract = "dataset_summary_report"
        elif (
            target_object in {"dataset", "local_workspace"}
            and structured_data_language
            and deliverable_form in {"report", "artifact_bundle", "dataset"}
            and analysis_frame in {"summary", "assessment", "audit"}
        ):
            deliverable_contract = "dataset_summary"
        elif deliverable_form == "artifact_bundle":
            deliverable_contract = "artifact_bundle"

        primary_objective = "deliver"
        if target_object == "repository" and analysis_frame in {"architecture", "audit", "assessment"}:
            primary_objective = "analyze"
        elif any(
            token in combined
            for token in ("analyze", "analyse", "inspect", "review", "examine", "分析", "检查", "审查", "梳理")
        ):
            primary_objective = "analyze"
        elif any(token in combined for token in ("collect", "search", "gather", "抓取", "搜索", "收集")):
            primary_objective = "collect"
        elif target_object == "dataset" and any(
            token in combined for token in ("aggregate", "group", "compare", "score", "统计", "聚合", "对比", "评分")
        ):
            primary_objective = "transform"
        elif target_object == "dataset" and any(
            token in combined for token in ("validate", "validation", "schema", "missing required columns", "required columns", "校验", "验证", "缺失列", "必填列")
        ):
            primary_objective = "validate"
        elif analysis_frame in {"summary", "architecture"} or any(token in combined for token in ("summarize", "summary", "brief", "总结", "摘要", "报告")):
            primary_objective = "synthesize"
        elif analysis_frame in {"assessment", "audit"}:
            primary_objective = "validate"
        elif any(token in combined for token in ("summarize", "summary", "brief", "总结", "摘要", "报告")):
            primary_objective = "synthesize"

        source_modality = "unknown"
        modality = str(source_plan.get("source_modality") or "")
        if target_object == "repository" and modality in {"local_workspace_repo"}:
            source_modality = "workspace_repo"
        elif target_object == "repository" and modality in {"git_remote", "github_repo_url", "archive_url"}:
            source_modality = "remote_repo"
        elif target_object == "repository" and modality in {"ambiguous_source", "unsupported_source"}:
            source_modality = "unknown"
        elif target_object == "dataset":
            source_modality = "local_path"
        elif target_object == "local_workspace":
            source_modality = "local_path"
        elif target_object == "web_resource":
            source_modality = "web_source"

        evidence_basis: list[str] = []
        if target_object == "repository":
            if any(token in combined for token in ("module", "模块", "structure", "结构", "dependency", "依赖")) or analysis_frame in {"architecture", "assessment", "audit"}:
                evidence_basis.append("repo_structure")
            if any(token in combined for token in ("dependency", "依赖", "relationship", "关系")) or analysis_frame in {"architecture", "audit"}:
                evidence_basis.append("repo_relationships")
            if any(token in combined for token in ("runtime", "build", "entrypoint", "entry point", "运行", "构建", "入口", "config")) or analysis_frame in {"architecture", "audit"}:
                evidence_basis.append("repo_execution_surface")
        elif target_object == "web_resource":
            evidence_basis.append("web_sources")
        elif target_object == "dataset":
            evidence_basis.append("structured_data")
        elif target_object == "local_workspace":
            if structured_data_language:
                evidence_basis.append("structured_data")
            else:
                evidence_basis.append("local_documents")

        evidence_requirements: list[str] = []
        if deliverable_contract == "architecture_report":
            evidence_requirements = ["repo.inventory", "repo.structure", "repo.evidence_bundle"]
        elif deliverable_contract == "research_briefing":
            evidence_requirements = ["research.sources", "research.brief.draft"]
        elif deliverable_contract in {
            "dataset_summary",
            "dataset_summary_report",
            "dataset_validation_report",
            "dataset_comparison_report",
        }:
            evidence_requirements = []
        elif deliverable_contract == "artifact_bundle":
            evidence_requirements = ["artifact.saved"]
        elif target_object == "repository" and evidence_basis:
            evidence_requirements = ["repo.inventory", "repo.structure"]
            if "repo_relationships" in evidence_basis:
                evidence_requirements.append("repo.relationships")
            if "repo_execution_surface" in evidence_basis:
                evidence_requirements.append("repo.execution_surface")
        elif target_object == "web_resource":
            evidence_requirements = ["research.sources"]

        workflow_shape = "single_service"
        if (
            deliverable_contract in {"architecture_report", "research_briefing"}
            or target_object == "dataset"
            or len(evidence_requirements) > 1
        ):
            workflow_shape = "bounded_workflow"
        if normalized.metadata.human_review_required or "review" in combined or "审阅" in combined:
            workflow_shape = "review_gated_workflow"

        ambiguities: list[str] = []
        if target_object == "repository" and source_modality == "unknown":
            ambiguities.append("repository_source_modality")
        if deliverable_form == "generic_output" and (
            not normalized.output_description or analysis_frame != "generic" or target_object == "repository"
        ):
            ambiguities.append("deliverable_form")
        if deliverable_form == "report" and analysis_frame == "generic":
            ambiguities.append("analysis_frame")
        if deliverable_contract == "generic_output" and deliverable_form in {"report", "briefing"}:
            ambiguities.append("deliverable_contract")
        confidence = 45
        if deliverable_contract == "architecture_report":
            confidence += 10
        elif deliverable_contract == "dataset_summary":
            confidence += 6
        elif analysis_frame in {"assessment", "audit"} and deliverable_form == "report":
            confidence += 8
        if source_modality != "unknown":
            confidence += 5
        if workflow_shape != "single_service":
            confidence += 5

        domain_packs: dict[str, Any] = {}
        if target_object == "dataset" or "structured_data" in evidence_basis:
            aggregation_mode = "summarize"
            if any(token in combined for token in ("group", "grouped", "breakdown", "分组", "拆分")):
                aggregation_mode = "group"
            elif any(token in combined for token in ("compare", "comparison", "对比", "比较")):
                aggregation_mode = "compare"
            elif any(token in combined for token in ("score", "评分", "rank", "ranking")):
                aggregation_mode = "score"
            output_granularity = "executive_summary"
            if any(token in combined for token in ("group", "breakdown", "分组", "区域")):
                output_granularity = "grouped"
            elif any(token in combined for token in ("row", "行级", "逐条")):
                output_granularity = "row_level"
            data_source_kind = "mixed"
            if ".csv" in combined or "csv" in combined:
                data_source_kind = "csv"
            elif any(ext in combined for ext in (".xlsx", ".xls")) or any(token in combined for token in ("xlsx", "xls", "excel")):
                data_source_kind = "xlsx"
            elif any(token in combined for token in ("table", "tabular", "rows", "columns", "数据表", "表格")):
                data_source_kind = "table"
            validation_need = "none"
            if any(token in combined for token in ("anomaly", "异常")):
                validation_need = "anomaly_check"
            elif any(token in combined for token in ("schema", "列", "字段格式")):
                validation_need = "schema"
            elif any(token in combined for token in ("consistency", "一致性", "核对")):
                validation_need = "consistency"
            domain_packs["data_pack"] = {
                "data_source_kind": data_source_kind,
                "aggregation_mode": aggregation_mode,
                "metric_focus": "explicit" if any(token in combined for token in ("top", "revenue", "metric", "指标", "统计", "kpi")) else "inferred",
                "output_granularity": output_granularity,
                "validation_need": validation_need,
            }

        return SemanticCore.model_validate(
            {
                "target_object": target_object,
                "primary_objective": primary_objective,
                "deliverable_contract": deliverable_contract,
                "deliverable_form": deliverable_form,
                "analysis_frame": analysis_frame,
                "decision_posture": decision_posture,
                "evidence_requirements": evidence_requirements,
                "evidence_basis": evidence_basis,
                "workflow_shape": workflow_shape,
                "source_modality": source_modality,
                "ambiguities": ambiguities,
                "confidence": min(100, confidence),
                "domain_packs": domain_packs,
            }
        )


class DeterministicFamilyRecommender:
    mode = RequirementCompilerMode.DETERMINISTIC

    def __init__(self, registry: CapabilityRegistryService | None = None) -> None:
        self.registry = registry or CapabilityRegistryService()

    def recommend(
        self,
        analysis: RequirementAnalysis,
        *,
        family_hint: BlueprintType | None = None,
        allow_family_override: bool = True,
    ) -> FamilyRecommendation:
        normalized = analysis.normalized
        metadata = normalized.metadata
        domain_text = " ".join(
            part for part in [normalized.title, normalized.summary, normalized.goal, metadata.domain or ""] if part
        ).lower()
        normalized_trace = normalized.trace_summary()
        deliverable_intent_candidates = self.registry.infer_deliverable_intent_candidates(
            normalized_requirement=normalized_trace,
            semantic_interpretation=analysis.semantic_interpretation.trace_summary(),
        )
        top_intent = deliverable_intent_candidates[0] if deliverable_intent_candidates else None
        semantic = analysis.semantic_interpretation
        tools_count = len(metadata.external_tools_needed)
        interactivity = (metadata.interactivity_level or "").lower()
        recurrence_mode = (metadata.recurrence_mode or "").lower()
        workflow_shape_override_reason: str | None = None
        workflow_shape_aligned = True
        semantic_workflow_shape = semantic.workflow_shape or None
        derived_intent_label = (
            semantic.deliverable_contract if semantic.deliverable_contract and semantic.deliverable_contract != "generic_output" else None
        )

        signals = [
            HeuristicSignal(
                key="typed_repository_analysis_intent",
                matched=bool(
                    top_intent is not None
                    and top_intent.intent_id == "architecture_report"
                    and top_intent.workflow_available
                ),
                target_family=BlueprintType.WORKFLOW,
                weight=88,
                detail="Capability semantics identified a repository-analysis architecture-report workflow.",
            ),
            HeuristicSignal(
                key="explicit_multi_agent_keyword",
                matched="multi-agent" in domain_text or "multi agent" in domain_text,
                target_family=BlueprintType.MULTI_AGENT,
                weight=100,
                detail="Domain/title/goal explicitly mentions multi-agent behavior.",
            ),
            HeuristicSignal(
                key="collaborative_interactivity",
                matched=interactivity in {"collaborative", "agentic"},
                target_family=BlueprintType.MULTI_AGENT,
                weight=95,
                detail="Interactivity level requires collaborative or agentic coordination.",
            ),
            HeuristicSignal(
                key="high_interactivity_with_review_or_tools",
                matched=interactivity == "high" and (metadata.human_review_required or tools_count >= 1),
                target_family=BlueprintType.MULTI_AGENT,
                weight=90,
                detail="High interactivity combined with review/tools suggests a coordinated multi-agent skeleton.",
            ),
            HeuristicSignal(
                key="semantic_workflow_shape",
                matched=semantic.workflow_shape in {"bounded_workflow", "review_gated_workflow"},
                target_family=BlueprintType.WORKFLOW,
                weight=94,
                detail=(
                    f"Semantic interpretation identified workflow shape '{semantic.workflow_shape}', "
                    "which requires a bounded workflow graph."
                ),
            ),
            HeuristicSignal(
                key="semantic_deliverable_contract",
                matched=semantic.deliverable_contract in {
                    "architecture_report",
                    "research_briefing",
                    "dataset_summary_report",
                    "dataset_validation_report",
                    "dataset_comparison_report",
                },
                target_family=BlueprintType.WORKFLOW,
                weight=90,
                detail=(
                    f"Semantic interpretation identified deliverable contract '{semantic.deliverable_contract}', "
                    "which maps to a multi-step workflow."
                ),
            ),
            HeuristicSignal(
                key="semantic_single_service_shape",
                matched=(
                    semantic.workflow_shape == "single_service"
                    and semantic.confidence >= 50
                    and recurrence_mode not in {"daily", "weekly", "scheduled", "recurring"}
                    and tools_count == 0
                    and interactivity not in {"high", "collaborative", "agentic"}
                    and not metadata.human_review_required
                ),
                target_family=BlueprintType.SINGLE_SERVICE,
                weight=92,
                detail="Semantic interpretation indicates a bounded single-service interaction without an evidence-chain workflow.",
            ),
            HeuristicSignal(
                key="recurring_execution",
                matched=recurrence_mode in {"daily", "weekly", "scheduled", "recurring"},
                target_family=BlueprintType.WORKFLOW,
                weight=85,
                detail="Recurring execution maps best to a workflow skeleton.",
            ),
            HeuristicSignal(
                key="multiple_external_tools",
                matched=tools_count >= 2,
                target_family=BlueprintType.WORKFLOW,
                weight=80,
                detail="Multiple external tools suggest a staged workflow.",
            ),
            HeuristicSignal(
                key="pipeline_language",
                matched=any(token in domain_text for token in ("workflow", "pipeline", "digest", "report", "工作流", "报告", "分析")),
                target_family=BlueprintType.WORKFLOW,
                weight=75,
                detail="Requirement language suggests a pipeline-style workflow.",
            ),
            HeuristicSignal(
                key="high_interactivity",
                matched=interactivity == "high",
                target_family=BlueprintType.WORKFLOW,
                weight=70,
                detail="High interactivity raises the draft from single service to workflow.",
            ),
        ]

        if semantic.workflow_shape == "single_service":
            override_signal_keys = {
                "explicit_multi_agent_keyword",
                "collaborative_interactivity",
                "high_interactivity_with_review_or_tools",
                "recurring_execution",
                "multiple_external_tools",
                "high_interactivity",
            }
            filtered_signals: list[HeuristicSignal] = []
            for signal in signals:
                if not signal.matched:
                    filtered_signals.append(signal)
                    continue
                if signal.target_family in {BlueprintType.WORKFLOW, BlueprintType.MULTI_AGENT} and signal.key not in override_signal_keys:
                    filtered_signals.append(signal.model_copy(update={"matched": False}))
                    continue
                filtered_signals.append(signal)
            signals = filtered_signals

        matched_signal = next((signal for signal in signals if signal.matched), None)
        if matched_signal is None:
            recommended_family = BlueprintType.SINGLE_SERVICE
            reason = "Requirement looks like a single bounded service interaction."
            confidence = 10
        else:
            recommended_family = matched_signal.target_family or BlueprintType.SINGLE_SERVICE
            reason = matched_signal.detail
            confidence = matched_signal.weight

        expected_family = None
        if semantic.workflow_shape == "single_service":
            expected_family = BlueprintType.SINGLE_SERVICE
        elif semantic.workflow_shape in {"bounded_workflow", "review_gated_workflow"}:
            expected_family = BlueprintType.WORKFLOW

        if expected_family is not None and recommended_family != expected_family:
            workflow_shape_aligned = False
            if matched_signal is not None:
                workflow_shape_override_reason = (
                    f"Semantic workflow shape '{semantic.workflow_shape}' was upgraded to {recommended_family.value} "
                    f"because '{matched_signal.key}' introduced an explicit higher-level coordination need."
                )
            else:
                workflow_shape_override_reason = (
                    f"Semantic workflow shape '{semantic.workflow_shape}' did not directly determine the family selection."
                )

        selected_family = recommended_family
        override_applied = False
        warnings: list[str] = []

        if family_hint is not None and not allow_family_override:
            selected_family = family_hint
            override_applied = recommended_family != family_hint
            if override_applied:
                warnings.append(
                    f"family_hint locked selection to {family_hint.value}; deterministic heuristics recommended {recommended_family.value}."
                )
                reason = (
                    f"Operator locked the compile to {family_hint.value}; deterministic heuristics recommended "
                    f"{recommended_family.value} because {reason.lower()}"
                )
            else:
                reason = f"Operator locked the compile to {family_hint.value}, which matched the deterministic recommendation."
        elif family_hint is not None and recommended_family == BlueprintType.SINGLE_SERVICE:
            selected_family = family_hint
            override_applied = family_hint != recommended_family
            if override_applied:
                reason = (
                    f"family_hint promoted the draft to {family_hint.value} because heuristics only reached the default "
                    "single-service fallback."
                )
            else:
                reason = "family_hint matched the deterministic recommendation."
        elif family_hint is not None and family_hint != recommended_family:
            warnings.append(
                f"family_hint requested {family_hint.value}, but deterministic heuristics selected {recommended_family.value}."
            )

        # Auto-upgrade: single_service has no working runtime for domains with
        # concrete workflow skeletons. Only upgrade when target_object has a known skeleton.
        if selected_family == BlueprintType.SINGLE_SERVICE and not (family_hint is not None and not allow_family_override):
            has_workflow_skeleton = (
                semantic.target_object in ("repository", "dataset")
                or semantic.deliverable_contract in (
                    "architecture_report", "research_briefing",
                    "dataset_summary_report", "dataset_validation_report",
                    "dataset_comparison_report",
                )
            )
            if has_workflow_skeleton:
                selected_family = BlueprintType.WORKFLOW
                override_applied = True
                reason = (
                    f"Auto-upgraded from single_service to workflow: "
                    f"domain '{semantic.target_object}' has workflow skeleton support. "
                    f"single_service has no working runtime."
                )
                warnings.append("Family auto-upgraded from single_service to workflow.")

        return FamilyRecommendation(
            recommended_family=recommended_family,
            selected_family=selected_family,
            reason=reason,
            family_hint=family_hint,
            allow_family_override=allow_family_override,
            override_applied=override_applied,
            confidence=confidence,
            heuristic_signals=signals,
            warnings=warnings,
            deterministic_recommended_family=recommended_family,
            deterministic_reason=reason,
            semantic_workflow_shape=semantic_workflow_shape,
            workflow_shape_aligned=workflow_shape_aligned,
            workflow_shape_override_reason=workflow_shape_override_reason,
            derived_intent_label=derived_intent_label,
        )


class DeterministicGraphClosureAssessor:
    REPOSITORY_SKELETON_VOCABULARY = [
        "repository_architecture_report",
        "repository_assessment_report",
        "repository_audit_summary",
        "repository_summary_report",
    ]
    DATA_SKELETON_VOCABULARY = [
        "dataset_summary_report",
        "dataset_validation_report",
        "dataset_comparison_report",
        "dataset_transform_delivery",
    ]
    ALLOWED_CLARIFICATION_AXES = {
        "target_object",
        "source_locator",
        "source_modality",
        "deliverable_form",
        "analysis_frame",
        "decision_posture",
        "evidence_basis",
        "workflow_shape",
        "delivery_target",
    }

    def candidate_skeleton_vocabulary(
        self,
        *,
        semantic: SemanticCore,
    ) -> list[str]:
        if semantic.target_object == "repository" and semantic.deliverable_form in {"report", "briefing", "generic_output"}:
            return list(self.REPOSITORY_SKELETON_VOCABULARY)
        data_pack = semantic.domain_pack_summary().get("data_pack") if hasattr(semantic, "domain_pack_summary") else None
        if semantic.target_object in {"dataset", "local_workspace"} and (
            "structured_data" in set(semantic.evidence_basis or [])
            or bool(data_pack)
        ):
            return list(self.DATA_SKELETON_VOCABULARY)
        return []

    def _repo_skeleton_supported(self, semantic: SemanticCore, skeleton_id: str) -> bool:
        evidence_basis = set(semantic.evidence_basis or [])
        if semantic.target_object != "repository":
            return False
        if skeleton_id == "repository_architecture_report":
            return (
                semantic.deliverable_form == "report"
                and semantic.analysis_frame == "architecture"
                and bool(evidence_basis & {"repo_structure", "repo_relationships", "repo_execution_surface"})
            )
        if skeleton_id == "repository_assessment_report":
            return (
                semantic.deliverable_form == "report"
                and semantic.analysis_frame == "assessment"
                and semantic.decision_posture in {"evaluative", "recommendation"}
                and "repo_structure" in evidence_basis
            )
        if skeleton_id == "repository_audit_summary":
            return (
                semantic.deliverable_form in {"report", "briefing"}
                and semantic.analysis_frame in {"audit", "assessment"}
                and semantic.decision_posture in {"evaluative", "recommendation"}
                and bool(evidence_basis & {"repo_structure", "repo_relationships", "repo_execution_surface"})
            )
        if skeleton_id == "repository_summary_report":
            return (
                semantic.deliverable_form in {"report", "briefing"}
                and semantic.analysis_frame == "summary"
                and semantic.decision_posture == "descriptive"
                and bool(evidence_basis & {"repo_structure", "repo_execution_surface"})
            )
        return False

    def _data_skeleton_supported(self, semantic: SemanticCore, skeleton_id: str) -> bool:
        evidence_basis = set(semantic.evidence_basis or [])
        if skeleton_id == "dataset_validation_report":
            return (
                semantic.deliverable_form == "report"
                and semantic.target_object in {"dataset", "local_workspace"}
                and "structured_data" in evidence_basis
                and (
                    semantic.primary_objective == "validate"
                    or semantic.analysis_frame in {"audit", "assessment"}
                    or (
                        semantic.domain_packs.data_pack is not None
                        and semantic.domain_packs.data_pack.validation_need not in {None, "none"}
                    )
                )
            )
        if skeleton_id == "dataset_comparison_report":
            return (
                semantic.deliverable_form == "report"
                and semantic.target_object in {"dataset", "local_workspace"}
                and "structured_data" in evidence_basis
                and (
                    semantic.decision_posture == "comparative"
                    or (
                        semantic.domain_packs.data_pack is not None
                        and semantic.domain_packs.data_pack.aggregation_mode == "compare"
                    )
                )
            )
        if skeleton_id == "dataset_summary_report":
            return (
                semantic.deliverable_form == "report"
                and semantic.target_object in {"dataset", "local_workspace"}
                and "structured_data" in evidence_basis
                and semantic.analysis_frame == "summary"
                and semantic.decision_posture != "comparative"
            )
        if skeleton_id == "dataset_transform_delivery":
            return (
                semantic.target_object in {"dataset", "local_workspace"}
                and "structured_data" in evidence_basis
                and semantic.primary_objective == "transform"
                and semantic.deliverable_form in {"artifact_bundle", "dataset", "generic_output"}
            )
        return False

    def _candidate_divergence_axes(self, skeleton_id: str) -> set[str]:
        if skeleton_id in {"repository_architecture_report", "repository_summary_report"}:
            return {"analysis_frame"}
        if skeleton_id in {"repository_assessment_report", "repository_audit_summary"}:
            return {"analysis_frame", "decision_posture"}
        if skeleton_id in {"dataset_summary_report", "dataset_validation_report"}:
            return {"analysis_frame"}
        if skeleton_id == "dataset_comparison_report":
            return {"analysis_frame", "decision_posture"}
        if skeleton_id == "dataset_transform_delivery":
            return {"deliverable_form"}
        return set()

    def validate_semantic_judgement(
        self,
        *,
        base_semantic: SemanticCore,
        payload: LLMSemanticJudgementPayload | None,
        candidate_skeleton_vocabulary: list[str],
    ) -> SemanticJudgementSummary:
        if payload is None:
            return SemanticJudgementSummary(
                used=False,
                source="deterministic_fallback",
                fallback_used=True,
                candidate_skeleton_vocabulary=list(candidate_skeleton_vocabulary),
            )

        repair_notes: list[str] = []
        refined_semantic = base_semantic.model_copy(deep=True)
        if payload.refined_global_core is not None:
            refined_semantic.global_core = payload.refined_global_core.model_copy(deep=True)
        if payload.refined_domain_packs is not None:
            refined_semantic.domain_packs = payload.refined_domain_packs.model_copy(deep=True)
        refined_semantic = SemanticCore.model_validate(refined_semantic.model_dump(mode="python"))

        allowed_candidates = list(candidate_skeleton_vocabulary)
        candidate_ids: list[str] = []
        for item in payload.candidate_skeleton_ids:
            normalized = str(item).strip()
            if not normalized:
                continue
            if normalized not in allowed_candidates:
                repair_notes.append(f"Dropped candidate skeleton '{normalized}' because it is outside the bounded vocabulary.")
                continue
            if normalized not in candidate_ids:
                candidate_ids.append(normalized)

        dominant_skeleton_id = payload.dominant_skeleton_id
        if dominant_skeleton_id:
            dominant_skeleton_id = str(dominant_skeleton_id).strip()
        if dominant_skeleton_id and dominant_skeleton_id not in allowed_candidates:
            repair_notes.append(
                f"Cleared dominant skeleton '{dominant_skeleton_id}' because it is outside the bounded vocabulary."
            )
            dominant_skeleton_id = None
        if dominant_skeleton_id and dominant_skeleton_id not in candidate_ids:
            candidate_ids.append(dominant_skeleton_id)
            repair_notes.append(
                f"Added dominant skeleton '{dominant_skeleton_id}' into the validated candidate list for consistency."
            )

        clarification_axes: list[str] = []
        for item in payload.clarification_axes:
            normalized = str(item).strip()
            if not normalized:
                continue
            if normalized not in self.ALLOWED_CLARIFICATION_AXES:
                repair_notes.append(f"Dropped clarification axis '{normalized}' because it is not allowed.")
                continue
            if normalized not in clarification_axes:
                clarification_axes.append(normalized)

        validated_candidate_ids: list[str] = []
        for candidate_id in candidate_ids:
            supported = True
            if candidate_id in self.REPOSITORY_SKELETON_VOCABULARY:
                supported = self._repo_skeleton_supported(refined_semantic, candidate_id)
            elif candidate_id in self.DATA_SKELETON_VOCABULARY:
                supported = self._data_skeleton_supported(refined_semantic, candidate_id)
            if supported:
                validated_candidate_ids.append(candidate_id)
                continue
            if self._candidate_divergence_axes(candidate_id) & set(clarification_axes):
                validated_candidate_ids.append(candidate_id)
                continue
            repair_notes.append(
                f"Dropped candidate skeleton '{candidate_id}' because the refined semantic state does not support it."
            )
        candidate_ids = validated_candidate_ids

        if dominant_skeleton_id in self.REPOSITORY_SKELETON_VOCABULARY and not self._repo_skeleton_supported(
            refined_semantic, dominant_skeleton_id
        ):
            repair_notes.append(
                f"Cleared {dominant_skeleton_id} because the refined semantic state does not support it."
            )
            candidate_ids = [item for item in candidate_ids if item != dominant_skeleton_id]
            dominant_skeleton_id = None

        if dominant_skeleton_id in self.DATA_SKELETON_VOCABULARY and not self._data_skeleton_supported(
            refined_semantic, dominant_skeleton_id
        ):
            repair_notes.append(
                f"Cleared {dominant_skeleton_id} because the refined semantic state does not support it."
            )
            candidate_ids = [item for item in candidate_ids if item != dominant_skeleton_id]
            dominant_skeleton_id = None

        used = bool(candidate_ids or clarification_axes or payload.refined_global_core is not None or payload.refined_domain_packs is not None)
        return SemanticJudgementSummary(
            used=used,
            source="llm_validated" if used else "deterministic_fallback",
            fallback_used=not used,
            candidate_skeleton_vocabulary=list(allowed_candidates),
            candidate_skeleton_ids=candidate_ids,
            dominant_skeleton_id=dominant_skeleton_id,
            clarification_axes=clarification_axes,
            semantic_uncertainties=[str(item).strip() for item in payload.semantic_uncertainties if str(item).strip()],
            judgement_confidence=max(0, min(100, int(payload.judgement_confidence))),
            reasoning_summary=payload.reasoning_summary.strip(),
            validator_repair_notes=repair_notes,
            refined_semantic_interpretation=refined_semantic,
        )

    def _candidate_skeleton_ids(
        self,
        *,
        semantic: SemanticCore,
        semantic_judgement: SemanticJudgementSummary | None = None,
    ) -> list[str]:
        if semantic_judgement is not None and semantic_judgement.used and semantic_judgement.candidate_skeleton_ids:
            return list(semantic_judgement.candidate_skeleton_ids)
        candidates: list[str] = []
        data_pack = semantic.domain_pack_summary().get("data_pack") if hasattr(semantic, "domain_pack_summary") else None

        if semantic.deliverable_contract == "architecture_report":
            candidates.append("repository_architecture_report")
        elif semantic.deliverable_contract == "research_briefing":
            candidates.append("research_briefing")
        elif semantic.deliverable_contract == "dataset_summary":
            candidates.append("dataset_summary")
        elif semantic.deliverable_contract == "dataset_summary_report":
            candidates.append("dataset_summary_report")
        elif semantic.deliverable_contract == "dataset_validation_report":
            candidates.append("dataset_validation_report")
        elif semantic.deliverable_contract == "dataset_comparison_report":
            candidates.append("dataset_comparison_report")
        elif semantic.deliverable_contract == "artifact_bundle":
            candidates.append("artifact_bundle_delivery")

        if semantic.target_object == "repository" and semantic.deliverable_form == "report":
            if semantic.analysis_frame in {"generic", "architecture"}:
                candidates.append("repository_architecture_report")
            if semantic.analysis_frame in {"generic", "assessment"}:
                candidates.append("repository_assessment_report")
            if semantic.analysis_frame in {"generic", "audit"}:
                candidates.append("repository_audit_summary")
            if semantic.analysis_frame in {"generic", "summary"}:
                candidates.append("repository_summary_report")

        if (
            semantic.target_object == "web_resource"
            and semantic.analysis_frame in {"generic", "summary"}
            and semantic.deliverable_form in {"generic_output", "report", "briefing"}
        ):
            candidates.append("research_briefing")

        if (
            semantic.target_object in {"dataset", "local_workspace"}
            and (
                "structured_data" in set(semantic.evidence_basis or [])
                or bool(data_pack)
            )
            and semantic.analysis_frame in {"generic", "summary", "assessment", "audit"}
            and semantic.deliverable_form in {"generic_output", "report", "artifact_bundle", "dataset"}
        ):
            specific_contract = semantic.deliverable_contract in {
                "dataset_summary_report",
                "dataset_validation_report",
                "dataset_comparison_report",
            }
            if not specific_contract:
                candidates.append("dataset_summary")
            if (
                semantic.deliverable_form == "report"
                and semantic.analysis_frame in {"generic", "summary"}
                and semantic.deliverable_contract not in {"dataset_validation_report", "dataset_comparison_report"}
            ):
                candidates.append("dataset_summary_report")
            if semantic.deliverable_form == "report" and (
                semantic.primary_objective == "validate"
                or semantic.analysis_frame in {"audit", "assessment"}
                or semantic.analysis_frame == "generic"
                or (data_pack and data_pack.get("validation_need") not in {None, "none"})
            ) and semantic.deliverable_contract != "dataset_comparison_report":
                candidates.append("dataset_validation_report")
            if semantic.deliverable_form == "report" and (
                semantic.decision_posture == "comparative"
                or (data_pack and data_pack.get("aggregation_mode") == "compare")
            ) and semantic.deliverable_contract != "dataset_validation_report":
                candidates.append("dataset_comparison_report")
            if semantic.primary_objective == "transform" and semantic.deliverable_form in {"artifact_bundle", "dataset", "generic_output"}:
                candidates.append("dataset_transform_delivery")

        if semantic.deliverable_form == "artifact_bundle":
            candidates.append("artifact_bundle_delivery")
        elif (
            semantic.target_object == "generic_request"
            and semantic.deliverable_form == "generic_output"
            and semantic.primary_objective in {"analyze", "synthesize", "validate"}
        ):
            candidates.append("artifact_bundle_delivery")

        if (
            semantic.target_object == "generic_request"
            and semantic.deliverable_form == "generic_output"
            and semantic.analysis_frame == "generic"
        ):
            candidates.append("single_service_response")

        deduped: list[str] = []
        for candidate in candidates:
            if candidate not in deduped:
                deduped.append(candidate)
        return deduped

    def _divergence_blocking_axes(
        self,
        *,
        semantic: SemanticCore,
        candidate_skeleton_ids: list[str],
        family: BlueprintType,
    ) -> tuple[list[str], dict[str, str]]:
        axes: list[str] = []
        reasons: dict[str, str] = {}

        repository_skeletons = {
            candidate
            for candidate in candidate_skeleton_ids
            if candidate in {
                "repository_architecture_report",
                "repository_assessment_report",
                "repository_audit_summary",
                "repository_summary_report",
            }
        }
        if len(repository_skeletons) > 1:
            axes.append("analysis_frame")
            reasons["analysis_frame"] = (
                "Multiple repository report skeletons remain plausible: " + ", ".join(sorted(repository_skeletons)) + "."
            )

        deliverable_skeletons = {
            candidate
            for candidate in candidate_skeleton_ids
            if candidate in {
                "research_briefing",
                "dataset_summary",
                "dataset_summary_report",
                "dataset_validation_report",
                "dataset_comparison_report",
                "dataset_transform_delivery",
                "artifact_bundle_delivery",
                "single_service_response",
            }
        }
        if (
            len(deliverable_skeletons) > 1
            and semantic.deliverable_form == "generic_output"
            and family != BlueprintType.MULTI_AGENT
        ):
            axes.append("deliverable_form")
            reasons["deliverable_form"] = (
                "Multiple delivery-oriented skeletons remain plausible: " + ", ".join(sorted(deliverable_skeletons)) + "."
            )

        if (
            {"research_briefing", "dataset_summary"}.issubset(set(candidate_skeleton_ids))
            and "structured_data" not in set(semantic.evidence_basis or [])
            and "web_sources" not in set(semantic.evidence_basis or [])
        ):
            if "evidence_basis" not in axes:
                axes.append("evidence_basis")
            reasons["evidence_basis"] = (
                "Research-style and structured-data skeletons are both still plausible without a dominant evidence basis."
            )

        data_skeletons = {
            candidate
            for candidate in candidate_skeleton_ids
            if candidate in {
                "dataset_summary_report",
                "dataset_validation_report",
                "dataset_comparison_report",
                "dataset_transform_delivery",
            }
        }
        if len(data_skeletons) > 1:
            if any(item in data_skeletons for item in {"dataset_summary_report", "dataset_validation_report"}):
                axes.append("analysis_frame")
                reasons["analysis_frame"] = (
                    "Multiple data-report skeletons remain plausible: " + ", ".join(sorted(data_skeletons)) + "."
                )
            if any(item in data_skeletons for item in {"dataset_summary_report", "dataset_comparison_report"}):
                if "decision_posture" not in axes:
                    axes.append("decision_posture")
                reasons["decision_posture"] = (
                    "Data summary and comparison skeletons are both still plausible until the comparison posture is explicit."
                )
            if "dataset_transform_delivery" in data_skeletons and semantic.deliverable_form == "generic_output":
                if "deliverable_form" not in axes:
                    axes.append("deliverable_form")
                reasons["deliverable_form"] = (
                    "Data transform delivery remains plausible until the final output form is explicit."
                )

        return axes, reasons

    def assess(
        self,
        analysis: RequirementAnalysis,
        recommendation: FamilyRecommendation,
    ) -> GraphClosureAssessment:
        semantic_judgement = analysis.semantic_judgement
        semantic = (
            semantic_judgement.refined_semantic_interpretation
            if semantic_judgement is not None and semantic_judgement.refined_semantic_interpretation is not None
            else analysis.semantic_interpretation
        )
        normalized = analysis.normalized
        source_plan = CapabilityRegistryService().infer_repository_source_plan(normalized.trace_summary())
        pending_fields = {
            str(item).strip()
            for item in (analysis.semantic_pending_fields or [])
            if str(item).strip()
        }
        family = recommendation.selected_family
        blocking_axes: list[str] = []
        unresolved_ambiguities: list[str] = []
        review_reasons: list[str] = []
        blocking_axis_reasons: dict[str, str] = {}
        candidate_skeleton_ids = self._candidate_skeleton_ids(semantic=semantic, semantic_judgement=semantic_judgement)
        candidate_skeleton_vocabulary = (
            list(semantic_judgement.candidate_skeleton_vocabulary)
            if semantic_judgement is not None and semantic_judgement.candidate_skeleton_vocabulary
            else self.candidate_skeleton_vocabulary(semantic=semantic)
        )
        graph_specific_evidence_basis = {
            item
            for item in semantic.evidence_basis
            if item not in {"artifact_payload"}
        }
        workflow_requires_explicit_source = family == BlueprintType.WORKFLOW and (
            semantic.primary_objective in {"analyze", "collect", "transform", "synthesize", "validate"}
            or semantic.analysis_frame != "generic"
            or bool(graph_specific_evidence_basis)
            or semantic.deliverable_contract in {"architecture_report", "research_briefing"}
        )
        deliverable_form_blocks = semantic.deliverable_form == "generic_output" and (
            not normalized.output_description
            or semantic.target_object == "repository"
            or (
                workflow_requires_explicit_source
                and semantic.target_object not in {"local_workspace", "web_resource"}
            )
            or (
                semantic.analysis_frame != "generic"
                and semantic.target_object == "repository"
            )
            or (
                bool(semantic.evidence_basis)
                and semantic.target_object == "repository"
            )
        )

        def block(axis: str, ambiguity: str | None = None) -> None:
            if axis not in blocking_axes:
                blocking_axes.append(axis)
            if ambiguity and ambiguity not in unresolved_ambiguities:
                unresolved_ambiguities.append(ambiguity)

        def block_with_reason(axis: str, reason: str, ambiguity: str | None = None) -> None:
            block(axis, ambiguity)
            blocking_axis_reasons.setdefault(axis, reason)

        if not normalized.input_description and (
            semantic.target_object != "generic_request"
            or family != BlueprintType.SINGLE_SERVICE
            or bool(semantic.evidence_basis)
            or semantic.confidence < 30
            or not normalized.output_description
        ):
            block_with_reason("source_locator", "The graph start point is still unknown because no concrete source locator was provided.", "source_locator")
        if not normalized.output_description and (
            semantic.deliverable_form != "generic_output"
            or semantic.analysis_frame != "generic"
            or family != BlueprintType.SINGLE_SERVICE
            or bool(semantic.evidence_basis)
            or semantic.confidence < 30
        ):
            block_with_reason("delivery_target", "The final delivery path cannot be chosen because the concrete output target is still missing.", "delivery_target")
        if "target_object" in pending_fields:
            block_with_reason("target_object", "The target object is still unresolved enough to prevent stable graph selection.", "target_object")
        if "source_locator" in pending_fields:
            block_with_reason("source_locator", "The source locator is still unresolved, so the graph start point cannot be chosen honestly.", "source_locator")
        if "source_modality" in pending_fields:
            block_with_reason("source_modality", "Source modality is still unresolved and would change the upstream graph start.", "source_modality")
        if "delivery_target" in pending_fields:
            block_with_reason("delivery_target", "The delivery target is still unresolved and blocks the final delivery path.", "delivery_target")
        if "deliverable_form" in pending_fields:
            block_with_reason("deliverable_form", "The final deliverable form is still unresolved and changes render/delivery structure.", "deliverable_form")
        if "analysis_frame" in pending_fields:
            block_with_reason("analysis_frame", "The report/analysis frame is still unresolved and changes the graph skeleton.", "analysis_frame")
        if "evidence_basis" in pending_fields:
            block_with_reason("evidence_basis", "The evidence basis is still unresolved and changes upstream evidence-gathering nodes.", "evidence_basis")
        if "workflow_shape" in pending_fields:
            block_with_reason("workflow_shape", "Workflow shape is still unresolved and changes the allowed graph container.", "workflow_shape")
        if "evidence_requirements" in pending_fields and semantic.deliverable_contract != "generic_output":
            block_with_reason("evidence_basis", "The required evidence set is still unresolved for the currently dominant semantic lane.", "evidence_requirements")
        if semantic.target_object == "generic_request" and workflow_requires_explicit_source and (
            not normalized.input_description
            or bool(graph_specific_evidence_basis)
            or semantic.analysis_frame in {"architecture", "audit", "assessment"}
        ):
            block_with_reason("target_object", "The target object is still too generic for the requested analysis/evidence shape.", "target_object")
        if (
            semantic.deliverable_form == "generic_output"
            and semantic.primary_objective in {"analyze", "synthesize", "validate"}
            and semantic.target_object == "generic_request"
            and (
                not normalized.output_description
                or family == BlueprintType.WORKFLOW
            )
        ):
            block_with_reason("deliverable_form", "The request implies analysis output but does not yet resolve what final deliverable form should be produced.", "deliverable_form")
        if (
            not normalized.output_description
            and semantic.deliverable_form == "generic_output"
            and (
                semantic.primary_objective in {"analyze", "synthesize", "validate"}
                or semantic.target_object == "generic_request"
            )
        ):
            block_with_reason("deliverable_form", "No concrete output target or deliverable form is available yet.", "deliverable_form")
        if deliverable_form_blocks:
            block_with_reason("deliverable_form", "The current semantic state still leaves the final deliverable form too generic for graph closure.", "deliverable_form")

        if semantic.target_object == "repository":
            if source_plan.get("source_modality") == "unsupported_source":
                review_reasons.append(
                    "Repository source type is unsupported for automatic checkout, so the graph should be proposed with operator review."
                )
            elif semantic.source_modality == "unknown":
                block_with_reason("source_modality", "Repository source modality is still unresolved between local inventory and remote acquisition paths.", "repository_source_modality")
            if semantic.deliverable_form == "report" and semantic.analysis_frame == "generic":
                block_with_reason("analysis_frame", "Repository reports still branch into materially different report skeletons until the analysis frame is explicit.", "analysis_frame")
            if not semantic.evidence_basis:
                block_with_reason("evidence_basis", "Repository report planning still needs an evidence basis such as structure, relationships, or execution surface.", "evidence_basis")
        elif semantic.target_object == "web_resource":
            if not any(item in {"web_sources", "local_documents"} for item in semantic.evidence_basis):
                block_with_reason("evidence_basis", "Web/resource flows still need a dominant evidence basis to choose the upstream source path.", "evidence_basis")
            if semantic.source_modality == "unknown":
                block_with_reason("source_modality", "Web/resource source modality is still unresolved.", "source_modality")
        elif semantic.target_object == "local_workspace":
            if semantic.source_modality == "unknown":
                block_with_reason("source_modality", "Local workspace source modality is still unresolved.", "source_modality")
            if (
                not semantic.evidence_basis
                and (
                    semantic.deliverable_form in {"report", "briefing", "artifact_bundle"}
                    or semantic.analysis_frame != "generic"
                    or semantic.workflow_shape != "single_service"
                )
            ):
                block_with_reason("evidence_basis", "Local workspace flows still need a dominant evidence basis to choose the right graph lane.", "evidence_basis")
        elif semantic.target_object == "dataset":
            if semantic.source_modality == "unknown":
                block_with_reason("source_modality", "Dataset source modality is still unresolved.", "source_modality")
            if "structured_data" not in set(semantic.evidence_basis or []):
                block_with_reason("evidence_basis", "Dataset flows need structured-data evidence to close the graph honestly.", "evidence_basis")
            if semantic.deliverable_form == "report" and semantic.analysis_frame == "generic":
                block_with_reason("analysis_frame", "Dataset reports still branch into different summary/assessment/audit skeletons until the frame is explicit.", "analysis_frame")

        if "monitoring_scope" in analysis.gaps:
            block_with_reason("workflow_shape", "Monitoring scope would change the workflow container and recurrence structure.", "monitoring_scope")

        for ambiguity in semantic.ambiguities:
            if ambiguity in {"repository_source_modality", "source_modality"}:
                if semantic.target_object == "repository" and source_plan.get("source_modality") == "unsupported_source":
                    continue
                block_with_reason("source_modality", "Source modality remains ambiguous enough to change the upstream graph path.", ambiguity)
            elif ambiguity == "source_locator" and (
                semantic.target_object != "generic_request" or family != BlueprintType.SINGLE_SERVICE
            ):
                block_with_reason("source_locator", "The source locator remains ambiguous enough to block graph start selection.", ambiguity)
            elif ambiguity == "delivery_target" and (
                semantic.deliverable_form != "generic_output"
                or family != BlueprintType.SINGLE_SERVICE
                or semantic.analysis_frame != "generic"
            ):
                block_with_reason("delivery_target", "The delivery target remains ambiguous enough to block delivery path selection.", ambiguity)
            elif ambiguity == "analysis_frame" and (
                family == BlueprintType.WORKFLOW or semantic.target_object == "repository"
            ):
                block_with_reason("analysis_frame", "The analysis frame remains ambiguous enough to branch the graph skeleton.", ambiguity)
            elif ambiguity == "deliverable_form" and deliverable_form_blocks:
                block_with_reason("deliverable_form", "The deliverable form remains ambiguous enough to branch render/delivery structure.", ambiguity)
            elif ambiguity == "evidence_basis":
                block_with_reason("evidence_basis", "The evidence basis remains ambiguous enough to branch upstream evidence nodes.", ambiguity)

        if semantic_judgement is not None and semantic_judgement.used:
            for axis in semantic_judgement.clarification_axes:
                if axis == "analysis_frame" and axis not in blocking_axes:
                    block_with_reason(axis, "Validated LLM semantic judgement still found graph-critical analysis-frame ambiguity.")
                elif axis == "decision_posture" and axis not in blocking_axes:
                    block_with_reason(axis, "Validated LLM semantic judgement still found graph-critical decision-posture ambiguity.")
                elif axis == "deliverable_form" and axis not in blocking_axes:
                    block_with_reason(axis, "Validated LLM semantic judgement still found graph-critical deliverable-form ambiguity.")
                elif axis == "source_modality" and axis not in blocking_axes:
                    block_with_reason(axis, "Validated LLM semantic judgement still found graph-critical source-modality ambiguity.")
                elif axis == "evidence_basis" and axis not in blocking_axes:
                    block_with_reason(axis, "Validated LLM semantic judgement still found graph-critical evidence-basis ambiguity.")

        divergence_axes, divergence_reasons = self._divergence_blocking_axes(
            semantic=semantic,
            candidate_skeleton_ids=candidate_skeleton_ids,
            family=family,
        )
        for axis in divergence_axes:
            block_with_reason(axis, divergence_reasons.get(axis) or "Multiple plausible graph skeletons remain unresolved for this semantic axis.")

        if not blocking_axes:
            if not analysis.normalized.acceptance_criteria.items:
                review_reasons.append("Acceptance or review criteria are still missing, so the graph should be treated as review-gated.")
            if semantic.confidence < 50 and semantic.ambiguities:
                review_reasons.append("Semantic confidence is still moderate, so the graph should be proposed with review.")
            non_blocking_ambiguities = [
                item for item in semantic.ambiguities if item not in unresolved_ambiguities
            ]
            if non_blocking_ambiguities:
                review_reasons.append(
                    "Some non-blocking semantic ambiguities remain: " + ", ".join(non_blocking_ambiguities) + "."
                )

        closure_status: str
        if blocking_axes:
            closure_status = "clarification_required"
        elif review_reasons:
            closure_status = "graph_proposable_with_review"
        else:
            closure_status = "graph_proposable"

        closure_confidence = max(0, min(100, semantic.confidence))
        if closure_status == "clarification_required":
            closure_confidence = min(closure_confidence, 45)
        elif closure_status == "graph_proposable_with_review":
            closure_confidence = min(closure_confidence, 75)

        if closure_status == "clarification_required":
            reasoning = (
                "Graph closure is blocked until the semantic axes "
                + ", ".join(blocking_axes)
                + " are clarified."
            )
            if candidate_skeleton_ids:
                reasoning += " Plausible skeleton candidates currently include: " + ", ".join(candidate_skeleton_ids) + "."
        elif closure_status == "graph_proposable_with_review":
            reasoning = (
                "A bounded graph can be proposed now, but review is still needed because "
                + " ".join(review_reasons)
            )
        else:
            reasoning = "Semantic axes are sufficiently closed to propose a bounded graph now."
        if recommendation.workflow_shape_override_reason:
            reasoning = (
                reasoning.rstrip(".")
                + f" Family selection was explicitly upgraded because {recommendation.workflow_shape_override_reason}."
            )

        dominant_skeleton_id = None
        divergent_skeleton_ids = []
        if semantic_judgement is not None and semantic_judgement.used and semantic_judgement.dominant_skeleton_id:
            dominant_skeleton_id = semantic_judgement.dominant_skeleton_id
        elif semantic.deliverable_contract == "architecture_report":
            dominant_skeleton_id = "repository_architecture_report"
        elif semantic.deliverable_contract == "research_briefing":
            dominant_skeleton_id = "research_briefing"
        elif semantic.deliverable_contract in {
            "dataset_summary",
            "dataset_summary_report",
            "dataset_validation_report",
            "dataset_comparison_report",
        }:
            specific_data_skeletons = [
                item
                for item in candidate_skeleton_ids
                if item in {
                    "dataset_summary_report",
                    "dataset_validation_report",
                    "dataset_comparison_report",
                    "dataset_transform_delivery",
                }
            ]
            if len(specific_data_skeletons) == 1:
                dominant_skeleton_id = specific_data_skeletons[0]
            elif semantic.deliverable_contract in {
                "dataset_summary_report",
                "dataset_validation_report",
                "dataset_comparison_report",
            }:
                dominant_skeleton_id = semantic.deliverable_contract
            elif "dataset_validation_report" in candidate_skeleton_ids and len(candidate_skeleton_ids) == 1:
                dominant_skeleton_id = "dataset_validation_report"
            elif "dataset_comparison_report" in candidate_skeleton_ids and len(candidate_skeleton_ids) == 1:
                dominant_skeleton_id = "dataset_comparison_report"
            elif "dataset_summary_report" in candidate_skeleton_ids and len(candidate_skeleton_ids) == 1:
                dominant_skeleton_id = "dataset_summary_report"
            else:
                dominant_skeleton_id = "dataset_summary"
        elif semantic.deliverable_contract == "artifact_bundle":
            dominant_skeleton_id = "artifact_bundle_delivery"
        elif len(candidate_skeleton_ids) == 1:
            dominant_skeleton_id = candidate_skeleton_ids[0]
        elif len(candidate_skeleton_ids) > 1:
            divergent_skeleton_ids = list(candidate_skeleton_ids)

        return GraphClosureAssessment(
            closure_status=closure_status,
            blocking_semantic_axes=blocking_axes,
            unresolved_ambiguities=unresolved_ambiguities,
            review_reasons=review_reasons,
            closure_confidence=closure_confidence,
            candidate_graph_family=recommendation.selected_family,
            candidate_skeleton_ids=candidate_skeleton_ids,
            dominant_skeleton_id=dominant_skeleton_id,
            divergent_skeleton_ids=divergent_skeleton_ids,
            blocking_axis_reasons=blocking_axis_reasons,
            candidate_graph_reasoning_summary=reasoning,
            family_override_reason=recommendation.workflow_shape_override_reason,
            semantic_judgement_used=bool(semantic_judgement is not None and semantic_judgement.used),
            semantic_judgement_source=(semantic_judgement.source if semantic_judgement is not None else "not_used"),
            semantic_judgement_fallback_used=bool(semantic_judgement.fallback_used) if semantic_judgement is not None else False,
            candidate_skeleton_vocabulary=candidate_skeleton_vocabulary,
            semantic_judgement_repair_notes=list(semantic_judgement.validator_repair_notes) if semantic_judgement is not None else [],
            semantic_judgement_reasoning_summary=(
                semantic_judgement.reasoning_summary if semantic_judgement is not None and semantic_judgement.reasoning_summary else None
            ),
        )


class DeterministicSkeletonFiller:
    mode = RequirementCompilerMode.DETERMINISTIC

    def __init__(self, registry: CapabilityRegistryService | None = None) -> None:
        self.registry = registry or CapabilityRegistryService()

    def build(
        self,
        requirement: Requirement,
        analysis: RequirementAnalysis,
        blueprint_type: BlueprintType,
        template_selection: TemplateSelectionResult | None = None,
    ) -> SkeletonDraft:
        normalized = analysis.normalized
        metadata = normalized.metadata
        base_payload: dict[str, Any] = BlueprintCreateRequest(
            project_id=requirement.project_id,
            requirement_id=requirement.id,
            blueprint_type=blueprint_type,
            name=f"{normalized.title} Draft",
            description=normalized.summary or normalized.goal or f"Blueprint draft compiled from requirement {normalized.title}.",
            status=BlueprintStatus.DRAFT,
            input_schema_json=self._draft_schema("input", normalized.input_description),
            output_schema_json=self._draft_schema("output", normalized.output_description),
            configurable_vars_json={
                "variables": [
                    {"name": "domain", "default": metadata.domain or "general"},
                    {"name": "human_review_required", "default": metadata.human_review_required},
                ]
            },
            execution_env_json={
                "env_required": metadata.external_tools_needed,
                "human_review_required": metadata.human_review_required,
                "recurrence_mode": metadata.recurrence_mode,
            },
            dependencies_json={"external_tools": metadata.external_tools_needed},
            artifacts_spec_json=self._artifacts_spec_for_type(blueprint_type),
            observability_spec_json={
                "step_logs": True,
                "token_usage": True,
                "run_summary_artifact": True,
            },
            retry_policy_json={"strategy": "simple_retry", "max_attempts": 2},
            runtime_constraints_json={"human_review_required": metadata.human_review_required},
            template_tags_json={
                "source": "requirement_compiler",
                "family": blueprint_type.value,
                "domain": metadata.domain or "general",
                "compiler_mode": self.mode.value,
            },
        ).model_dump(mode="python")

        normalized_trace = normalized.trace_summary()
        base_payload.setdefault("runtime_constraints_json", {})["normalized_requirement_trace"] = dict(normalized_trace)
        base_payload.setdefault("runtime_constraints_json", {})["semantic_interpretation"] = (
            analysis.semantic_interpretation.trace_summary()
        )
        intent_candidates = self.registry.infer_deliverable_intent_candidates(
            normalized_requirement=normalized_trace,
            semantic_interpretation=analysis.semantic_interpretation.trace_summary(),
        )
        typed_workflow_intent = (
            analysis.semantic_judgement.dominant_skeleton_id
            if analysis.semantic_judgement is not None
            and analysis.semantic_judgement.used
            and analysis.semantic_judgement.dominant_skeleton_id
            and analysis.semantic_judgement.dominant_skeleton_id in {
                "dataset_summary_report",
                "dataset_validation_report",
                "dataset_comparison_report",
            }
            else (intent_candidates[0].intent_id if intent_candidates else None)
        )
        typed_workflow_proposal = (
            self.registry.build_typed_workflow_proposal(
                typed_workflow_intent,
                context="blueprint_planning",
                normalized_requirement=normalized_trace,
                semantic_interpretation=analysis.semantic_interpretation.trace_summary(),
            )
            if blueprint_type == BlueprintType.WORKFLOW and typed_workflow_intent is not None
            else None
        )

        factory_key = f"deterministic.{blueprint_type.value}.v1"
        if (
            typed_workflow_proposal is not None
            and typed_workflow_proposal.steps
            and typed_workflow_proposal.validation.valid
        ):
            typed_workflow_locked = self._should_lock_typed_workflow(typed_workflow_proposal)
            repository_source_plan = self.registry.infer_repository_source_plan(normalized_trace)
            edges_by_target: dict[str, list[dict[str, Any]]] = {}
            for edge in typed_workflow_proposal.edges:
                edges_by_target.setdefault(edge.to_step_id, []).append(edge.model_dump(mode="python"))
            base_payload["execution_steps_json"] = {
                "steps": [
                    self._step_payload_from_typed_workflow_step(
                        step,
                        intent_id=typed_workflow_proposal.intent_id,
                        default_args=dict(step.default_args),
                        planning_dependencies=edges_by_target.get(step.step_id, []),
                    )
                    for step in typed_workflow_proposal.steps
                ]
            }
            base_payload["execution_nodes_json"] = {
                "nodes": self._node_payloads_from_typed_workflow_steps(typed_workflow_proposal.steps),
                "workflow_pattern": " -> ".join(step.logical_step_kind for step in typed_workflow_proposal.steps),
            }
            artifacts_spec = base_payload.setdefault("artifacts_spec_json", {})
            artifacts_spec["default_outputs"] = self._merge_default_outputs(
                existing=list(artifacts_spec.get("default_outputs", [])),
                proposed=list(typed_workflow_proposal.default_output_artifacts),
            )
            runtime_constraints = base_payload.setdefault("runtime_constraints_json", {})
            runtime_constraints["typed_workflow_proposal"] = typed_workflow_proposal.model_dump(mode="python")
            runtime_constraints["typed_workflow_locked"] = typed_workflow_locked
            runtime_constraints["typed_workflow_intent_candidates"] = [
                item.model_dump(mode="python") for item in intent_candidates
            ]
            runtime_constraints["planning_graph"] = {
                "intent_id": typed_workflow_proposal.intent_id,
                "nodes": [item.model_dump(mode="python") for item in typed_workflow_proposal.nodes],
                "edges": [item.model_dump(mode="python") for item in typed_workflow_proposal.edges],
                "validation": typed_workflow_proposal.validation.model_dump(mode="python"),
            }
            runtime_constraints["repository_source_plan"] = dict(repository_source_plan)
            template_tags = base_payload.setdefault("template_tags_json", {})
            template_tags["typed_workflow_intent_id"] = typed_workflow_proposal.intent_id
            template_tags["typed_workflow_applied"] = True
            template_tags["typed_workflow_locked"] = typed_workflow_locked
            template_tags["repository_source_modality"] = repository_source_plan.get("source_modality")
            step_plan_summary = StepPlanSummary(
                used=False,
                llm_status="no_changes",
                confidence=typed_workflow_proposal.confidence,
                proposed_steps=[
                    LLMStepPlanStep(
                        step_id=step.step_id,
                        order=step.order,
                        logical_step_kind=step.logical_step_kind,
                        semantic_role=step.semantic_role,
                        purpose=step.purpose,
                        input_intent=step.input_intent,
                        output_intent=step.output_intent,
                        required_capabilities=[step.capability_id],
                        artifact_expectations=list(step.artifact_expectations),
                        human_review_required=step.human_review_required,
                        selected_reusable_capability_id=step.selected_reusable_capability_id,
                        consumes_evidence_types=list(step.consumes_evidence_types),
                        produces_evidence_types=list(step.produces_evidence_types),
                        deliverable_intents=[typed_workflow_proposal.intent_id],
                        confidence=typed_workflow_proposal.confidence,
                    )
                    for step in typed_workflow_proposal.steps
                ],
                accepted_step_ids=[step.step_id for step in typed_workflow_proposal.steps],
                referenced_capabilities=[step.capability_id for step in typed_workflow_proposal.steps],
                warnings=[
                    f"Typed workflow proposal '{typed_workflow_proposal.intent_id}' expanded the deterministic workflow skeleton."
                ],
                inferred_deliverable_intent=typed_workflow_proposal.intent_id,
                deliverable_intent_candidates=[
                    DeliverableIntentCandidate(**item.model_dump(mode="python")) for item in intent_candidates
                ],
                planning_graph_nodes=[item.model_dump(mode="python") for item in typed_workflow_proposal.nodes],
                planning_graph_edges=[item.model_dump(mode="python") for item in typed_workflow_proposal.edges],
                planning_graph_validation=typed_workflow_proposal.validation.model_dump(mode="python"),
                typed_workflow_applied=True,
                typed_workflow_locked=typed_workflow_locked,
                typed_workflow_respected=typed_workflow_locked,
            )
        elif blueprint_type == BlueprintType.SINGLE_SERVICE:
            base_payload["execution_steps_json"] = {
                "steps": [
                    {"step_name": "ingest_request", "step_type": "python_function", "function_name": "ingest_request"},
                    {"step_name": "process_request", "step_type": "llm_task", "task_kind": "single_service_core"},
                    {"step_name": "emit_response", "step_type": "artifact_writer", "writer_kind": "single_service_output"},
                ]
            }
            base_payload["execution_nodes_json"] = {"nodes": [{"name": "service_core", "role": "service_handler"}]}
        elif blueprint_type == BlueprintType.WORKFLOW:
            base_payload["execution_steps_json"] = {
                "steps": [
                    {"step_name": "collect_inputs", "step_type": "python_function", "function_name": "collect_sources"},
                    {"step_name": "filter_and_process", "step_type": "llm_task", "task_kind": "workflow_processor"},
                    {"step_name": "publish_outputs", "step_type": "artifact_writer", "writer_kind": "workflow_publish"},
                ]
            }
            base_payload["execution_nodes_json"] = {
                "nodes": [
                    {"name": "collector", "role": "input_stage"},
                    {"name": "processor", "role": "transformation_stage"},
                    {"name": "publisher", "role": "output_stage"},
                ]
            }
        else:
            base_payload["execution_steps_json"] = {
                "steps": [
                    {"step_name": "planner_agent", "step_type": "llm_task", "task_kind": "plan_work"},
                    {"step_name": "specialist_agent", "step_type": "llm_task", "task_kind": "execute_specialized_work"},
                    {"step_name": "review_agent", "step_type": "artifact_writer", "writer_kind": "multi_agent_review"},
                ]
            }
            base_payload["execution_nodes_json"] = {
                "nodes": [
                    {"name": "planner", "role": "planner"},
                    {"name": "specialist", "role": "specialist"},
                    {"name": "reviewer", "role": "reviewer"},
                ],
                "collaboration_mode": "sequential_placeholder",
            }

        self._apply_template_overlay(base_payload, template_selection)
        return SkeletonDraft(
            payload=base_payload,
            factory_key=factory_key,
            filler_mode=self.mode,
            step_plan_summary=locals().get("step_plan_summary", StepPlanSummary(
                deliverable_intent_candidates=[
                    DeliverableIntentCandidate(**item.model_dump(mode="python")) for item in intent_candidates
                ],
                inferred_deliverable_intent=typed_workflow_intent,
            )),
        )

    def _should_lock_typed_workflow(self, proposal: CapabilityTypedWorkflowProposalResponse) -> bool:
        if proposal.confidence < 80 or not proposal.steps or not proposal.validation.valid:
            return False
        for step in proposal.steps:
            validation = self.registry.validate_capability_usage(
                capability_id=step.capability_id,
                context="blueprint_planning",
                step_kind=step.logical_step_kind,
            )
            if not validation.valid:
                return False
        return True

    def _step_payload_from_typed_workflow_step(
        self,
        step,  # noqa: ANN001
        *,
        intent_id: str,
        default_args: dict[str, Any],
        planning_dependencies: list[dict[str, Any]],
    ) -> dict[str, Any]:
        capability = self.registry.get_capability(step.capability_id)
        execution_kind = capability.execution_kind if capability is not None else None
        payload: dict[str, Any] = {
            "step_name": step.step_id,
            "node_name": step.node_name,
            "logical_step_kind": step.logical_step_kind,
            "semantic_role": step.semantic_role,
            "purpose": step.purpose,
            "input_intent": step.input_intent,
            "output_intent": step.output_intent,
            "artifact_expectations": list(step.artifact_expectations),
            "required_capabilities": [step.capability_id],
            "selected_capability_id": step.capability_id,
            "selected_reusable_capability_id": step.selected_reusable_capability_id,
            "recommended_reusable_capability_ids": list(step.recommended_reusable_capability_ids),
            "consumes_evidence_types": list(step.consumes_evidence_types),
            "produces_evidence_types": list(step.produces_evidence_types),
            "deliverable_intents": [intent_id],
            "planning_dependencies": list(planning_dependencies),
        }
        if step.human_review_required:
            payload["human_review_required"] = True
        if execution_kind == "python_function":
            payload["step_type"] = "python_function"
            payload["function_name"] = step.capability_id
            payload["args"] = dict(default_args)
        elif execution_kind == "artifact_writer":
            payload["step_type"] = "artifact_writer"
            payload["writer_kind"] = step.capability_id
            payload["args"] = dict(default_args)
        elif execution_kind == "llm_task":
            payload["step_type"] = "llm_task"
            payload["task_kind"] = step.capability_id
            payload["args"] = dict(default_args)
            if payload["args"].get("task_prompt"):
                payload["task_prompt"] = payload["args"]["task_prompt"]
            if payload["args"].get("expected_output"):
                payload["expected_output"] = payload["args"]["expected_output"]
        else:
            payload["step_type"] = execution_kind or "llm_task"
            payload["args"] = dict(default_args)
        return payload

    def _node_payloads_from_typed_workflow_steps(self, steps: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                "name": step.node_name,
                "role": step.semantic_role or step.logical_step_kind,
                "capability_id": step.capability_id,
            }
            for step in steps
        ]

    def _merge_default_outputs(self, *, existing: list[str], proposed: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in [*existing, *proposed]:
            normalized = str(item).strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                ordered.append(normalized)
        return ordered

    def _apply_template_overlay(
        self,
        payload: dict[str, Any],
        template_selection: TemplateSelectionResult | None,
    ) -> None:
        if template_selection is None or template_selection.selected_template_id is None:
            return

        slot_hints = template_selection.slot_hints
        if slot_hints.blueprint_description:
            payload["description"] = slot_hints.blueprint_description

        steps = (payload.get("execution_steps_json") or {}).get("steps", [])
        steps_by_name = {
            str(step.get("step_name")): step
            for step in steps
            if isinstance(step, dict) and step.get("step_name")
        }
        for step_name, hint in slot_hints.step_hints.items():
            step = steps_by_name.get(step_name)
            if step is None:
                continue
            if hint.description:
                step["description"] = hint.description
            if hint.purpose:
                step["purpose"] = hint.purpose

        artifacts_spec = payload.setdefault("artifacts_spec_json", {})
        allowed_artifacts = {
            item
            for item in artifacts_spec.get("default_outputs", [])
            if isinstance(item, str)
        }
        artifact_details = artifacts_spec.setdefault("artifact_details", {})
        for artifact_name, hint in slot_hints.artifact_hints.items():
            if artifact_name not in allowed_artifacts:
                continue
            artifact_details[artifact_name] = {
                "intent": hint.intent,
                "description": hint.description,
            }

        variables = (payload.get("configurable_vars_json") or {}).get("variables", [])
        variables_by_name = {
            str(variable.get("name")): variable
            for variable in variables
            if isinstance(variable, dict) and variable.get("name")
        }
        for variable_name, hint in slot_hints.variable_hints.items():
            variable = variables_by_name.get(variable_name)
            if variable is None:
                continue
            if hint.hint:
                variable["hint"] = hint.hint
            if hint.default is not None:
                variable["default"] = hint.default

        template_tags = payload.setdefault("template_tags_json", {})
        template_tags.update(slot_hints.template_tags)
        template_tags["template_id"] = template_selection.selected_template_id
        template_tags["template_name"] = template_selection.selected_template_name
        template_tags["template_family"] = template_selection.selected_family.value
        template_tags["template_source"] = "registry"

    def _draft_schema(self, label: str, description: str) -> dict[str, Any]:
        return {
            "type": "object",
            "title": f"{label.title()} Payload",
            "description": description or f"Draft {label} schema generated from the requirement compiler.",
            "properties": {},
        }

    def _artifacts_spec_for_type(self, blueprint_type: BlueprintType) -> dict[str, Any]:
        default_outputs = {
            BlueprintType.SINGLE_SERVICE: ["run_summary", "service_output.json"],
            BlueprintType.WORKFLOW: ["run_summary", "workflow_report.json"],
            BlueprintType.MULTI_AGENT: ["run_summary", "agent_review.json"],
        }
        return {"default_outputs": default_outputs[blueprint_type]}
