from __future__ import annotations

from app.core.enums import BlueprintType
from app.services.requirement.compiler.contracts import (
    NormalizedRequirement,
    TemplateArtifactHint,
    TemplateDefinition,
    TemplateMatchCandidate,
    TemplateMatchRules,
    TemplateSelectionResult,
    TemplateSlotHints,
    TemplateStepHint,
    TemplateVariableHint,
)


STATIC_TEMPLATE_REGISTRY: tuple[TemplateDefinition, ...] = (
    TemplateDefinition(
        template_id="single_service.request_response",
        name="Request Response Service",
        family=BlueprintType.SINGLE_SERVICE,
        description="Bounded single-request processing service.",
        tags=["service", "request-response", "api"],
        match_rules=TemplateMatchRules(
            keywords_any=["request", "response", "reply", "service", "support"],
        ),
        slot_hints=TemplateSlotHints(
            blueprint_description="Single bounded request-processing service with deterministic ingress and response emission.",
            step_hints={
                "ingest_request": TemplateStepHint(
                    description="Read the incoming request payload and normalize it for processing.",
                    purpose="Prepare a consistent request envelope for the service core.",
                ),
                "process_request": TemplateStepHint(
                    description="Apply the core service logic to the normalized request.",
                    purpose="Produce the main response payload for the caller.",
                ),
                "emit_response": TemplateStepHint(
                    description="Write the final response artifact for downstream consumption.",
                    purpose="Publish a stable service response output.",
                ),
            },
            variable_hints={"domain": TemplateVariableHint(hint="service_domain")},
            template_tags={"template_category": "request_response"},
        ),
    ),
    TemplateDefinition(
        template_id="workflow.monitoring_digest",
        name="Monitoring Digest Workflow",
        family=BlueprintType.WORKFLOW,
        description="Recurring monitoring and digest publication workflow.",
        tags=["workflow", "monitoring", "digest", "recurring"],
        match_rules=TemplateMatchRules(
            recurrence_modes=["daily", "weekly", "scheduled", "recurring"],
            required_tools_any=["rss", "search", "web_search"],
            keywords_any=["digest", "monitor", "news", "tracking", "briefing"],
        ),
        slot_hints=TemplateSlotHints(
            blueprint_description="Recurring workflow that collects monitored inputs, synthesizes changes, and publishes a digest package.",
            step_hints={
                "collect_inputs": TemplateStepHint(
                    description="Collect the latest monitored inputs and normalize them into one working bundle.",
                    purpose="Prepare current-source material for downstream digest synthesis.",
                ),
                "filter_and_process": TemplateStepHint(
                    description="Filter the monitored inputs and synthesize the digest-ready findings.",
                    purpose="Transform raw monitored inputs into concise digest content.",
                ),
                "publish_outputs": TemplateStepHint(
                    description="Publish the digest artifacts for readers and downstream systems.",
                    purpose="Emit a stable digest package on each scheduled run.",
                ),
            },
            variable_hints={"domain": TemplateVariableHint(hint="monitoring")},
            template_tags={"template_category": "monitoring_digest"},
        ),
    ),
    TemplateDefinition(
        template_id="workflow.data_pipeline",
        name="Data Processing Workflow",
        family=BlueprintType.WORKFLOW,
        description="Staged extraction, transformation, and publish workflow.",
        tags=["workflow", "etl", "data", "processing"],
        match_rules=TemplateMatchRules(
            keywords_any=["etl", "extract", "transform", "load", "sync", "pipeline", "data"],
            required_tools_any=["database", "storage", "filesystem"],
        ),
        slot_hints=TemplateSlotHints(
            blueprint_description="Workflow that stages data collection, transformation, and publication in a deterministic pipeline.",
            step_hints={
                "collect_inputs": TemplateStepHint(
                    description="Extract the source data inputs and normalize them into one processing bundle.",
                    purpose="Stage raw data for downstream transformation.",
                ),
                "filter_and_process": TemplateStepHint(
                    description="Transform the normalized inputs into the target processed dataset.",
                    purpose="Apply the core data-processing logic in a bounded stage.",
                ),
                "publish_outputs": TemplateStepHint(
                    description="Publish the transformed outputs for downstream consumers.",
                    purpose="Write deterministic processed-data artifacts.",
                ),
            },
            variable_hints={"domain": TemplateVariableHint(hint="data_processing")},
            template_tags={"template_category": "data_pipeline"},
        ),
    ),
    TemplateDefinition(
        template_id="workflow.research_briefing",
        name="Research Briefing Workflow",
        family=BlueprintType.WORKFLOW,
        description="Collect, synthesize, and publish a research briefing package.",
        tags=["workflow", "research", "analysis", "briefing"],
        match_rules=TemplateMatchRules(
            domains=["research", "analysis"],
            keywords_any=["research", "brief", "briefing", "report", "analysis"],
        ),
        slot_hints=TemplateSlotHints(
            blueprint_description="Workflow that gathers research inputs, synthesizes findings, and publishes a briefing package.",
            step_hints={
                "collect_inputs": TemplateStepHint(
                    description="Collect source material and normalize it into a briefing-ready research bundle.",
                    purpose="Prepare relevant research inputs for analysis.",
                ),
                "filter_and_process": TemplateStepHint(
                    description="Filter the collected material and synthesize the key findings.",
                    purpose="Produce the core analytical content for the briefing.",
                ),
                "publish_outputs": TemplateStepHint(
                    description="Publish the final briefing artifacts for readers and downstream workflows.",
                    purpose="Emit the structured and human-readable briefing outputs.",
                ),
            },
            artifact_hints={
                "workflow_report.json": TemplateArtifactHint(
                    intent="Structured research briefing output",
                    description="Machine-readable research briefing payload.",
                )
            },
            variable_hints={"domain": TemplateVariableHint(hint="research")},
            template_tags={"template_category": "research_briefing"},
        ),
    ),
    TemplateDefinition(
        template_id="multi_agent.review_escalation",
        name="Review And Escalation Multi-Agent Flow",
        family=BlueprintType.MULTI_AGENT,
        description="Planner, specialist, and reviewer collaboration flow with escalation-aware review.",
        tags=["multi_agent", "review", "escalation", "collaboration"],
        match_rules=TemplateMatchRules(
            human_review_required=True,
            keywords_any=["review", "approve", "escalate", "specialist", "collaborative"],
        ),
        slot_hints=TemplateSlotHints(
            blueprint_description="Multi-agent review flow that coordinates planning, specialist execution, and final review.",
            step_hints={
                "planner_agent": TemplateStepHint(
                    description="Plan the work breakdown and assign the specialist focus.",
                    purpose="Create a clear coordinated plan for downstream agent execution.",
                ),
                "specialist_agent": TemplateStepHint(
                    description="Execute the specialist analysis or task work against the planned scope.",
                    purpose="Produce the substantive specialist output.",
                ),
                "review_agent": TemplateStepHint(
                    description="Review the specialist output and determine whether escalation or revision is needed.",
                    purpose="Add explicit review and escalation control before publication.",
                ),
            },
            variable_hints={"domain": TemplateVariableHint(hint="review_and_escalation")},
            template_tags={"template_category": "review_escalation"},
        ),
    ),
)


class DeterministicTemplateMatcher:
    selection_mode = "static_registry.v3_0"

    def select(
        self,
        *,
        normalized: NormalizedRequirement,
        selected_family: BlueprintType,
    ) -> TemplateSelectionResult:
        family_templates = [template for template in STATIC_TEMPLATE_REGISTRY if template.family == selected_family]
        candidates: list[tuple[TemplateDefinition, TemplateMatchCandidate]] = []
        for template in family_templates:
            candidate = self._score_template(template=template, normalized=normalized)
            if candidate.score > 0:
                candidates.append((template, candidate))

        candidates.sort(
            key=lambda item: (
                item[1].score,
                item[1].matched_rule_count,
                item[0].template_id,
            ),
            reverse=True,
        )

        if not candidates:
            return TemplateSelectionResult(
                selected_family=selected_family,
                selection_mode=self.selection_mode,
                matched_candidates=[],
                match_reasons=[],
            )

        selected_template, selected_candidate = candidates[0]
        return TemplateSelectionResult(
            selected_template_id=selected_template.template_id,
            selected_template_name=selected_template.name,
            selected_family=selected_family,
            selection_mode=self.selection_mode,
            confidence=selected_candidate.score,
            matched_candidates=[item[1] for item in candidates],
            match_reasons=list(selected_candidate.reasons),
            template_tags=list(selected_template.tags),
            slot_hints=selected_template.slot_hints.model_copy(deep=True),
        )

    def _score_template(
        self,
        *,
        template: TemplateDefinition,
        normalized: NormalizedRequirement,
    ) -> TemplateMatchCandidate:
        rules = template.match_rules
        text_parts = [
            normalized.title,
            normalized.summary,
            normalized.goal,
            normalized.input_description,
            normalized.output_description,
            normalized.target_users,
            normalized.metadata.domain or "",
        ]
        haystack = " ".join(part for part in text_parts if part).lower()
        domain = (normalized.metadata.domain or "").strip().lower()
        recurrence_mode = (normalized.metadata.recurrence_mode or "").strip().lower()
        tools = {item.strip().lower() for item in normalized.metadata.external_tools_needed if item.strip()}

        score = 0
        reasons: list[str] = []

        if domain and domain in {item.strip().lower() for item in rules.domains}:
            score += 50
            reasons.append(f"domain matched {domain}")

        if recurrence_mode and recurrence_mode in {item.strip().lower() for item in rules.recurrence_modes}:
            score += 25
            reasons.append(f"recurrence matched {recurrence_mode}")

        matched_tools = sorted(
            tool for tool in tools if tool in {item.strip().lower() for item in rules.required_tools_any}
        )
        if matched_tools:
            score += 20
            reasons.append(f"required tools matched {', '.join(matched_tools)}")

        matched_keywords = sorted(
            keyword
            for keyword in {item.strip().lower() for item in rules.keywords_any}
            if keyword and keyword in haystack
        )
        if matched_keywords:
            score += min(20, 10 * len(matched_keywords))
            reasons.append(f"keywords matched {', '.join(matched_keywords[:3])}")

        if rules.human_review_required is not None and normalized.metadata.human_review_required == rules.human_review_required:
            score += 20
            reasons.append(f"human_review_required matched {rules.human_review_required}")

        return TemplateMatchCandidate(
            template_id=template.template_id,
            template_name=template.name,
            family=template.family,
            score=score,
            matched_rule_count=len(reasons),
            reasons=reasons,
            tags=list(template.tags),
        )
