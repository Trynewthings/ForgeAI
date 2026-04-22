from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy.orm import Session

from app.schemas.executable_manifest import (
    BuildRealizationCandidate,
    BuildRealizationFlexibilityExplanation,
    BuildRealizationRejectedCandidate,
    BuildRealizationSelectionResult,
)


REPOSITORY_SOURCE_URL_RE = re.compile(r"(?P<url>(?:https?|git|ssh|file|svn)://[^\s'\"<>]+|git@[^\s'\"<>]+)")
REPOSITORY_PATH_RE = re.compile(r"(?P<path>(?:\./|\.\./|/)[^\s'\"<>]+)")
REPOSITORY_ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")
from app.schemas.capability_registry import (
    CapabilityAlternativeResponse,
    CapabilityBindingDefinition,
    CapabilityContext,
    CapabilityDefinition,
    CapabilityDeliverableIntentCandidateResponse,
    CapabilityDeliverableIntentDefinition,
    CapabilityGap,
    CapabilityGapKind,
    CapabilityGapResponse,
    CapabilityGapSummaryResponse,
    CapabilityPlanningGraphEdgeResponse,
    CapabilityPlanningGraphNodeResponse,
    CapabilityPlanningGraphValidationIssueResponse,
    CapabilityPlanningGraphValidationResponse,
    CapabilityRegistrySnapshot,
    CapabilityRequestKind,
    CapabilityRequestSuggestionResponse,
    CapabilityReusableCandidateResponse,
    CapabilityTypedWorkflowProposalResponse,
    CapabilityTypedWorkflowStepResponse,
    CapabilityType,
    CapabilityUsageValidationResult,
    CapabilityIntentWorkflowStepTemplate,
    LogicalStepKindDefinition,
)
from app.schemas.semantic_core import SemanticCore
from app.schemas.workflow_native import PlannerCapabilityPortProjection, PlannerCapabilityProjection


LOGICAL_STEP_KINDS: list[LogicalStepKindDefinition] = [
    LogicalStepKindDefinition(
        step_kind="ingest",
        name="Ingest",
        description="Collect raw inputs from sources, feeds, or operator-provided locations.",
        typical_inputs=["source identifiers", "URLs", "feed configuration"],
        typical_outputs=["raw content", "normalized source items"],
        allowed_capability_types=["external_tool", "deterministic", "code_template"],
        tags=["input", "collection"],
    ),
    LogicalStepKindDefinition(
        step_kind="transform",
        name="Transform",
        description="Parse, normalize, filter, or reshape collected data into a usable intermediate form.",
        typical_inputs=["raw content", "structured source items"],
        typical_outputs=["normalized records", "filtered records"],
        allowed_capability_types=["deterministic", "code_template", "llm_assisted"],
        tags=["processing"],
    ),
    LogicalStepKindDefinition(
        step_kind="summarize",
        name="Summarize",
        description="Produce concise summaries or briefs from larger content sets.",
        typical_inputs=["documents", "aggregated records"],
        typical_outputs=["summary text", "briefing artifacts"],
        allowed_capability_types=["llm_assisted", "deterministic"],
        tags=["language", "synthesis"],
    ),
    LogicalStepKindDefinition(
        step_kind="rank",
        name="Rank",
        description="Score or order candidates according to explicit criteria.",
        typical_inputs=["candidate items", "ranking criteria"],
        typical_outputs=["ordered candidates", "scores"],
        allowed_capability_types=["deterministic", "llm_assisted"],
        tags=["selection"],
    ),
    LogicalStepKindDefinition(
        step_kind="validate",
        name="Validate",
        description="Check outputs or inputs against schemas, policies, or quality gates.",
        typical_inputs=["structured payloads", "schemas", "policy rules"],
        typical_outputs=["validation result", "error list"],
        allowed_capability_types=["deterministic", "human_gate"],
        tags=["quality", "guardrail"],
    ),
    LogicalStepKindDefinition(
        step_kind="notify",
        name="Notify",
        description="Deliver notifications or outbound messages to people or systems.",
        typical_inputs=["message payload", "recipient routing"],
        typical_outputs=["delivery attempt", "notification artifact"],
        allowed_capability_types=["notification", "external_tool"],
        tags=["delivery"],
    ),
    LogicalStepKindDefinition(
        step_kind="persist",
        name="Persist",
        description="Write outputs or execution records into durable artifacts or storage.",
        typical_inputs=["final payloads", "artifact metadata"],
        typical_outputs=["stored artifact reference", "storage confirmation"],
        allowed_capability_types=["deterministic", "code_template"],
        tags=["storage", "artifact"],
    ),
    LogicalStepKindDefinition(
        step_kind="review",
        name="Review",
        description="Pause for explicit human or policy review before proceeding.",
        typical_inputs=["draft outputs", "review criteria"],
        typical_outputs=["approval decision", "review notes"],
        allowed_capability_types=["human_gate", "llm_assisted"],
        tags=["human_in_loop"],
    ),
]


CAPABILITIES: list[CapabilityDefinition] = [
    CapabilityDefinition(
        capability_id="fetch_url",
        name="Fetch URL",
        description="Retrieve bounded remote or local page content from an explicit URL source.",
        supported_step_kinds=["ingest"],
        capability_type="external_tool",
        input_contract_summary="URL plus explicit network policy, optional domain constraints, and fetch mode.",
        output_contract_summary="Fetched page/text content plus source, content-type, and fetch metadata.",
        execution_kind="network_fetch",
        runtime_adapter="bounded_network_fetch",
        runtime_requirements=["network_access"],
        provider_requirements=["http_client"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        policy_summary="Remote fetch requires explicit allow_network and optionally bounded domain constraints; local file fetch remains workspace-scoped.",
        supported_actions=["fetch_text", "fetch_page"],
        policy_hints={"requires_allow_network": True, "supports_allowed_domains": True},
        semantic_purpose="Acquire bounded remote or local source material for downstream evidence collection.",
        semantic_role="evidence_source",
        produces_evidence_types=["research.sources"],
        deliverable_intents=["research_briefing"],
        source_modalities_supported=["http_url", "https_url", "file_url"],
        coverage_semantics="Fetches only explicit sources and returns bounded content rather than exploratory coverage.",
        confidence_semantics="High confidence for fetched payload fidelity; no claim beyond fetched sources.",
        tags=["http", "source"],
    ),
    CapabilityDefinition(
        capability_id="repository_source_resolve",
        name="Repository Source Resolve",
        description="Classify a repository locator as a local workspace repo, remote git source, archive source, or unsupported locator.",
        supported_step_kinds=["ingest"],
        capability_type="deterministic",
        input_contract_summary="Repository locator, optional repo root hints, and bounded workspace/source policy settings.",
        output_contract_summary="Structured repository source classification with source kind, normalized locator, checkout requirement, and workspace-root hints.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        runtime_requirements=["workspace_access"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        policy_summary="Repository source resolution stays explicit, bounded, and auditable; unsupported or ambiguous sources are reported honestly.",
        semantic_purpose="Resolve repository source modality before repository analysis or checkout planning begins.",
        semantic_role="evidence_source",
        produces_evidence_types=["repo.source"],
        deliverable_intents=["architecture_report", "repository_onboarding_brief", "repository_audit_summary"],
        preferred_downstream_capability_ids=["repository_checkout", "repository_inventory"],
        source_modalities_supported=["local_workspace_repo", "git_remote", "github_repo_url", "archive_url", "unsupported_source", "ambiguous_source"],
        coverage_semantics="Classifies only the explicit repository locator in scope.",
        confidence_semantics="High confidence when locator syntax is explicit; otherwise falls back to review-needed ambiguity.",
        tags=["repository", "source", "planning"],
    ),
    CapabilityDefinition(
        capability_id="repository_checkout",
        name="Repository Checkout",
        description="Materialize a supported repository source into a bounded workspace root for downstream repository analysis.",
        supported_step_kinds=["ingest"],
        capability_type="deterministic",
        input_contract_summary="Resolved repository source plus bounded checkout destination and optional shallow-clone/archive-extract policy hints.",
        output_contract_summary="Workspace root, checkout status, source provenance, and acquisition metadata.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        runtime_requirements=["workspace_access"],
        provider_requirements=["git_client_for_remote_git"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        requires_confirmation=True,
        policy_summary="Checkout remains bounded to explicit workspace destinations and requires confirmation before writing local repository materializations.",
        semantic_purpose="Materialize repository sources into a stable workspace root before bounded repository evidence extraction.",
        semantic_role="evidence_transform",
        consumes_evidence_types=["repo.source"],
        produces_evidence_types=["repo.workspace_root"],
        deliverable_intents=["architecture_report", "repository_onboarding_brief", "repository_audit_summary"],
        preferred_upstream_capability_ids=["repository_source_resolve"],
        preferred_downstream_capability_ids=["repository_inventory"],
        source_modalities_supported=["git_remote", "github_repo_url", "archive_url", "local_workspace_repo"],
        coverage_semantics="Materializes one explicit repository source into one bounded workspace root.",
        confidence_semantics="High confidence for local/archive/file checkout; remote checkout remains bounded and policy-gated.",
        tags=["repository", "checkout", "workspace"],
    ),
    CapabilityDefinition(
        capability_id="repository_inventory",
        name="Repository Inventory",
        description="Scan a bounded repository root and produce a structured inventory of files, directories, ecosystems, and candidate entry surfaces.",
        supported_step_kinds=["ingest"],
        capability_type="deterministic",
        input_contract_summary="Repository root, bounded scan controls, optional focus paths, and ignore rules.",
        output_contract_summary="Structured repository inventory with file summaries, config files, ecosystems, and candidate entry points.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        runtime_requirements=["workspace_access"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        policy_summary="Repository scanning stays inside the allowed workspace root and reports bounded/partial coverage honestly.",
        semantic_purpose="Produce bounded repository inventory evidence for downstream codebase analysis and reporting workflows.",
        semantic_role="evidence_source",
        produces_evidence_types=["repo.inventory"],
        deliverable_intents=["architecture_report", "repository_onboarding_brief", "repository_audit_summary"],
        preferred_upstream_capability_ids=["repository_source_resolve", "repository_checkout"],
        preferred_downstream_capability_ids=["source_structure_extract", "repository_execution_surface_inspect", "repository_evidence_bundle"],
        source_modalities_supported=["workspace_root"],
        coverage_semantics="Reports partial coverage honestly when file-count or byte limits are reached.",
        confidence_semantics="High confidence for enumerated files and configs; partial coverage is explicit.",
        tags=["repository", "inventory", "codebase"],
    ),
    CapabilityDefinition(
        capability_id="rss_fetch",
        name="Fetch RSS Feed",
        description="Load and normalize items from an RSS or Atom feed.",
        supported_step_kinds=["ingest"],
        capability_type="external_tool",
        input_contract_summary="Feed URL and optional item limits.",
        output_contract_summary="Normalized feed items and feed metadata.",
        runtime_requirements=["network_access"],
        provider_requirements=["rss_parser"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        enabled=False,
        tags=["rss", "feed"],
    ),
    CapabilityDefinition(
        capability_id="parse_json",
        name="Parse JSON",
        description="Parse JSON content into validated structured records.",
        supported_step_kinds=["transform"],
        capability_type="deterministic",
        input_contract_summary="JSON string or JSON-like object.",
        output_contract_summary="Parsed object graph or parsing errors.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        allowed_contexts=["authoring", "blueprint_planning", "build_planning", "runtime"],
        tags=["json", "parser"],
    ),
    CapabilityDefinition(
        capability_id="source_structure_extract",
        name="Source Structure Extract",
        description="Extract bounded structural information such as languages, imports, symbols, file roles, and framework hints from repository sources.",
        supported_step_kinds=["transform"],
        capability_type="deterministic",
        input_contract_summary="Repository inventory or bounded file set plus extraction profile and limits.",
        output_contract_summary="Structured module records, imports, symbols, file roles, and extraction coverage metadata.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        runtime_requirements=["workspace_access"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        policy_summary="Extraction remains bounded by repo scope, file count, and byte limits; unsupported languages are reported rather than silently ignored.",
        semantic_purpose="Extract repository structure evidence such as modules, symbols, imports, and framework hints.",
        semantic_role="evidence_transform",
        consumes_evidence_types=["repo.inventory"],
        produces_evidence_types=["repo.structure"],
        deliverable_intents=["architecture_report", "repository_onboarding_brief", "repository_audit_summary"],
        preferred_upstream_capability_ids=["repository_inventory"],
        preferred_downstream_capability_ids=["repository_relationship_map", "repository_execution_surface_inspect", "repository_evidence_bundle"],
        tags=["repository", "structure", "source"],
    ),
    CapabilityDefinition(
        capability_id="repository_relationship_map",
        name="Repository Relationship Map",
        description="Build bounded dependency and boundary hints across repository modules from extracted source structure.",
        supported_step_kinds=["transform"],
        capability_type="deterministic",
        input_contract_summary="Structured module extraction output and optional focus/boundary hints.",
        output_contract_summary="Dependency edges, module clusters, hotspots, and likely architecture boundaries.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        semantic_purpose="Derive bounded dependency, hotspot, and architecture boundary evidence from repository structure.",
        semantic_role="evidence_transform",
        consumes_evidence_types=["repo.structure"],
        produces_evidence_types=["repo.relationships"],
        deliverable_intents=["architecture_report", "repository_audit_summary"],
        preferred_upstream_capability_ids=["source_structure_extract"],
        preferred_downstream_capability_ids=["repository_evidence_bundle"],
        tags=["repository", "dependencies", "architecture"],
    ),
    CapabilityDefinition(
        capability_id="repository_execution_surface_inspect",
        name="Repository Execution Surface Inspect",
        description="Inspect bounded build/runtime/task/config surfaces across a repository.",
        supported_step_kinds=["transform", "validate"],
        capability_type="deterministic",
        input_contract_summary="Repository inventory, optional extracted structure, and bounded config/script inspection settings.",
        output_contract_summary="Build entry points, runtime entry points, task surfaces, config/env dependencies, and integration hints.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        runtime_requirements=["workspace_access"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        semantic_purpose="Inspect build/runtime/task/config surfaces to ground execution-oriented repository understanding.",
        semantic_role="evidence_transform",
        consumes_evidence_types=["repo.inventory", "repo.structure"],
        produces_evidence_types=["repo.execution_surface"],
        deliverable_intents=["architecture_report", "repository_onboarding_brief", "repository_audit_summary"],
        preferred_upstream_capability_ids=["repository_inventory", "source_structure_extract"],
        preferred_downstream_capability_ids=["repository_evidence_bundle"],
        tags=["repository", "runtime", "build-surface"],
    ),
    CapabilityDefinition(
        capability_id="deduplicate_items",
        name="Deduplicate Items",
        description="Remove duplicate records using deterministic matching keys.",
        supported_step_kinds=["transform"],
        capability_type="deterministic",
        input_contract_summary="List of records and matching keys or signatures.",
        output_contract_summary="Deduplicated records with duplicate counts.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        tags=["cleanup"],
    ),
    CapabilityDefinition(
        capability_id="web_search",
        name="Web Search",
        description="Search a bounded local corpus, supplied documents, or explicitly allowed fetched sources and return ranked results.",
        supported_step_kinds=["ingest"],
        capability_type="external_tool",
        input_contract_summary="Search query, optional corpus/source selector, optional fetch source URLs, and result limit.",
        output_contract_summary="Ranked search results with snippets, source metadata, and scores.",
        execution_kind="web_search",
        runtime_adapter="bounded_web_search",
        runtime_requirements=["bounded_search_index"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        policy_summary="Search uses a bounded corpus by default and only touches remote sources when explicit network policy is provided.",
        supported_actions=["search"],
        policy_hints={"supports_source_urls": True, "requires_allow_network_for_remote_sources": True},
        semantic_purpose="Acquire bounded research or monitoring sources through explicit search queries.",
        semantic_role="evidence_source",
        produces_evidence_types=["research.sources"],
        deliverable_intents=["research_briefing"],
        coverage_semantics="Returns ranked bounded search results, not exhaustive web coverage.",
        confidence_semantics="Confidence is limited to surfaced ranked results and snippets.",
        tags=["search", "retrieval"],
    ),
    CapabilityDefinition(
        capability_id="filesystem_list",
        name="Filesystem List",
        description="List files or directories inside the allowed workspace scope.",
        supported_step_kinds=["ingest"],
        capability_type="external_tool",
        input_contract_summary="Workspace-relative path and optional recursion or entry limits.",
        output_contract_summary="Structured file or directory listing inside the allowed scope.",
        execution_kind="filesystem",
        runtime_adapter="bounded_filesystem",
        runtime_requirements=["workspace_access"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        policy_summary="Only paths inside the allowed workspace scope are accessible.",
        tags=["filesystem", "workspace"],
    ),
    CapabilityDefinition(
        capability_id="filesystem_read",
        name="Filesystem Read",
        description="Read text content from a file inside the allowed workspace scope.",
        supported_step_kinds=["ingest", "transform"],
        capability_type="external_tool",
        input_contract_summary="Workspace-relative file path and optional byte or line limits.",
        output_contract_summary="File content preview plus file metadata.",
        execution_kind="filesystem",
        runtime_adapter="bounded_filesystem",
        runtime_requirements=["workspace_access"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        policy_summary="Read access is limited to the allowed workspace scope.",
        semantic_purpose="Read bounded local source material into the workflow evidence chain.",
        semantic_role="evidence_source",
        produces_evidence_types=["local.content", "data.table.raw"],
        # Declare the modality family so the planner's source-aware
        # capability binder can pick filesystem_read for local CSV / JSON
        # / text reads instead of falling through to fetch_url.
        source_modalities_supported=[
            "local_path",
            "local_file",
            "file",
            "csv",
            "tsv",
            "json_file",
            "workspace_root",
        ],
        coverage_semantics="Reads only explicitly addressed workspace files.",
        confidence_semantics="High confidence for the returned file content preview within byte or line limits.",
        tags=["filesystem", "workspace", "read"],
    ),
    CapabilityDefinition(
        capability_id="filesystem_write",
        name="Filesystem Write",
        description="Write text or JSON content into a file inside the allowed workspace scope.",
        supported_step_kinds=["persist"],
        capability_type="external_tool",
        input_contract_summary="Workspace-relative target path plus explicit write content.",
        output_contract_summary="Write confirmation with file path and byte count.",
        execution_kind="filesystem",
        runtime_adapter="bounded_filesystem",
        runtime_requirements=["workspace_access"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        requires_confirmation=True,
        policy_summary="Write access is limited to the allowed workspace scope and requires explicit write allowance.",
        semantic_purpose="Persist workflow outputs or intermediate files inside the bounded workspace.",
        semantic_role="delivery",
        consumes_evidence_types=["research.brief.draft", "artifact.saved", "local.content"],
        produces_evidence_types=["local.saved_output"],
        deliverable_intents=["research_briefing"],
        coverage_semantics="Writes only explicit payloads to explicit workspace paths.",
        confidence_semantics="High confidence for the persisted bytes and target path; no extra side effects are implied.",
        planning_visibility="reusable_allowed",
        tags=["filesystem", "workspace", "write"],
    ),
    CapabilityDefinition(
        capability_id="summarize_text_llm",
        name="Summarize Text With LLM",
        description="Generate concise summaries from text using a constrained LLM pass.",
        supported_step_kinds=["summarize"],
        capability_type="llm_assisted",
        input_contract_summary="Text corpus plus summary objective and output length constraints.",
        output_contract_summary="Summary text with optional supporting bullets.",
        execution_kind="llm_task",
        runtime_adapter="provider_runtime",
        provider_requirements=["model_config"],
        allowed_contexts=["authoring", "blueprint_planning", "build_planning", "runtime"],
        requires_review=True,
        semantic_purpose="Synthesize concise bounded research or report narratives from structured source material.",
        semantic_role="synthesis",
        consumes_evidence_types=["research.sources", "local.content", "data.table.raw", "data.validation.findings", "data.comparison.findings"],
        produces_evidence_types=["research.brief.draft", "data.summary.draft", "data.validation.report.draft", "data.comparison.report.draft"],
        deliverable_intents=["research_briefing", "dataset_summary_report", "dataset_validation_report", "dataset_comparison_report"],
        coverage_semantics="Summaries are grounded only in supplied source material and constraints.",
        confidence_semantics="Narrative confidence depends on supplied evidence quality and explicit model uncertainty.",
        planning_visibility="reusable_allowed",
        tags=["summary", "llm"],
    ),
    CapabilityDefinition(
        capability_id="rank_candidates",
        name="Rank Candidates",
        description="Score candidates using deterministic criteria and produce an ordered ranking.",
        supported_step_kinds=["rank"],
        capability_type="deterministic",
        input_contract_summary="Candidate list and ranking rubric.",
        output_contract_summary="Ranked candidates and score breakdown.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        semantic_purpose="Apply deterministic ranking to candidate sets within a bounded evidence chain.",
        semantic_role="evidence_transform",
        planning_visibility="reusable_allowed",
        tags=["ranking"],
    ),
    CapabilityDefinition(
        capability_id="validate_schema",
        name="Validate Schema",
        description="Validate structured payloads against schema or contract rules.",
        supported_step_kinds=["validate"],
        capability_type="deterministic",
        input_contract_summary="Structured payload plus schema or rule set.",
        output_contract_summary="Validation result with errors and warnings.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        allowed_contexts=["authoring", "blueprint_planning", "build_planning", "runtime"],
        semantic_purpose="Apply deterministic validation or quality gates to structured outputs before delivery.",
        semantic_role="quality_gate",
        consumes_evidence_types=["report.architecture.rendered", "report.quality", "local.content", "data.table.raw"],
        produces_evidence_types=["report.architecture.validated", "data.validation.findings"],
        coverage_semantics="Validates only the explicit payload and schema/rule set provided.",
        confidence_semantics="High confidence for deterministic schema checks; no claim beyond provided rules.",
        planning_visibility="reusable_allowed",
        tags=["schema", "guardrail"],
    ),
    CapabilityDefinition(
        capability_id="terminal_command",
        name="Terminal Command",
        description="Run an allowlisted terminal command inside the allowed workspace scope and capture stdout/stderr.",
        supported_step_kinds=["transform", "validate"],
        capability_type="external_tool",
        input_contract_summary="Command argv list, bounded working directory, and timeout.",
        output_contract_summary="Command result with stdout, stderr, return code, and working directory.",
        execution_kind="terminal_command",
        runtime_adapter="bounded_terminal",
        runtime_requirements=["workspace_access", "subprocess_runtime"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        default_timeout_seconds=5,
        policy_summary="Only allowlisted text-oriented commands run without shell expansion and only inside the allowed workspace scope.",
        semantic_purpose="Apply bounded local text processing or validation over explicit intermediate artifacts.",
        semantic_role="evidence_transform",
        produces_evidence_types=["local.derived"],
        coverage_semantics="Operates only on explicit command inputs inside the bounded workspace.",
        confidence_semantics="Confidence is limited to the command result and exit status inside allowlisted execution.",
        tags=["terminal", "workspace"],
    ),
    CapabilityDefinition(
        capability_id="send_email",
        name="Send Email",
        description="Send an outbound email notification to configured recipients.",
        supported_step_kinds=["notify"],
        capability_type="notification",
        input_contract_summary="Rendered message payload and recipient list.",
        output_contract_summary="Delivery status and provider response metadata.",
        execution_kind="blocked",
        runtime_adapter="unsupported",
        runtime_requirements=["smtp_or_email_api"],
        provider_requirements=["email_provider"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        requires_review=True,
        tags=["email", "notification"],
    ),
    CapabilityDefinition(
        capability_id="browser_interaction",
        name="Browser Interaction",
        description="Open and interact with bounded local or remote pages using explicit browser actions, including auditable save/download handoffs.",
        supported_step_kinds=["ingest", "transform", "persist"],
        capability_type="external_tool",
        input_contract_summary="Page URL or local file path, explicit browser action kind, bounded target selector, and policy options.",
        output_contract_summary="Structured page/result state, action audit metadata, and optional screenshot or navigation continuation output.",
        execution_kind="browser_interaction",
        runtime_adapter="bounded_browser",
        runtime_requirements=["workspace_access"],
        provider_requirements=["network_access_for_remote_pages"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        requires_confirmation=True,
        policy_summary="Browser actions stay selector-bounded, policy-gated, and auditable; save/download outputs remain workspace-scoped and unsupported interaction loops remain blocked or review-only.",
        supported_actions=["open_page", "extract_text", "inspect_links", "click", "fill", "select", "submit", "screenshot", "wait_for_target", "save_text", "download_file"],
        policy_hints={"requires_allow_network_for_remote_pages": True, "interactive_actions_require_confirmation": True},
        semantic_purpose="Acquire bounded page evidence or save explicit browser-derived outputs without autonomous browsing loops.",
        semantic_role="evidence_source",
        produces_evidence_types=["browser.page", "local.saved_output"],
        coverage_semantics="Only explicit selector-bounded page actions are supported.",
        confidence_semantics="Confidence is limited to the observed page state and saved outputs from explicit actions.",
        tags=["browser", "page", "extract"],
    ),
    CapabilityDefinition(
        capability_id="run_python_template",
        name="Run Python Template",
        description="Execute a reviewed Python template for deterministic processing or enrichment.",
        supported_step_kinds=["transform", "persist"],
        capability_type="code_template",
        input_contract_summary="Template identifier plus structured input payload.",
        output_contract_summary="Template result payload and execution metadata.",
        execution_kind="blocked",
        runtime_adapter="template_runtime",
        runtime_requirements=["python_runtime"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        requires_review=True,
        template_backed_only=True,
        tags=["python", "template"],
    ),
    CapabilityDefinition(
        capability_id="write_artifact",
        name="Write Artifact",
        description="Persist final outputs as named artifacts in the generated runtime workspace.",
        supported_step_kinds=["persist"],
        capability_type="deterministic",
        input_contract_summary="Artifact payload, artifact type, and storage metadata.",
        output_contract_summary="Artifact reference and persistence status.",
        execution_kind="artifact_writer",
        runtime_adapter="artifact_writer",
        runtime_requirements=["artifact_storage"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        semantic_purpose="Persist final or intermediate workflow outputs as explicit delivery artifacts.",
        semantic_role="delivery",
        consumes_evidence_types=["repo.evidence_bundle", "report.architecture.rendered", "report.architecture.validated", "research.brief.draft", "research.brief.rendered", "data.summary.draft", "data.validation.report.draft", "data.comparison.report.draft", "data.validation.findings"],
        produces_evidence_types=["artifact.saved"],
        deliverable_intents=["architecture_report", "repository_onboarding_brief", "repository_audit_summary", "research_briefing", "dataset_summary_report", "dataset_validation_report", "dataset_comparison_report"],
        coverage_semantics="Writes explicit artifacts only; artifact presence is part of the execution truth.",
        confidence_semantics="High confidence for saved artifact name and payload origin.",
        planning_visibility="reusable_allowed",
        tags=["artifact", "storage"],
    ),
    CapabilityDefinition(
        capability_id="research_briefing_render",
        name="Research Briefing Render",
        description="Render a structured research briefing draft into delivery-ready markdown.",
        supported_step_kinds=["persist"],
        capability_type="deterministic",
        input_contract_summary="Structured research briefing draft plus optional title and formatting hints.",
        output_contract_summary="Rendered markdown research briefing with summary, key findings, and unknowns.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        semantic_purpose="Render research briefing drafts into stable markdown deliverables before artifact persistence.",
        semantic_role="render",
        consumes_evidence_types=["research.brief.draft"],
        produces_evidence_types=["research.brief.rendered"],
        deliverable_intents=["research_briefing"],
        preferred_upstream_capability_ids=["summarize_text_llm"],
        preferred_downstream_capability_ids=["write_artifact"],
        tags=["research", "briefing", "render"],
    ),
    CapabilityDefinition(
        capability_id="repository_evidence_bundle",
        name="Repository Evidence Bundle",
        description="Package repository inventory, structure, dependency, and execution-surface evidence into a reusable bounded evidence bundle.",
        supported_step_kinds=["persist"],
        capability_type="deterministic",
        input_contract_summary="Structured repository analysis step outputs and evidence bundling configuration.",
        output_contract_summary="Evidence bundle JSON with file refs, confidence markers, coverage limits, and summary tables.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        semantic_purpose="Bundle repository analysis evidence into a reusable artifact for downstream synthesis and review.",
        semantic_role="evidence_bundle",
        consumes_evidence_types=["repo.inventory", "repo.structure", "repo.relationships", "repo.execution_surface"],
        produces_evidence_types=["repo.evidence_bundle"],
        deliverable_intents=["architecture_report", "repository_onboarding_brief", "repository_audit_summary"],
        preferred_upstream_capability_ids=[
            "repository_inventory",
            "source_structure_extract",
            "repository_relationship_map",
            "repository_execution_surface_inspect",
        ],
        preferred_downstream_capability_ids=[
            "architecture_report_brief",
            "architecture_report_synthesis",
            "architecture_report_synthesis_llm",
        ],
        tags=["repository", "evidence", "artifact"],
    ),
    CapabilityDefinition(
        capability_id="architecture_report_brief",
        name="Architecture Report Brief",
        description="Convert a repository evidence bundle into a bounded briefing packet for downstream report synthesis.",
        supported_step_kinds=["transform", "summarize"],
        capability_type="deterministic",
        input_contract_summary="Repository evidence bundle plus objective and audience hints.",
        output_contract_summary="Grounded report brief with coverage markers, focus modules, risks, and citation candidates.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        semantic_purpose="Prepare grounded, bounded architecture-report inputs from repository evidence.",
        semantic_role="evidence_transform",
        consumes_evidence_types=["repo.evidence_bundle"],
        produces_evidence_types=["report.architecture.brief"],
        deliverable_intents=["architecture_report"],
        preferred_upstream_capability_ids=["repository_evidence_bundle"],
        preferred_downstream_capability_ids=["architecture_report_synthesis_llm", "architecture_report_synthesis"],
        tags=["repository", "architecture-report", "brief"],
    ),
    CapabilityDefinition(
        capability_id="architecture_report_synthesis_llm",
        name="Architecture Report Synthesis With LLM",
        description="Generate a grounded architecture draft from a bounded architecture-report brief using an LLM step.",
        supported_step_kinds=["summarize"],
        capability_type="llm_assisted",
        input_contract_summary="Architecture report brief, synthesis objective, audience, and output section contract.",
        output_contract_summary="Structured architecture draft grounded in the supplied brief and citation set.",
        execution_kind="llm_task",
        runtime_adapter="provider_runtime",
        provider_requirements=["model_config"],
        allowed_contexts=["authoring", "blueprint_planning", "build_planning", "runtime"],
        requires_review=True,
        semantic_purpose="Synthesize grounded architectural narrative from structured evidence rather than raw repository reads.",
        semantic_role="synthesis",
        consumes_evidence_types=["report.architecture.brief"],
        produces_evidence_types=["report.architecture.draft"],
        deliverable_intents=["architecture_report"],
        preferred_upstream_capability_ids=["architecture_report_brief"],
        preferred_downstream_capability_ids=["architecture_report_render", "architecture_report_quality_gate"],
        tags=["repository", "architecture-report", "llm"],
    ),
    CapabilityDefinition(
        capability_id="architecture_report_synthesis",
        name="Architecture Report Synthesis",
        description="Generate a bounded architecture-oriented report from a structured repository evidence bundle.",
        supported_step_kinds=["summarize"],
        capability_type="deterministic",
        input_contract_summary="Repository evidence bundle plus objective and audience hints.",
        output_contract_summary="Architecture report markdown, key findings, and report metadata grounded in structured evidence.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        semantic_purpose="Provide a deterministic fallback architecture-report draft from repository evidence.",
        semantic_role="synthesis",
        consumes_evidence_types=["repo.evidence_bundle"],
        produces_evidence_types=["report.architecture.draft"],
        deliverable_intents=["architecture_report"],
        preferred_upstream_capability_ids=["repository_evidence_bundle"],
        preferred_downstream_capability_ids=["architecture_report_render", "architecture_report_quality_gate"],
        tags=["repository", "architecture-report", "summary"],
    ),
    CapabilityDefinition(
        capability_id="architecture_report_render",
        name="Architecture Report Render",
        description="Render a structured architecture report draft into delivery-ready markdown while preserving citations and coverage notes.",
        supported_step_kinds=["persist"],
        capability_type="deterministic",
        input_contract_summary="Structured architecture report draft plus rendering and formatting preferences.",
        output_contract_summary="Rendered markdown report with section ordering, coverage note, and cited file references.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        semantic_purpose="Render architecture report drafts into stable markdown deliverables.",
        semantic_role="render",
        consumes_evidence_types=["report.architecture.draft"],
        produces_evidence_types=["report.architecture.rendered"],
        deliverable_intents=["architecture_report"],
        preferred_upstream_capability_ids=["architecture_report_synthesis_llm", "architecture_report_synthesis"],
        preferred_downstream_capability_ids=["architecture_report_quality_gate", "write_artifact"],
        tags=["repository", "architecture-report", "render"],
    ),
    CapabilityDefinition(
        capability_id="architecture_report_quality_gate",
        name="Architecture Report Quality Gate",
        description="Validate that a rendered architecture report includes required sections, coverage notes, and evidence-backed citations.",
        supported_step_kinds=["validate", "review"],
        capability_type="deterministic",
        input_contract_summary="Rendered architecture report plus optional report brief/evidence references.",
        output_contract_summary="Quality-gate result with pass/fail state, missing sections, citation coverage, and review notes.",
        execution_kind="python_function",
        runtime_adapter="builtin_python",
        allowed_contexts=["authoring", "blueprint_planning", "build_planning", "runtime"],
        semantic_purpose="Apply explicit delivery-quality checks to architecture reports before final persistence or review.",
        semantic_role="quality_gate",
        consumes_evidence_types=["report.architecture.rendered", "report.architecture.brief"],
        produces_evidence_types=["report.quality", "report.architecture.validated"],
        deliverable_intents=["architecture_report"],
        preferred_upstream_capability_ids=["architecture_report_render", "architecture_report_brief"],
        preferred_downstream_capability_ids=["write_artifact"],
        quality_gate_relevance="required",
        tags=["repository", "architecture-report", "quality"],
    ),
    CapabilityDefinition(
        capability_id="human_review_gate",
        name="Human Review Gate",
        description="Require explicit operator or reviewer approval before continuing.",
        supported_step_kinds=["review", "validate"],
        capability_type="human_gate",
        input_contract_summary="Review package, reviewer instructions, and acceptance decision fields.",
        output_contract_summary="Approval or rejection decision with review notes.",
        allowed_contexts=["authoring", "blueprint_planning", "runtime"],
        requires_review=True,
        semantic_purpose="Insert an explicit operator review checkpoint into the planning graph when automation should pause.",
        semantic_role="review_gate",
        produces_evidence_types=["review.decision"],
        confidence_semantics="Review outcomes are explicit operator signals rather than inferred automation.",
        tags=["review", "approval"],
    ),
    # Generic bounded-LLM reasoning capability. Registering the agentic
    # reasoning loop itself as a capability lets solvability see that most
    # "analyze / summarize / transform / synthesize" work is reachable even
    # when no specialised tool is registered — an agentic node with file
    # read + bounded reasoning can cover it. WITHOUT this entry the
    # front-chain systematically over-reports capability gaps for ordinary
    # "read this and summarise it" tasks. See solvability_validator for
    # the matching step_kind-fallback logic that gives this capability
    # universal reachability across the listed step kinds.
    CapabilityDefinition(
        capability_id="general_agentic_reasoning",
        name="General Agentic Reasoning",
        description=(
            "Bounded LLM reasoning loop backed by node_loop_executor. Can "
            "read input evidence, produce summaries / analysis / structured "
            "content, and decide between provided tools. Not a specialised "
            "tool — the universal reasoning backbone every workflow has "
            "access to."
        ),
        supported_step_kinds=["transform", "summarize", "validate", "review"],
        capability_type="llm_assisted",
        input_contract_summary=(
            "Evidence bundle (text, structured records, or file content) plus "
            "a natural-language objective. Optional tool set for inline use."
        ),
        output_contract_summary=(
            "Structured reasoning output — free-form text, JSON, or a "
            "summary — produced inside a bounded agentic loop."
        ),
        execution_kind="llm_task",
        runtime_adapter="node_loop_executor",
        runtime_requirements=["llm_provider"],
        provider_requirements=["chat_completion"],
        allowed_contexts=["authoring", "blueprint_planning", "build_planning", "runtime"],
        policy_summary=(
            "Reasoning-only path: no network, no filesystem, no shell by "
            "itself. Side-effect capabilities must be explicitly attached "
            "as tools to the loop to have any effect."
        ),
        semantic_purpose=(
            "Universal reasoning backbone for analysis, summarisation, "
            "synthesis, and validation tasks when no specialised tool "
            "applies."
        ),
        semantic_role="evidence_transform",
        consumes_evidence_types=["text.content", "local.content", "research.sources"],
        produces_evidence_types=["text.summary", "text.analysis", "text.synthesis"],
        coverage_semantics=(
            "Covers bounded reasoning over provided evidence. Does not "
            "fabricate evidence it was not given."
        ),
        confidence_semantics=(
            "Confidence depends on the quality and completeness of the "
            "evidence supplied to the loop."
        ),
        planning_visibility="reusable_allowed",
        tags=["llm", "agentic", "reasoning", "universal"],
    ),
    # -----------------------------------------------------------------
    # Coding Agent — the "escape hatch" for everything the registry
    # doesn't model. Authors and executes a fresh python/bash script
    # inside a bounded sandbox. Picked as LAST-RESORT by the planner
    # only when the per-step-kind pool is otherwise empty (or when the
    # Shape-LLM explicitly escalates). Every invocation is
    # review-required by default; the execution sandbox enforces
    # workspace scope, timeout, memory, and optional network bounds.
    # -----------------------------------------------------------------
    CapabilityDefinition(
        capability_id="coding_agent",
        name="Coding Agent",
        description=(
            "Author and execute a fresh python or bash script to solve a "
            "bounded data/compute/transform task inside a sandboxed "
            "workspace. Chosen when no specialised capability covers the "
            "subgoal — the planner's generalist escape hatch."
        ),
        # Broad step-kind support so the planner can reach for it
        # whenever the specialised pool is unmatched. The fallback
        # ordering in capability_binder keeps it out of the preferred
        # list until all concrete alternatives fail.
        supported_step_kinds=["ingest", "transform", "validate", "persist", "summarize"],
        capability_type="llm_assisted",
        input_contract_summary=(
            "Natural-language task description, input_evidence_kinds, "
            "output_evidence_kinds, language preference (python|bash), "
            "workspace scope, allow_network bool, timeout_seconds, "
            "memory_mb_cap, optional seed files."
        ),
        output_contract_summary=(
            "Script source, stdout/stderr preview, exit code, produced "
            "files list, structured evidence emission."
        ),
        execution_kind="sandboxed_script",
        runtime_adapter="bounded_code_runner",
        runtime_requirements=[
            "workspace_access",
            "sandboxed_execution",
            "llm_provider",
        ],
        provider_requirements=["chat_completion"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        requires_review=True,
        policy_summary=(
            "Scripts run in a fresh sandbox with workspace-scoped "
            "filesystem, optional bounded network, and hard time / "
            "memory caps. No persistent side effects outside the "
            "declared workspace. Requires explicit reviewer approval "
            "for any persist-family invocation."
        ),
        supported_actions=[
            "write_python",
            "run_python",
            "write_bash",
            "run_bash",
            "iterate_on_error",
        ],
        policy_hints={
            "requires_sandbox": True,
            "default_language": "python",
            "network_default": "deny",
            "timeout_default_seconds": 120,
            "memory_default_mb": 512,
            "max_iterations_default": 3,
            "persist_requires_review": True,
        },
        semantic_purpose=(
            "Escape hatch for tasks whose transform / ingest / validate "
            "/ persist isn't covered by a dedicated capability. "
            "Converts 'registry gap' into 'on-the-fly script' without "
            "regressing to an unbounded autonomous agent."
        ),
        # No first-class enum value for "generalist_executor"; the closest
        # honest fit is ``evidence_transform`` since the script always
        # consumes and produces evidence.
        semantic_role="evidence_transform",
        consumes_evidence_types=[
            "text.content",
            "local.content",
            "data.table.raw",
            "text.analysis",
        ],
        produces_evidence_types=[
            "script.output",
            "data.derived",
            "local.content",
        ],
        source_modalities_supported=[
            "local_path",
            "local_file",
            "file",
            "csv",
            "tsv",
            "json_file",
            "workspace_root",
            "mixed",
        ],
        coverage_semantics=(
            "Covers the specific script the planner asks it to author; "
            "not an open-ended autonomous agent."
        ),
        confidence_semantics=(
            "High confidence for narrow deterministic transforms; "
            "medium for heuristic cleanup; low (review-required) for "
            "anything that rewrites business-critical schemas."
        ),
        planning_visibility="reusable_allowed",
        # ``generalist_fallback`` is the structural marker the planner's
        # binder reads. Any capability with this tag is preserved in the
        # pool alongside the top-k specialists (one preserve slot per
        # subgoal) so Shape-LLM always has a generalist option when the
        # specialists look domain-wrong.
        tags=["coding", "script", "generalist", "generalist_fallback", "sandbox", "fallback"],
    ),
]


CAPABILITY_BINDINGS: list[CapabilityBindingDefinition] = [
    CapabilityBindingDefinition(
        step_kind="ingest",
        capability_ids=[
            "fetch_url",
            "rss_fetch",
            "web_search",
            "filesystem_list",
            "filesystem_read",
            "browser_interaction",
            "repository_source_resolve",
            "repository_checkout",
            "repository_inventory",
            "coding_agent",  # last-resort fallback
        ],
        preferred_capability_ids=[
            "repository_source_resolve",
            "repository_inventory",
            "web_search",
            "filesystem_read",
            "fetch_url",
            "repository_checkout",
        ],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
        notes=["Ingest capabilities are concrete fetch implementations, not planning-only abstractions."],
    ),
    CapabilityBindingDefinition(
        step_kind="transform",
        capability_ids=["parse_json", "deduplicate_items", "run_python_template", "filesystem_read", "terminal_command", "browser_interaction", "source_structure_extract", "repository_relationship_map", "repository_execution_surface_inspect", "architecture_report_brief", "general_agentic_reasoning", "coding_agent"],
        preferred_capability_ids=["parse_json", "deduplicate_items", "filesystem_read", "terminal_command", "source_structure_extract", "architecture_report_brief"],
        allowed_contexts=["authoring", "blueprint_planning", "build_planning", "runtime"],
    ),
    CapabilityBindingDefinition(
        step_kind="summarize",
        capability_ids=["summarize_text_llm", "architecture_report_synthesis", "architecture_report_synthesis_llm", "architecture_report_brief", "general_agentic_reasoning", "coding_agent"],
        preferred_capability_ids=["summarize_text_llm", "architecture_report_brief", "architecture_report_synthesis", "architecture_report_synthesis_llm"],
        allowed_contexts=["authoring", "blueprint_planning", "build_planning", "runtime"],
    ),
    CapabilityBindingDefinition(
        step_kind="rank",
        capability_ids=["rank_candidates"],
        preferred_capability_ids=["rank_candidates"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
    ),
    CapabilityBindingDefinition(
        step_kind="validate",
        capability_ids=["validate_schema", "human_review_gate", "terminal_command", "repository_execution_surface_inspect", "architecture_report_quality_gate", "general_agentic_reasoning", "coding_agent"],
        preferred_capability_ids=["architecture_report_quality_gate", "validate_schema", "terminal_command", "repository_execution_surface_inspect"],
        allowed_contexts=["authoring", "blueprint_planning", "build_planning", "runtime"],
    ),
    CapabilityBindingDefinition(
        step_kind="notify",
        capability_ids=["send_email"],
        preferred_capability_ids=["send_email"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
    ),
    CapabilityBindingDefinition(
        step_kind="persist",
        capability_ids=["write_artifact", "run_python_template", "filesystem_write", "browser_interaction", "repository_evidence_bundle", "architecture_report_render", "research_briefing_render", "coding_agent"],
        preferred_capability_ids=["repository_evidence_bundle", "architecture_report_render", "research_briefing_render", "write_artifact", "filesystem_write", "browser_interaction"],
        allowed_contexts=["blueprint_planning", "build_planning", "runtime"],
    ),
    CapabilityBindingDefinition(
        step_kind="review",
        capability_ids=["human_review_gate", "general_agentic_reasoning"],
        preferred_capability_ids=["human_review_gate"],
        allowed_contexts=["authoring", "blueprint_planning", "runtime"],
    ),
]


DELIVERABLE_INTENTS: list[CapabilityDeliverableIntentDefinition] = [
    CapabilityDeliverableIntentDefinition(
        intent_id="research_briefing",
        name="Research Briefing",
        description="Collect bounded research sources, synthesize a grounded briefing, and publish a delivery artifact.",
        domain="research",
        keywords=["brief", "briefing", "research briefing"],
        deliverable_forms=["briefing"],
        analysis_frames=["summary"],
        decision_postures=["descriptive"],
        target_objects=["web_resource", "local_workspace"],
        primary_objectives=["collect", "synthesize", "deliver"],
        workflow_shapes=["bounded_workflow", "review_gated_workflow"],
        source_modalities_supported=["web_source", "local_path", "mixed", "unknown"],
        evidence_basis_supported=["web_sources", "local_documents"],
        required_evidence_types=["research.sources", "research.brief.draft"],
        optional_evidence_types=["artifact.saved"],
        final_output_evidence_type="artifact.saved",
        default_output_artifacts=["research_briefing.md"],
        workflow_steps=[
            CapabilityIntentWorkflowStepTemplate(
                step_id="collect_research_sources",
                node_name="research_sources",
                logical_step_kind="ingest",
                capability_id="web_search",
                semantic_role="evidence_source",
                purpose="Collect bounded research sources before synthesis begins.",
                input_intent="Research question, search scope, and bounded source policy.",
                output_intent="Ranked research sources and snippets.",
                produces_evidence_types=["research.sources"],
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="synthesize_research_briefing",
                node_name="research_synthesis",
                logical_step_kind="summarize",
                capability_id="summarize_text_llm",
                semantic_role="synthesis",
                purpose="Synthesize a concise research briefing from the collected sources.",
                input_intent="Bounded research sources plus summary objective and audience.",
                output_intent="Structured briefing draft grounded in the supplied source material.",
                consumes_evidence_types=["research.sources"],
                produces_evidence_types=["research.brief.draft"],
                default_args={
                    "task_prompt": (
                        "Summarize the supplied research sources into a concise engineering-oriented briefing. "
                        "Use only the provided sources and state uncertainty when evidence is incomplete."
                    ),
                    "expected_output": {
                        "summary": "string",
                        "key_findings": ["string"],
                        "unknowns": ["string"],
                    },
                },
                human_review_required=True,
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="render_research_briefing",
                node_name="research_render",
                logical_step_kind="persist",
                capability_id="research_briefing_render",
                semantic_role="render",
                purpose="Render the structured research briefing draft into delivery-ready markdown.",
                input_intent="Structured research briefing draft ready for publication.",
                output_intent="Rendered markdown research briefing.",
                consumes_evidence_types=["research.brief.draft"],
                produces_evidence_types=["research.brief.rendered"],
                default_args={
                    "source_step": "synthesize_research_briefing",
                    "title": "Research Briefing",
                },
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="persist_research_briefing",
                node_name="research_delivery",
                logical_step_kind="persist",
                capability_id="write_artifact",
                semantic_role="delivery",
                purpose="Persist the final research briefing as a delivery artifact.",
                input_intent="Rendered markdown research briefing ready for publication.",
                output_intent="Saved research briefing artifact.",
                consumes_evidence_types=["research.brief.rendered"],
                produces_evidence_types=["artifact.saved"],
                artifact_expectations=["research_briefing.md"],
                default_args={
                    "source_step": "render_research_briefing",
                    "source_field": "report_markdown",
                    "artifact_filename": "research_briefing.md",
                    "artifact_format": "markdown",
                },
            ),
        ],
    ),
    CapabilityDeliverableIntentDefinition(
        intent_id="dataset_summary",
        name="Dataset Summary",
        description="Analyze structured local data and produce a concise summary report or artifact without forcing a research-style workflow.",
        domain="data_analysis",
        keywords=["dataset summary", "csv report", "spreadsheet summary", "data summary", "metrics summary"],
        deliverable_forms=["report", "artifact_bundle", "dataset"],
        analysis_frames=["summary", "assessment", "audit"],
        decision_postures=["descriptive", "evaluative", "comparative"],
        target_objects=["dataset", "local_workspace"],
        primary_objectives=["analyze", "transform", "synthesize", "validate", "deliver"],
        workflow_shapes=["single_service", "bounded_workflow", "review_gated_workflow"],
        source_modalities_supported=["local_path", "mixed", "unknown"],
        evidence_basis_supported=["structured_data"],
        required_evidence_types=[],
        optional_evidence_types=["artifact.saved"],
        final_output_evidence_type="artifact.saved",
        default_output_artifacts=["dataset_summary.md"],
    ),
    CapabilityDeliverableIntentDefinition(
        intent_id="dataset_summary_report",
        name="Dataset Summary Report",
        description="Read local structured data, summarize the main trends, and persist a markdown report.",
        domain="data_analysis",
        keywords=["csv summary", "spreadsheet summary", "data report", "metrics report", "dataset report"],
        deliverable_forms=["report"],
        analysis_frames=["summary"],
        decision_postures=["descriptive"],
        target_objects=["dataset", "local_workspace"],
        primary_objectives=["synthesize", "analyze", "deliver"],
        workflow_shapes=["bounded_workflow", "review_gated_workflow"],
        source_modalities_supported=["local_path", "mixed", "unknown"],
        evidence_basis_supported=["structured_data"],
        required_evidence_types=["data.table.raw", "data.summary.draft"],
        optional_evidence_types=["artifact.saved"],
        final_output_evidence_type="artifact.saved",
        default_output_artifacts=["dataset_summary.md"],
        workflow_steps=[
            CapabilityIntentWorkflowStepTemplate(
                step_id="read_structured_data_sources",
                node_name="data_sources",
                logical_step_kind="ingest",
                capability_id="filesystem_read",
                semantic_role="evidence_source",
                purpose="Read bounded structured-data source files from the local workspace.",
                input_intent="Local CSV or spreadsheet source paths inside the workspace.",
                output_intent="Bounded raw structured-data content for downstream analysis.",
                produces_evidence_types=["data.table.raw"],
                default_args={"action": "read"},
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="summarize_dataset_report",
                node_name="data_summary",
                logical_step_kind="summarize",
                capability_id="summarize_text_llm",
                semantic_role="synthesis",
                purpose="Summarize the main metrics, trends, and findings from the structured data.",
                input_intent="Bounded structured-data content and reporting objective.",
                output_intent="Structured markdown-ready summary draft.",
                consumes_evidence_types=["data.table.raw"],
                produces_evidence_types=["data.summary.draft"],
                default_args={
                    "task_prompt": (
                        "Summarize the supplied structured-data content into a concise markdown report. "
                        "Highlight the main trends, outliers, and notable grouped findings grounded only in the provided data."
                    ),
                    "expected_output": {
                        "summary": "string",
                        "key_findings": ["string"],
                        "unknowns": ["string"],
                    },
                },
                human_review_required=True,
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="persist_dataset_summary_report",
                node_name="data_delivery",
                logical_step_kind="persist",
                capability_id="write_artifact",
                semantic_role="delivery",
                purpose="Persist the structured-data summary as a markdown report artifact.",
                input_intent="Markdown-ready structured-data summary draft.",
                output_intent="Saved dataset summary artifact.",
                consumes_evidence_types=["data.summary.draft"],
                produces_evidence_types=["artifact.saved"],
                artifact_expectations=["dataset_summary.md"],
                default_args={
                    "source_step": "summarize_dataset_report",
                    "source_field": "summary",
                    "artifact_filename": "dataset_summary.md",
                    "artifact_format": "markdown",
                },
            ),
        ],
    ),
    CapabilityDeliverableIntentDefinition(
        intent_id="dataset_validation_report",
        name="Dataset Validation Report",
        description="Read local structured data, validate expected schema/columns, and persist a validation report.",
        domain="data_analysis",
        keywords=["validate csv", "spreadsheet validation", "schema report", "missing columns", "data validation"],
        deliverable_forms=["report"],
        analysis_frames=["audit", "assessment"],
        decision_postures=["evaluative", "descriptive"],
        target_objects=["dataset", "local_workspace"],
        primary_objectives=["validate", "deliver"],
        workflow_shapes=["bounded_workflow", "review_gated_workflow"],
        source_modalities_supported=["local_path", "mixed", "unknown"],
        evidence_basis_supported=["structured_data"],
        required_evidence_types=["data.table.raw", "data.validation.findings"],
        optional_evidence_types=["artifact.saved"],
        final_output_evidence_type="artifact.saved",
        default_output_artifacts=["dataset_validation_report.md"],
        workflow_steps=[
            CapabilityIntentWorkflowStepTemplate(
                step_id="read_structured_data_sources",
                node_name="data_sources",
                logical_step_kind="ingest",
                capability_id="filesystem_read",
                semantic_role="evidence_source",
                purpose="Read bounded structured-data source files from the local workspace.",
                input_intent="Local CSV or spreadsheet source paths inside the workspace.",
                output_intent="Bounded raw structured-data content for downstream validation.",
                produces_evidence_types=["data.table.raw"],
                default_args={"action": "read"},
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="validate_structured_data_contract",
                node_name="data_validation",
                logical_step_kind="validate",
                capability_id="validate_schema",
                semantic_role="quality_gate",
                purpose="Validate that required columns or schema fields are present in the structured data.",
                input_intent="Bounded structured-data content and required schema hints.",
                output_intent="Validation findings over the supplied structured data.",
                consumes_evidence_types=["data.table.raw"],
                produces_evidence_types=["data.validation.findings"],
                default_args={
                    "source_step": "read_structured_data_sources",
                    "source_field": "content",
                },
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="persist_dataset_validation_report",
                node_name="data_delivery",
                logical_step_kind="persist",
                capability_id="write_artifact",
                semantic_role="delivery",
                purpose="Persist the validation findings as a markdown report artifact.",
                input_intent="Structured validation findings over the data source.",
                output_intent="Saved dataset validation report artifact.",
                consumes_evidence_types=["data.validation.findings"],
                produces_evidence_types=["artifact.saved"],
                artifact_expectations=["dataset_validation_report.md"],
                default_args={
                    "source_step": "validate_structured_data_contract",
                    "artifact_filename": "dataset_validation_report.md",
                    "artifact_format": "markdown",
                },
            ),
        ],
    ),
    CapabilityDeliverableIntentDefinition(
        intent_id="dataset_comparison_report",
        name="Dataset Comparison Report",
        description="Read local structured data, compare grouped metrics, and persist a comparison-oriented markdown report.",
        domain="data_analysis",
        keywords=["compare csv", "compare spreadsheet", "comparison report", "revenue comparison", "category comparison"],
        deliverable_forms=["report"],
        analysis_frames=["summary", "assessment"],
        decision_postures=["comparative"],
        target_objects=["dataset", "local_workspace"],
        primary_objectives=["transform", "synthesize", "deliver"],
        workflow_shapes=["bounded_workflow", "review_gated_workflow"],
        source_modalities_supported=["local_path", "mixed", "unknown"],
        evidence_basis_supported=["structured_data"],
        required_evidence_types=["data.table.raw", "data.comparison.report.draft"],
        optional_evidence_types=["artifact.saved"],
        final_output_evidence_type="artifact.saved",
        default_output_artifacts=["dataset_comparison_report.md"],
        workflow_steps=[
            CapabilityIntentWorkflowStepTemplate(
                step_id="read_structured_data_sources",
                node_name="data_sources",
                logical_step_kind="ingest",
                capability_id="filesystem_read",
                semantic_role="evidence_source",
                purpose="Read bounded structured-data source files from the local workspace.",
                input_intent="Local CSV or spreadsheet source paths inside the workspace.",
                output_intent="Bounded raw structured-data content for downstream comparison analysis.",
                produces_evidence_types=["data.table.raw"],
                default_args={"action": "read"},
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="compare_structured_data_metrics",
                node_name="data_comparison",
                logical_step_kind="summarize",
                capability_id="summarize_text_llm",
                semantic_role="synthesis",
                purpose="Compare grouped metrics and differences across the structured data sources.",
                input_intent="Bounded structured-data content and comparison objective.",
                output_intent="Comparison-focused markdown-ready findings draft.",
                consumes_evidence_types=["data.table.raw"],
                produces_evidence_types=["data.comparison.report.draft"],
                default_args={
                    "task_prompt": (
                        "Compare the supplied structured-data content and produce a concise markdown report. "
                        "Focus on grouped differences, directional changes, and notable category or regional contrasts grounded only in the provided data."
                    ),
                    "expected_output": {
                        "summary": "string",
                        "key_findings": ["string"],
                        "unknowns": ["string"],
                    },
                },
                human_review_required=True,
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="persist_dataset_comparison_report",
                node_name="data_delivery",
                logical_step_kind="persist",
                capability_id="write_artifact",
                semantic_role="delivery",
                purpose="Persist the comparison findings as a markdown report artifact.",
                input_intent="Comparison-oriented markdown-ready findings draft.",
                output_intent="Saved dataset comparison report artifact.",
                consumes_evidence_types=["data.comparison.report.draft"],
                produces_evidence_types=["artifact.saved"],
                artifact_expectations=["dataset_comparison_report.md"],
                default_args={
                    "source_step": "compare_structured_data_metrics",
                    "source_field": "summary",
                    "artifact_filename": "dataset_comparison_report.md",
                    "artifact_format": "markdown",
                },
            ),
        ],
    ),
    CapabilityDeliverableIntentDefinition(
        intent_id="architecture_report",
        name="Architecture Report",
        description="Analyze a software repository and produce a grounded architectural report with evidence, coverage notes, and citations.",
        domain="repository_analysis",
        keywords=[
            "architecture",
            "architectural",
            "repository",
            "repo",
            "codebase",
            "system design",
            "report",
            "analysis",
            "架构",
            "架构报告",
            "仓库",
            "代码仓库",
            "依赖关系",
            "构建入口",
            "运行入口",
            "模块",
            "软件仓库",
            "整体架构",
        ],
        deliverable_forms=["report"],
        analysis_frames=["architecture"],
        decision_postures=["descriptive", "recommendation"],
        target_objects=["repository"],
        primary_objectives=["analyze", "synthesize", "deliver"],
        workflow_shapes=["bounded_workflow", "review_gated_workflow"],
        source_modalities_supported=["workspace_repo", "remote_repo", "unknown"],
        evidence_basis_supported=["repo_structure", "repo_relationships", "repo_execution_surface"],
        required_evidence_types=["repo.inventory", "repo.structure", "repo.evidence_bundle"],
        optional_evidence_types=["repo.source", "repo.workspace_root", "repo.relationships", "repo.execution_surface", "report.quality"],
        final_output_evidence_type="report.architecture.validated",
        default_output_artifacts=["repository_evidence_bundle.json", "architecture_report.md", "architecture_report_quality.json"],
        workflow_steps=[
            CapabilityIntentWorkflowStepTemplate(
                step_id="inventory_repository",
                node_name="repo_inventory",
                logical_step_kind="ingest",
                capability_id="repository_inventory",
                purpose="Generate a bounded repository inventory as the initial evidence pass.",
                input_intent="A repository root and bounded scan controls.",
                output_intent="Repository inventory with files, configs, ecosystems, and entrypoint hints.",
                produces_evidence_types=["repo.inventory"],
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="extract_source_structure",
                node_name="repo_structure",
                logical_step_kind="transform",
                capability_id="source_structure_extract",
                purpose="Extract bounded module, symbol, import, and framework structure.",
                input_intent="Repository inventory and optional focus constraints.",
                output_intent="Structured source/module evidence for downstream mapping.",
                consumes_evidence_types=["repo.inventory"],
                produces_evidence_types=["repo.structure"],
                default_args={"source_step": "inventory_repository"},
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="map_repository_relationships",
                node_name="repo_relationships",
                logical_step_kind="transform",
                capability_id="repository_relationship_map",
                purpose="Map dependency and boundary hints across repository modules.",
                input_intent="Structured source extraction results.",
                output_intent="Dependency edges, hotspots, and module clusters.",
                consumes_evidence_types=["repo.structure"],
                produces_evidence_types=["repo.relationships"],
                default_args={"source_step": "extract_source_structure"},
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="inspect_execution_surfaces",
                node_name="repo_execution",
                logical_step_kind="transform",
                capability_id="repository_execution_surface_inspect",
                purpose="Inspect build, runtime, task, and config surfaces in the repository.",
                input_intent="Repository inventory and extracted source structure.",
                output_intent="Execution surface evidence and integration/config hints.",
                consumes_evidence_types=["repo.inventory", "repo.structure"],
                produces_evidence_types=["repo.execution_surface"],
                default_args={
                    "inventory_step": "inventory_repository",
                    "structure_step": "extract_source_structure",
                },
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="bundle_repository_evidence",
                node_name="repo_evidence",
                logical_step_kind="persist",
                capability_id="repository_evidence_bundle",
                purpose="Bundle the bounded repository evidence into a reusable analysis packet.",
                input_intent="Repository inventory, structure, relationship, and execution-surface evidence.",
                output_intent="Repository evidence bundle with file refs, confidence markers, and unknowns.",
                consumes_evidence_types=["repo.inventory", "repo.structure", "repo.relationships", "repo.execution_surface"],
                produces_evidence_types=["repo.evidence_bundle"],
                artifact_expectations=["repository_evidence_bundle.json"],
                default_args={
                    "inventory_step": "inventory_repository",
                    "structure_step": "extract_source_structure",
                    "relationship_step": "map_repository_relationships",
                    "execution_surface_step": "inspect_execution_surfaces",
                },
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="draft_architecture_report_brief",
                node_name="report_brief",
                logical_step_kind="transform",
                capability_id="architecture_report_brief",
                purpose="Prepare a grounded briefing packet for architecture report synthesis.",
                input_intent="Repository evidence bundle, objective, and audience.",
                output_intent="Architecture-report brief with coverage notes, focus points, and citations.",
                consumes_evidence_types=["repo.evidence_bundle"],
                produces_evidence_types=["report.architecture.brief"],
                default_args={"source_step": "bundle_repository_evidence", "source_field": "evidence_bundle"},
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="synthesize_architecture_report",
                node_name="report_synthesis",
                logical_step_kind="summarize",
                capability_id="architecture_report_synthesis_llm",
                purpose="Synthesize a grounded architecture draft from the repository brief.",
                input_intent="Architecture report brief plus report objective and audience.",
                output_intent="Structured architecture draft grounded in evidence.",
                consumes_evidence_types=["report.architecture.brief"],
                produces_evidence_types=["report.architecture.draft"],
                default_args={
                    "source_step": "draft_architecture_report_brief",
                    "source_field": "report_brief",
                    "task_prompt": (
                        "You are generating a repository architecture report. Use only the provided report brief "
                        "and previous outputs. Return a single JSON object with keys: overview, subsystem_map, "
                        "execution_surfaces, dependencies, risks_unknowns, next_inspection_points, citations."
                    ),
                    "expected_output": {
                        "overview": "string",
                        "subsystem_map": ["string"],
                        "execution_surfaces": ["string"],
                        "dependencies": ["string"],
                        "risks_unknowns": ["string"],
                        "next_inspection_points": ["string"],
                        "citations": ["string"],
                    },
                },
                human_review_required=True,
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="render_architecture_report",
                node_name="report_render",
                logical_step_kind="persist",
                capability_id="architecture_report_render",
                purpose="Render the architecture draft into delivery-ready markdown.",
                input_intent="Structured architecture draft and coverage/citation notes.",
                output_intent="Rendered markdown architecture report.",
                consumes_evidence_types=["report.architecture.draft"],
                produces_evidence_types=["report.architecture.rendered"],
                artifact_expectations=["architecture_report.md"],
                default_args={"source_step": "synthesize_architecture_report"},
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="validate_architecture_report",
                node_name="report_quality",
                logical_step_kind="validate",
                capability_id="architecture_report_quality_gate",
                purpose="Check the rendered architecture report for required sections, citations, and coverage notes.",
                input_intent="Rendered report plus upstream brief.",
                output_intent="Architecture report quality result with review guidance.",
                consumes_evidence_types=["report.architecture.rendered", "report.architecture.brief"],
                produces_evidence_types=["report.quality", "report.architecture.validated"],
                artifact_expectations=["architecture_report_quality.json"],
                default_args={
                    "report_step": "render_architecture_report",
                    "brief_step": "draft_architecture_report_brief",
                },
                human_review_required=True,
            ),
            CapabilityIntentWorkflowStepTemplate(
                step_id="write_architecture_report_artifact",
                node_name="report_delivery",
                logical_step_kind="persist",
                capability_id="write_artifact",
                purpose="Persist the validated architecture report as the final markdown deliverable.",
                input_intent="Validated architecture report payload plus artifact metadata.",
                output_intent="Saved markdown architecture report artifact.",
                consumes_evidence_types=["report.architecture.validated"],
                produces_evidence_types=["artifact.saved"],
                artifact_expectations=["architecture_report.md"],
                default_args={
                    "artifact_filename": "architecture_report.md",
                    "artifact_format": "markdown",
                    "source_step": "validate_architecture_report",
                    "source_field": "report_markdown",
                },
            ),
        ],
    ),
    CapabilityDeliverableIntentDefinition(
        intent_id="repository_onboarding_brief",
        name="Repository Onboarding Brief",
        description="Summarize a repository's purpose, key modules, and runtime/build surfaces for onboarding.",
        domain="repository_analysis",
        keywords=["onboarding", "repository", "repo", "codebase", "brief", "orientation"],
        deliverable_forms=["briefing"],
        analysis_frames=["summary"],
        decision_postures=["descriptive"],
        target_objects=["repository"],
        primary_objectives=["analyze", "synthesize", "deliver"],
        workflow_shapes=["bounded_workflow"],
        source_modalities_supported=["workspace_repo", "remote_repo", "unknown"],
        evidence_basis_supported=["repo_structure", "repo_execution_surface"],
        required_evidence_types=["repo.inventory", "repo.structure", "repo.evidence_bundle"],
        optional_evidence_types=["repo.execution_surface"],
        final_output_evidence_type="report.onboarding.final",
    ),
    CapabilityDeliverableIntentDefinition(
        intent_id="repository_audit_summary",
        name="Repository Audit Summary",
        description="Summarize architectural risks, hotspots, and execution/config surfaces for bounded audit review.",
        domain="repository_analysis",
        keywords=["audit", "repository", "repo", "risk", "hotspot", "review"],
        deliverable_forms=["report", "briefing"],
        analysis_frames=["audit", "assessment"],
        decision_postures=["evaluative", "recommendation"],
        target_objects=["repository"],
        primary_objectives=["analyze", "validate", "deliver"],
        workflow_shapes=["review_gated_workflow"],
        source_modalities_supported=["workspace_repo", "remote_repo", "unknown"],
        evidence_basis_supported=["repo_structure", "repo_relationships", "repo_execution_surface"],
        required_evidence_types=["repo.inventory", "repo.structure", "repo.relationships", "repo.evidence_bundle"],
        optional_evidence_types=["repo.execution_surface"],
        final_output_evidence_type="report.audit.final",
    ),
]


class CapabilityRegistryService:
    def __init__(self, db: Session | None = None, *, project_id: str | None = None) -> None:
        self.db = db
        self.project_id = project_id
        self._step_kinds = {item.step_kind: item.model_copy(deep=True) for item in LOGICAL_STEP_KINDS}
        self._capabilities = {item.capability_id: item.model_copy(deep=True) for item in CAPABILITIES}
        self._bindings = {item.step_kind: item.model_copy(deep=True) for item in CAPABILITY_BINDINGS}
        self._deliverable_intents = {item.intent_id: item.model_copy(deep=True) for item in DELIVERABLE_INTENTS}
        self._capability_sources: dict[str, tuple[str, str | None]] = {
            item.capability_id: ("static_registry", item.capability_id) for item in CAPABILITIES
        }
        self._reusable_candidates: dict[str, CapabilityReusableCandidateResponse] = {}
        self._active_internal_candidates: dict[str, dict[str, object]] = {}
        self._active_internal_aliases: dict[str, str] = {}
        if db is not None and project_id is not None:
            from app.services.capabilities.materialization_service import CapabilityMaterializationService
            from app.services.capabilities.lifecycle_service import CapabilityLifecycleService
            from app.db.repositories.capability_request_repository import CapabilityRequestRepository

            for candidate in CapabilityMaterializationService(db).list_reusable_candidates(project_id):
                if not candidate.reusable:
                    continue
                capability = self._capability_for_reusable_candidate(candidate)
                self._capabilities[capability.capability_id] = capability
                self._capability_sources[capability.capability_id] = ("materialization", candidate.source_materialization_id)
                self._reusable_candidates[capability.capability_id] = candidate
                for step_kind in candidate.supported_step_kinds:
                    binding = self._bindings.get(step_kind)
                    if binding is None:
                        self._bindings[step_kind] = CapabilityBindingDefinition(
                            step_kind=step_kind,
                            capability_ids=[capability.capability_id],
                            preferred_capability_ids=[],
                            allowed_contexts=list(candidate.enabled_contexts),
                            notes=["Derived from reviewed runtime-materialized internal capability."],
                        )
                        continue
                    if capability.capability_id not in binding.capability_ids:
                        binding.capability_ids.append(capability.capability_id)
            lifecycle_service = CapabilityLifecycleService(db)
            request_repository = CapabilityRequestRepository(db)
            for entry in lifecycle_service.repository.list_active_by_project(project_id):
                if entry.request_kind_origin not in {"generated_internal", "template_backed_internal"}:
                    continue
                request = request_repository.get_by_id(entry.source_request_id)
                alias_ids = [
                    value
                    for value in {
                        entry.capability_id,
                        entry.linked_static_registry_capability_id,
                        request.requested_capability_name_or_label if request is not None else None,
                    }
                    if value
                ]
                self._active_internal_candidates[entry.capability_id] = {
                    "capability_id": entry.capability_id,
                    "source_ref": entry.id,
                    "request_kind_origin": entry.request_kind_origin,
                    "supported_step_kinds": list(entry.supported_step_kinds_json or []),
                    "enabled_contexts": list(entry.enabled_contexts_json or []),
                    "linked_static_registry_capability_id": entry.linked_static_registry_capability_id,
                    "alias_ids": alias_ids,
                }
                for alias in alias_ids:
                    self._active_internal_aliases[alias] = entry.capability_id
                # Register the active entry as a proper CapabilityDefinition
                # so list_capabilities() and get_capability() can find it.
                cap_def = lifecycle_service._to_capability_definition(entry)
                if cap_def is not None and cap_def.capability_id not in self._capabilities:
                    self._capabilities[cap_def.capability_id] = cap_def
                    # Also register in relevant step kind bindings
                    for sk in cap_def.supported_step_kinds:
                        binding = self._bindings.get(sk)
                        if binding is not None and cap_def.capability_id not in binding.capability_ids:
                            binding.capability_ids.append(cap_def.capability_id)
        self._validate_registry()

    def list_step_kinds(self) -> list[LogicalStepKindDefinition]:
        return list(self._step_kinds.values())

    def list_capabilities(
        self,
        *,
        context: CapabilityContext | None = None,
        include_disabled: bool = False,
    ) -> list[CapabilityDefinition]:
        capabilities = list(self._capabilities.values())
        if not include_disabled:
            capabilities = [item for item in capabilities if item.enabled]
        if context is not None:
            capabilities = [item for item in capabilities if context in item.allowed_contexts]
        return capabilities

    def list_bindings(self) -> list[CapabilityBindingDefinition]:
        return list(self._bindings.values())

    def get_step_kind(self, step_kind: str) -> LogicalStepKindDefinition | None:
        return self._step_kinds.get(step_kind)

    def get_capability(self, capability_id: str) -> CapabilityDefinition | None:
        return self._capabilities.get(capability_id)

    def get_capability_source(self, capability_id: str) -> tuple[str, str | None]:
        return self._capability_sources.get(capability_id, ("unbound", None))

    def list_reusable_candidates(self) -> list[CapabilityReusableCandidateResponse]:
        return list(self._reusable_candidates.values())

    def snapshot(
        self,
        *,
        context: CapabilityContext | None = None,
        include_disabled: bool = False,
    ) -> CapabilityRegistrySnapshot:
        capabilities = self.list_capabilities(context=context, include_disabled=include_disabled)
        return CapabilityRegistrySnapshot(
            logical_step_kinds=self.list_step_kinds(),
            capabilities=capabilities,
            bindings=self.list_bindings(),
            deliverable_intents=self.list_deliverable_intents(),
            planner_capability_projections=[
                self.build_planner_capability_projection(item.capability_id)
                for item in capabilities
            ],
        )

    def list_planner_capability_projections(
        self,
        *,
        context: CapabilityContext | None = None,
        include_disabled: bool = False,
    ) -> list[PlannerCapabilityProjection]:
        return [
            self.build_planner_capability_projection(item.capability_id)
            for item in self.list_capabilities(context=context, include_disabled=include_disabled)
        ]

    def build_planner_capability_projection(self, capability_id: str) -> PlannerCapabilityProjection:
        capability = self.get_capability(capability_id)
        if capability is None:
            raise ValueError(f"Unknown capability_id: {capability_id}")
        return PlannerCapabilityProjection(
            capability_id=capability.capability_id,
            capability_name=capability.name,
            node_semantic_types=self._planner_node_semantic_types(capability),
            admissible_execution_regimes=self._planner_admissible_regimes(capability),
            input_ports=self._planner_input_ports(capability),
            output_ports=self._planner_output_ports(capability),
            side_effect_class=self._planner_side_effect_class(capability),
            observability_support=self._planner_observability_support(capability),
            testability_support=self._planner_testability_support(capability),
            runtime_constraints=list(capability.runtime_requirements or []),
            fallback_capability_ids=list(capability.preferred_downstream_capability_ids or []),
            requires_review=capability.requires_review,
            requires_confirmation=capability.requires_confirmation,
            planning_visibility=capability.planning_visibility,
            projection_notes=self._planner_projection_notes(capability),
            projection_metadata={
                "semantic_role": capability.semantic_role,
                "supported_step_kinds": list(capability.supported_step_kinds or []),
                "deliverable_intents": list(capability.deliverable_intents or []),
            },
        )

    def _planner_node_semantic_types(self, capability: CapabilityDefinition) -> list[str]:
        role = capability.semantic_role
        if role == "evidence_source":
            return ["acquire"]
        if role in {"evidence_transform", "evidence_bundle", "synthesis", "render"}:
            return ["transform"]
        if role == "quality_gate":
            return ["verify"]
        if role == "review_gate":
            return ["human_gate"]
        if role == "delivery":
            return ["act"]
        node_types: list[str] = []
        for step_kind in capability.supported_step_kinds:
            if step_kind == "ingest":
                node_types.append("acquire")
            elif step_kind in {"transform", "summarize", "rank"}:
                node_types.append("transform")
            elif step_kind == "validate":
                node_types.append("verify")
            elif step_kind in {"notify", "persist"}:
                node_types.append("act")
            elif step_kind == "review":
                node_types.append("human_gate")
        return list(dict.fromkeys(node_types or ["transform"]))

    def _planner_admissible_regimes(self, capability: CapabilityDefinition) -> list[str]:
        regimes: list[str] = []
        if capability.capability_type == "deterministic":
            regimes.append("deterministic")
        if capability.execution_kind in {"network_fetch", "web_search", "browser_interaction"} or "network_access" in capability.runtime_requirements:
            regimes.append("evidence_seeking")
        if capability.runtime_adapter == "computer_use" or capability.capability_id == "browser_interaction":
            regimes.append("gui_augmented")
        if capability.requires_review or capability.requires_confirmation or capability.semantic_role == "review_gate":
            regimes.append("human_gated")
        if capability.semantic_role in {"evidence_source", "evidence_transform", "evidence_bundle", "synthesis", "render", "quality_gate", "delivery"}:
            regimes.append("workflow_build")
        if not regimes:
            regimes.append("analysis_only")
        return list(dict.fromkeys(regimes))

    def _planner_input_ports(self, capability: CapabilityDefinition) -> list[PlannerCapabilityPortProjection]:
        return [
            PlannerCapabilityPortProjection(
                port_name="primary_input",
                schema_summary=capability.input_contract_summary,
                semantic_role=capability.semantic_role or "input",
                required=True,
            )
        ]

    def _planner_output_ports(self, capability: CapabilityDefinition) -> list[PlannerCapabilityPortProjection]:
        outputs = [
            PlannerCapabilityPortProjection(
                port_name="primary_output",
                schema_summary=capability.output_contract_summary,
                semantic_role=capability.semantic_role or "output",
                required=True,
            )
        ]
        for evidence_type in capability.produces_evidence_types:
            outputs.append(
                PlannerCapabilityPortProjection(
                    port_name=evidence_type,
                    schema_summary=f"Produces evidence type {evidence_type}.",
                    semantic_role="evidence",
                    required=False,
                )
            )
        return outputs

    def _planner_side_effect_class(self, capability: CapabilityDefinition) -> str:
        if capability.semantic_role == "review_gate":
            return "human_decision"
        if capability.execution_kind in {"artifact_writer", "filesystem", "terminal_command"} or "workspace_access" in capability.runtime_requirements:
            return "filesystem_write" if capability.semantic_role == "delivery" else "external_read"
        if capability.execution_kind in {"network_fetch", "web_search"} or "network_access" in capability.runtime_requirements:
            return "external_read"
        if capability.runtime_adapter == "computer_use" or capability.capability_id == "browser_interaction":
            return "environment_mutation"
        if capability.capability_type == "notification":
            return "communication"
        if capability.semantic_role == "delivery":
            return "external_write"
        return "none"

    def _planner_observability_support(self, capability: CapabilityDefinition) -> list[str]:
        support = ["execution_trace", "output_snapshot"]
        if capability.produces_evidence_types:
            support.append("emitted_evidence")
        if capability.runtime_adapter == "computer_use" or capability.capability_id == "browser_interaction":
            support.extend(["screen_before", "screen_after", "target_locator_info"])
        return list(dict.fromkeys(support))

    def _planner_testability_support(self, capability: CapabilityDefinition) -> list[str]:
        support = ["expected_output_schema", "mockable_capability_binding"]
        if capability.capability_type == "deterministic":
            support.append("deterministic_check_strategy")
        if capability.semantic_role in {"evidence_transform", "synthesis", "quality_gate"}:
            support.append("fixture_inputs")
        return list(dict.fromkeys(support))

    def _planner_projection_notes(self, capability: CapabilityDefinition) -> list[str]:
        notes: list[str] = []
        if capability.requires_review:
            notes.append("Projected as requiring review in at least one admissible execution regime.")
        if capability.requires_confirmation:
            notes.append("Projected with confirmation-sensitive execution semantics.")
        if capability.blocked_by_default:
            notes.append("Capability remains blocked by default and may require explicit operator action.")
        return notes

    def list_deliverable_intents(self) -> list[CapabilityDeliverableIntentDefinition]:
        return [item.model_copy(deep=True) for item in self._deliverable_intents.values()]

    def get_deliverable_intent(self, intent_id: str) -> CapabilityDeliverableIntentDefinition | None:
        intent = self._deliverable_intents.get(intent_id)
        return intent.model_copy(deep=True) if intent is not None else None

    def infer_deliverable_intent_candidates(
        self,
        *,
        normalized_requirement: dict[str, object],
        semantic_interpretation: dict[str, object] | None = None,
        limit: int = 3,
    ) -> list[CapabilityDeliverableIntentCandidateResponse]:
        haystack = " ".join(
            str(part).strip().lower()
            for part in (
                normalized_requirement.get("title"),
                normalized_requirement.get("summary"),
                normalized_requirement.get("goal"),
                normalized_requirement.get("output_description"),
                normalized_requirement.get("domain"),
            )
            if str(part or "").strip()
        )
        candidates: list[CapabilityDeliverableIntentCandidateResponse] = []
        semantic = self._normalize_semantic_interpretation(semantic_interpretation)
        for intent in self._deliverable_intents.values():
            matched_keywords = [keyword for keyword in intent.keywords if self._keyword_matches(keyword, haystack)]
            semantic_target = str(semantic.get("target_object") or "").strip()
            semantic_contract = str(semantic.get("deliverable_contract") or "").strip()
            if intent.domain == "repository_analysis":
                repository_markers = {
                    "repository",
                    "repo",
                    "codebase",
                    "architectural",
                    "architecture",
                    "system design",
                    "架构",
                    "架构报告",
                    "仓库",
                    "代码仓库",
                    "软件仓库",
                    "整体架构",
                }
                repo_keyword_match = any(keyword.lower() in repository_markers for keyword in matched_keywords)
                if semantic_target != "repository" and semantic_contract != intent.intent_id and not repo_keyword_match:
                    matched_keywords = []
            if intent.intent_id == "research_briefing":
                research_markers = {"brief", "briefing", "research briefing"}
                research_keyword_match = any(keyword.lower() in research_markers for keyword in matched_keywords)
                if semantic_contract != intent.intent_id and semantic_target not in {"web_resource", "local_workspace"} and not research_keyword_match:
                    matched_keywords = []
            semantic_score, semantic_reasons = self._semantic_intent_score(
                intent=intent,
                semantic_interpretation=semantic,
            )
            keyword_score = 0
            if matched_keywords:
                keyword_score = min(30, 10 + (len(matched_keywords) * 5))
            if semantic_score <= 0 and keyword_score <= 0:
                continue
            score = min(100, semantic_score + keyword_score)
            proposal = self.build_typed_workflow_proposal(
                intent.intent_id,
                context="blueprint_planning",
                normalized_requirement=normalized_requirement,
                semantic_interpretation=semantic,
            )
            reason_parts = list(semantic_reasons)
            if matched_keywords:
                reason_parts.append(f"Matched lexical cues: {', '.join(matched_keywords[:5])}.")
            candidates.append(
                CapabilityDeliverableIntentCandidateResponse(
                    intent_id=intent.intent_id,
                    name=intent.name,
                    score=score,
                    reason=" ".join(reason_parts) if reason_parts else "Matched semantic intent requirements.",
                    matched_keywords=matched_keywords[:8],
                    workflow_available=proposal is not None and bool(proposal.steps),
                )
            )
        candidates.sort(key=lambda item: (-item.score, item.intent_id))
        return candidates[:limit]

    def _keyword_matches(self, keyword: str, haystack: str) -> bool:
        needle = keyword.strip().lower()
        if not needle:
            return False
        if re.search(r"[\u4e00-\u9fff]", needle):
            return needle in haystack
        if " " in needle or "-" in needle or "/" in needle:
            return needle in haystack
        return re.search(rf"\b{re.escape(needle)}\b", haystack) is not None

    def _normalize_semantic_interpretation(
        self,
        semantic_interpretation: dict[str, object] | None,
    ) -> dict[str, object]:
        if not isinstance(semantic_interpretation, dict):
            return {}
        try:
            semantic = SemanticCore.model_validate(semantic_interpretation)
        except Exception:
            return {}
        return semantic.trace_summary()

    def _semantic_intent_score(
        self,
        *,
        intent: CapabilityDeliverableIntentDefinition,
        semantic_interpretation: dict[str, object],
    ) -> tuple[int, list[str]]:
        if not semantic_interpretation:
            return 0, []
        score = 0
        reasons: list[str] = []
        anchored = False
        deliverable_contract = str(semantic_interpretation.get("deliverable_contract") or "").strip()
        if deliverable_contract and deliverable_contract == intent.intent_id:
            score += 65
            reasons.append(f"Semantic deliverable contract matched '{intent.intent_id}'.")
            anchored = True
        deliverable_form = str(semantic_interpretation.get("deliverable_form") or "").strip()
        target_object = str(semantic_interpretation.get("target_object") or "").strip()
        if (
            intent.intent_id == "research_briefing"
            and deliverable_contract != intent.intent_id
            and not (deliverable_form == "briefing" or target_object == "web_resource")
        ):
            return 0, []
        if deliverable_form and deliverable_form in intent.deliverable_forms:
            score += 12
            reasons.append(f"Deliverable form matched '{deliverable_form}'.")
        analysis_frame = str(semantic_interpretation.get("analysis_frame") or "").strip()
        if analysis_frame and analysis_frame in intent.analysis_frames:
            score += 22
            reasons.append(f"Analysis frame matched '{analysis_frame}'.")
            anchored = True
        decision_posture = str(semantic_interpretation.get("decision_posture") or "").strip()
        if decision_posture and decision_posture in intent.decision_postures:
            score += 10
            reasons.append(f"Decision posture matched '{decision_posture}'.")
        if target_object and target_object in intent.target_objects:
            score += 18
            reasons.append(f"Semantic target object matched '{target_object}'.")
        evidence_basis = {
            str(item).strip()
            for item in (semantic_interpretation.get("evidence_basis") or [])
            if str(item).strip()
        }
        if evidence_basis:
            overlap_basis = evidence_basis.intersection(set(intent.evidence_basis_supported))
            if overlap_basis:
                score += min(16, len(overlap_basis) * 6)
                reasons.append(f"Evidence basis overlapped on {', '.join(sorted(overlap_basis)[:4])}.")
        evidence_requirements = {
            str(item).strip()
            for item in (semantic_interpretation.get("evidence_requirements") or [])
            if str(item).strip()
        }
        if evidence_requirements:
            overlap = evidence_requirements.intersection(set(intent.required_evidence_types))
            if overlap:
                score += min(20, len(overlap) * 6)
                reasons.append(f"Evidence requirements overlapped on {', '.join(sorted(overlap)[:4])}.")
        if not anchored:
            return 0, []
        primary_objective = str(semantic_interpretation.get("primary_objective") or "").strip()
        if primary_objective and primary_objective != "deliver" and primary_objective in intent.primary_objectives:
            score += 12
            reasons.append(f"Semantic objective matched '{primary_objective}'.")
        workflow_shape = str(semantic_interpretation.get("workflow_shape") or "").strip()
        if workflow_shape and workflow_shape in intent.workflow_shapes:
            score += 10
            reasons.append(f"Workflow shape matched '{workflow_shape}'.")
        source_modality = str(semantic_interpretation.get("source_modality") or "").strip()
        if source_modality and source_modality in intent.source_modalities_supported:
            score += 8
            reasons.append(f"Source modality matched '{source_modality}'.")
        return score, reasons

    def build_typed_workflow_proposal(
        self,
        intent_id: str,
        *,
        context: CapabilityContext,
        normalized_requirement: dict[str, object] | None = None,
        semantic_interpretation: dict[str, object] | None = None,
    ) -> CapabilityTypedWorkflowProposalResponse | None:
        intent = self._deliverable_intents.get(intent_id)
        if intent is None:
            return None
        workflow_steps = self._workflow_templates_for_intent(
            intent=intent,
            normalized_requirement=normalized_requirement or {},
            semantic_interpretation=self._normalize_semantic_interpretation(semantic_interpretation),
        )
        steps: list[CapabilityTypedWorkflowStepResponse] = []
        missing_capabilities: list[str] = []
        for index, template in enumerate(workflow_steps, start=1):
            capability = self.get_capability(template.capability_id)
            if capability is None:
                missing_capabilities.append(template.capability_id)
                continue
            validation = self.validate_capability_usage(
                capability_id=template.capability_id,
                context=context,
                step_kind=template.logical_step_kind,
            )
            if not validation.valid:
                missing_capabilities.append(template.capability_id)
                continue
            selected_reusable_capability_id, recommended_reusable_capability_ids = self._planning_reusable_preferences(
                template=template,
                capability=capability,
                intent_id=intent.intent_id,
            )
            steps.append(
                CapabilityTypedWorkflowStepResponse(
                    step_id=template.step_id,
                    order=index,
                    node_name=template.node_name,
                    logical_step_kind=template.logical_step_kind,
                    capability_id=template.capability_id,
                    semantic_role=template.semantic_role or capability.semantic_role,
                    purpose=template.purpose or (capability.semantic_purpose or capability.description),
                    input_intent=template.input_intent,
                    output_intent=template.output_intent,
                    consumes_evidence_types=list(template.consumes_evidence_types or capability.consumes_evidence_types),
                    produces_evidence_types=list(template.produces_evidence_types or capability.produces_evidence_types),
                    artifact_expectations=list(template.artifact_expectations),
                    default_args=dict(template.default_args),
                    human_review_required=template.human_review_required or capability.requires_review,
                    selected_reusable_capability_id=selected_reusable_capability_id,
                    recommended_reusable_capability_ids=recommended_reusable_capability_ids,
                )
            )
        if missing_capabilities:
            return None
        nodes, edges, graph_validation = self._build_planning_graph(
            intent=intent,
            steps=steps,
            normalized_requirement=normalized_requirement or {},
        )
        return CapabilityTypedWorkflowProposalResponse(
            intent_id=intent.intent_id,
            intent_name=intent.name,
            summary=intent.description,
            confidence=85 if steps else 0,
            final_output_evidence_type=intent.final_output_evidence_type,
            default_output_artifacts=list(intent.default_output_artifacts),
            steps=steps,
            nodes=nodes,
            edges=edges,
            validation=graph_validation,
        )

    def build_planning_graph_for_steps(
        self,
        *,
        intent_id: str | None,
        steps: list[CapabilityTypedWorkflowStepResponse],
        normalized_requirement: dict[str, object] | None = None,
    ) -> tuple[
        list[CapabilityPlanningGraphNodeResponse],
        list[CapabilityPlanningGraphEdgeResponse],
        CapabilityPlanningGraphValidationResponse,
    ]:
        if intent_id is not None:
            intent = self._deliverable_intents.get(intent_id)
        else:
            intent = CapabilityDeliverableIntentDefinition(
                intent_id="ad_hoc_workflow",
                name="Ad Hoc Workflow",
                description="Ad hoc workflow graph built from bounded planning semantics.",
                final_output_evidence_type=None,
            )
        return self._build_planning_graph(
            intent=intent,
            steps=steps,
            normalized_requirement=normalized_requirement or {},
        )

    def _planning_reusable_preferences(
        self,
        *,
        template: CapabilityIntentWorkflowStepTemplate,
        capability: CapabilityDefinition,
        intent_id: str,
    ) -> tuple[str | None, list[str]]:
        if capability.planning_visibility != "reusable_allowed":
            return None, []
        eligible: list[CapabilityReusableCandidateResponse] = []
        for candidate in self._reusable_candidates.values():
            if not self._reusable_candidate_is_planning_eligible(candidate):
                continue
            if template.logical_step_kind not in candidate.supported_step_kinds:
                continue
            candidate_capability = self.get_capability(candidate.capability_id)
            if candidate_capability is None:
                continue
            if intent_id not in candidate_capability.deliverable_intents:
                continue
            expected_role = template.semantic_role or capability.semantic_role
            if expected_role and candidate_capability.semantic_role != expected_role:
                continue
            expected_consumes = set(template.consumes_evidence_types or capability.consumes_evidence_types)
            expected_produces = set(template.produces_evidence_types or capability.produces_evidence_types)
            candidate_consumes = set(candidate_capability.consumes_evidence_types)
            candidate_produces = set(candidate_capability.produces_evidence_types)
            if expected_consumes and not expected_consumes.issubset(candidate_consumes):
                continue
            if expected_produces and not expected_produces.issubset(candidate_produces):
                continue
            eligible.append(candidate)
        if not eligible:
            return None, []
        eligible.sort(
            key=lambda item: (
                0 if item.linked_static_registry_capability_id == template.capability_id else 1,
                -item.selection_priority,
                item.capability_id,
            )
        )
        recommended = [item.capability_id for item in eligible[:3]]
        selected = next(
            (item.capability_id for item in eligible if item.linked_static_registry_capability_id == template.capability_id),
            None,
        )
        return selected, recommended

    def _reusable_candidate_is_planning_eligible(self, candidate: CapabilityReusableCandidateResponse) -> bool:
        if not candidate.reusable or not candidate.runtime_ready:
            return False
        static_capability_id = str(candidate.linked_static_registry_capability_id or "").strip()
        if not static_capability_id:
            return False
        static_capability = self._capabilities.get(static_capability_id)
        if static_capability is None:
            return False
        if not static_capability.semantic_role:
            return False
        if not static_capability.deliverable_intents:
            return False
        has_evidence_semantics = bool(
            static_capability.consumes_evidence_types or static_capability.produces_evidence_types
        )
        if not has_evidence_semantics:
            return False
        return True

    def _build_planning_graph(
        self,
        *,
        intent: CapabilityDeliverableIntentDefinition,
        steps: list[CapabilityTypedWorkflowStepResponse],
        normalized_requirement: dict[str, object],
    ) -> tuple[
        list[CapabilityPlanningGraphNodeResponse],
        list[CapabilityPlanningGraphEdgeResponse],
        CapabilityPlanningGraphValidationResponse,
    ]:
        ordered_steps = sorted(steps, key=lambda item: (item.order or 0, item.step_id))
        nodes = [
            CapabilityPlanningGraphNodeResponse(
                step_id=step.step_id,
                order=step.order,
                node_name=step.node_name,
                logical_step_kind=step.logical_step_kind,
                capability_id=step.capability_id,
                semantic_role=step.semantic_role,
                selected_reusable_capability_id=step.selected_reusable_capability_id,
                recommended_reusable_capability_ids=list(step.recommended_reusable_capability_ids),
                consumes_evidence_types=list(step.consumes_evidence_types),
                produces_evidence_types=list(step.produces_evidence_types),
                deliverable_intents=[intent.intent_id],
            )
            for step in ordered_steps
        ]
        node_index = {node.step_id: node for node in nodes}
        edges: list[CapabilityPlanningGraphEdgeResponse] = []
        issues: list[CapabilityPlanningGraphValidationIssueResponse] = []
        for step in ordered_steps:
            for evidence_type in self._unique(list(step.consumes_evidence_types)):
                producers = [
                    upstream
                    for upstream in ordered_steps
                    if upstream.order < step.order and evidence_type in upstream.produces_evidence_types
                ]
                if not producers:
                    issues.append(
                        CapabilityPlanningGraphValidationIssueResponse(
                            issue_kind="missing_dependency",
                            severity="error",
                            message=(
                                f"Step '{step.step_id}' consumes evidence '{evidence_type}' but no prior step produces it."
                            ),
                            step_id=step.step_id,
                            evidence_type=evidence_type,
                        )
                    )
                    continue
                if len(producers) > 1:
                    issues.append(
                        CapabilityPlanningGraphValidationIssueResponse(
                            issue_kind="ambiguous_dependency",
                            severity="error",
                            message=(
                                f"Step '{step.step_id}' consumes evidence '{evidence_type}' from multiple prior producers; "
                                "bounded planning requires one explicit producer per consumed evidence type."
                            ),
                            step_id=step.step_id,
                            evidence_type=evidence_type,
                        )
                    )
                    continue
                producer = producers[0]
                edges.append(
                    CapabilityPlanningGraphEdgeResponse(
                        from_step_id=producer.step_id,
                        to_step_id=step.step_id,
                        evidence_type=evidence_type,
                    )
                )

        incoming_by_step: dict[str, set[str]] = {}
        for edge in edges:
            incoming_by_step.setdefault(edge.to_step_id, set()).add(edge.from_step_id)
            producer = node_index.get(edge.from_step_id)
            consumer = node_index.get(edge.to_step_id)
            if producer is None or consumer is None:
                issues.append(
                    CapabilityPlanningGraphValidationIssueResponse(
                        issue_kind="missing_node",
                        severity="error",
                        message=f"Planning edge {edge.from_step_id} -> {edge.to_step_id} points to an unknown node.",
                    )
                )
                continue
            if edge.evidence_type not in producer.produces_evidence_types:
                issues.append(
                    CapabilityPlanningGraphValidationIssueResponse(
                        issue_kind="role_mismatch",
                        severity="error",
                        message=(
                            f"Planning edge {edge.from_step_id} -> {edge.to_step_id} uses evidence '{edge.evidence_type}' "
                            "that the source node does not declare."
                        ),
                        step_id=edge.to_step_id,
                        evidence_type=edge.evidence_type,
                    )
                )
            if edge.evidence_type not in consumer.consumes_evidence_types:
                issues.append(
                    CapabilityPlanningGraphValidationIssueResponse(
                        issue_kind="undeclared_dependency",
                        severity="error",
                        message=(
                            f"Planning edge {edge.from_step_id} -> {edge.to_step_id} uses evidence '{edge.evidence_type}' "
                            "outside the consumer's declared evidence dependencies."
                        ),
                        step_id=edge.to_step_id,
                        evidence_type=edge.evidence_type,
                    )
                )

        for step in ordered_steps:
            referenced_steps = self._referenced_step_ids(step.default_args)
            if not referenced_steps:
                continue
            declared_dependencies = incoming_by_step.get(step.step_id, set())
            for reference_step in sorted(referenced_steps):
                if reference_step not in declared_dependencies:
                    issues.append(
                        CapabilityPlanningGraphValidationIssueResponse(
                            issue_kind="undeclared_source_dependency",
                            severity="error",
                            message=(
                                f"Step '{step.step_id}' references upstream step '{reference_step}' in args without a matching "
                                "explicit evidence edge."
                            ),
                            step_id=step.step_id,
                        )
                    )

        cycle_detected = self._graph_has_cycle(nodes=nodes, edges=edges)
        if cycle_detected:
            issues.append(
                CapabilityPlanningGraphValidationIssueResponse(
                    issue_kind="cycle",
                    severity="error",
                    message="Planning graph contains a cycle; only DAG planning graphs are allowed.",
                )
            )

        if any(
            capability.quality_gate_relevance == "required" and intent.intent_id in capability.deliverable_intents
            for capability in self._capabilities.values()
        ) and not any(node.semantic_role == "quality_gate" for node in nodes):
            issues.append(
                CapabilityPlanningGraphValidationIssueResponse(
                    issue_kind="missing_quality_gate",
                    severity="error",
                    message=f"Deliverable intent '{intent.intent_id}' requires a quality-gate node before final delivery.",
                )
            )

        source_plan = (
            self.infer_repository_source_plan(normalized_requirement)
            if intent.domain == "repository_analysis"
            else {}
        )
        if source_plan.get("source_modality") in {"ambiguous_source", "unsupported_source"}:
            issues.append(
                CapabilityPlanningGraphValidationIssueResponse(
                    issue_kind="source_review_needed",
                    severity="warning",
                    message=str(source_plan.get("reason") or "Repository source modality requires review."),
                )
            )

        error_count = len([item for item in issues if item.severity == "error"])
        warning_count = len([item for item in issues if item.severity == "warning"])
        if error_count:
            validation = CapabilityPlanningGraphValidationResponse(
                valid=False,
                decision="rejected",
                summary=f"Rejected planning graph for deliverable intent '{intent.intent_id}' due to {error_count} graph validation error(s).",
                issues=issues,
                cycle_detected=cycle_detected,
            )
        elif warning_count:
            validation = CapabilityPlanningGraphValidationResponse(
                valid=True,
                decision="review_needed",
                summary=(
                    f"Planning graph for deliverable intent '{intent.intent_id}' is structurally valid but requires review for "
                    f"{warning_count} planning concern(s)."
                ),
                issues=issues,
                cycle_detected=False,
            )
        else:
            validation = CapabilityPlanningGraphValidationResponse(
                valid=True,
                decision="accepted",
                summary=f"Accepted explicit planning DAG for deliverable intent '{intent.intent_id}'.",
                issues=[],
                cycle_detected=False,
            )
        return nodes, edges, validation

    def _referenced_step_ids(self, args: dict[str, Any]) -> set[str]:
        references: set[str] = set()
        for key, value in args.items():
            if not isinstance(value, str) or not value.strip():
                continue
            normalized_key = str(key).strip().lower()
            if normalized_key == "source_step" or normalized_key.endswith("_step"):
                references.add(value.strip())
        return references

    def _graph_has_cycle(
        self,
        *,
        nodes: list[CapabilityPlanningGraphNodeResponse],
        edges: list[CapabilityPlanningGraphEdgeResponse],
    ) -> bool:
        adjacency: dict[str, set[str]] = {node.step_id: set() for node in nodes}
        indegree: dict[str, int] = {node.step_id: 0 for node in nodes}
        for edge in edges:
            if edge.from_step_id not in adjacency or edge.to_step_id not in indegree:
                return True
            if edge.to_step_id not in adjacency[edge.from_step_id]:
                adjacency[edge.from_step_id].add(edge.to_step_id)
                indegree[edge.to_step_id] += 1
        queue = [node_id for node_id, degree in indegree.items() if degree == 0]
        visited = 0
        while queue:
            current = queue.pop(0)
            visited += 1
            for child in adjacency.get(current, set()):
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        return visited != len(nodes)

    def infer_repository_source_plan(self, normalized_requirement: dict[str, object]) -> dict[str, Any]:
        texts = [
            str(normalized_requirement.get(key) or "").strip()
            for key in ("title", "summary", "goal", "input_description", "output_description")
        ]
        combined = "\n".join(item for item in texts if item)
        lowered = combined.lower()
        locator = self._first_repository_locator(combined)
        if locator:
            normalized_locator = locator.rstrip(").,]")
            lowered_locator = normalized_locator.lower()
            if lowered_locator.startswith("svn://"):
                return {
                    "source_modality": "unsupported_source",
                    "source_locator": normalized_locator,
                    "requires_acquisition_prefix": True,
                    "requires_checkout": True,
                    "allow_network": False,
                    "human_review_required": True,
                    "reason": "Repository source uses an unsupported VCS locator.",
                }
            if lowered_locator.endswith(REPOSITORY_ARCHIVE_SUFFIXES):
                return {
                    "source_modality": "archive_url",
                    "source_locator": normalized_locator,
                    "requires_acquisition_prefix": True,
                    "requires_checkout": True,
                    "allow_network": not lowered_locator.startswith("file://"),
                    "human_review_required": False,
                    "reason": "Repository source is an archive locator that must be materialized before analysis.",
                }
            if "github.com/" in lowered_locator:
                return {
                    "source_modality": "github_repo_url",
                    "source_locator": normalized_locator,
                    "requires_acquisition_prefix": True,
                    "requires_checkout": True,
                    "allow_network": True,
                    "human_review_required": False,
                    "reason": "Repository source is a GitHub URL that requires checkout before bounded inventory.",
                }
            if lowered_locator.startswith(("http://", "https://", "git://", "ssh://", "git@")) or lowered_locator.endswith(".git"):
                return {
                    "source_modality": "git_remote",
                    "source_locator": normalized_locator,
                    "requires_acquisition_prefix": True,
                    "requires_checkout": True,
                    "allow_network": not lowered_locator.startswith("file://"),
                    "human_review_required": False,
                    "reason": "Repository source is a remote git locator that requires checkout before bounded inventory.",
                }
            if lowered_locator.startswith("file://"):
                return {
                    "source_modality": "archive_url" if lowered_locator.endswith(REPOSITORY_ARCHIVE_SUFFIXES) else "git_remote",
                    "source_locator": normalized_locator,
                    "requires_acquisition_prefix": True,
                    "requires_checkout": True,
                    "allow_network": False,
                    "human_review_required": False,
                    "reason": "Repository source is a file-based locator that must be copied or extracted into the workspace before analysis.",
                }
            return {
                "source_modality": "unsupported_source",
                "source_locator": normalized_locator,
                "requires_acquisition_prefix": True,
                "requires_checkout": True,
                "allow_network": False,
                "human_review_required": True,
                "reason": "Repository locator could not be mapped onto the bounded checkout runtime.",
            }

        if any(marker in lowered for marker in ("inside the workspace", "workspace repo", "workspace path", "local repository", "local repo")):
            return {
                "source_modality": "local_workspace_repo",
                "source_locator": self._first_workspace_path(combined),
                "requires_acquisition_prefix": False,
                "requires_checkout": False,
                "allow_network": False,
                "human_review_required": False,
                "reason": "Requirement explicitly refers to a repository already available inside the workspace.",
            }

        workspace_path = self._first_workspace_path(combined)
        if workspace_path:
            return {
                "source_modality": "local_workspace_repo",
                "source_locator": workspace_path,
                "requires_acquisition_prefix": False,
                "requires_checkout": False,
                "allow_network": False,
                "human_review_required": False,
                "reason": "Requirement includes a workspace-relative repository path.",
            }

        if any(token in lowered for token in ("repository", "repo", "codebase")):
            return {
                "source_modality": "ambiguous_source",
                "source_locator": None,
                "requires_acquisition_prefix": True,
                "requires_checkout": True,
                "allow_network": False,
                "human_review_required": True,
                "reason": "Repository analysis intent is clear, but the repository source modality was not explicit.",
            }

        return {
            "source_modality": "unspecified",
            "source_locator": None,
            "requires_acquisition_prefix": False,
            "requires_checkout": False,
            "allow_network": False,
            "human_review_required": False,
            "reason": "No repository source modality was inferred from the requirement.",
        }

    def _workflow_templates_for_intent(
        self,
        *,
        intent: CapabilityDeliverableIntentDefinition,
        normalized_requirement: dict[str, object],
        semantic_interpretation: dict[str, object] | None = None,
    ) -> list[CapabilityIntentWorkflowStepTemplate]:
        workflow_steps = [step.model_copy(deep=True) for step in intent.workflow_steps]
        if intent.domain != "repository_analysis" or not workflow_steps:
            return workflow_steps

        source_plan = self.infer_repository_source_plan(normalized_requirement)
        semantic = semantic_interpretation or {}
        if source_plan.get("source_modality") == "unspecified":
            semantic_source = str(semantic.get("source_modality") or "").strip()
            if semantic_source == "workspace_repo":
                source_plan = {
                    **source_plan,
                    "source_modality": "local_workspace_repo",
                    "requires_acquisition_prefix": False,
                    "requires_checkout": False,
                }
            elif semantic_source == "remote_repo":
                source_plan = {
                    **source_plan,
                    "source_modality": "ambiguous_source",
                    "requires_acquisition_prefix": True,
                    "requires_checkout": True,
                    "human_review_required": True,
                    "reason": "Repository intent implies a remote source, but the concrete locator still requires review.",
                }
        first_inventory = next((step for step in workflow_steps if step.capability_id == "repository_inventory"), None)
        if first_inventory is None:
            return workflow_steps

        source_locator = str(source_plan.get("source_locator") or "").strip() or None
        if source_plan.get("requires_acquisition_prefix"):
            prefix = [
                CapabilityIntentWorkflowStepTemplate(
                    step_id="resolve_repository_source",
                    node_name="repo_source",
                    logical_step_kind="ingest",
                    capability_id="repository_source_resolve",
                    purpose="Classify and normalize the repository source before checkout or direct analysis.",
                    input_intent="Repository locator or repository-root hint from the requirement or run input.",
                    output_intent="Resolved repository source modality, locator normalization, and checkout requirement.",
                    produces_evidence_types=["repo.source"],
                    default_args={"repo_locator": source_locator} if source_locator else {},
                    human_review_required=bool(source_plan.get("human_review_required")),
                ),
                CapabilityIntentWorkflowStepTemplate(
                    step_id="checkout_repository_source",
                    node_name="repo_checkout",
                    logical_step_kind="ingest",
                    capability_id="repository_checkout",
                    purpose="Materialize the resolved repository source into a bounded workspace root before inventory.",
                    input_intent="Resolved repository source plus bounded workspace destination and checkout policy.",
                    output_intent="Workspace root ready for bounded repository inventory.",
                    consumes_evidence_types=["repo.source"],
                    produces_evidence_types=["repo.workspace_root"],
                    default_args={
                        "source_step": "resolve_repository_source",
                        **({"allow_network": True} if source_plan.get("allow_network") else {}),
                    },
                    human_review_required=bool(source_plan.get("human_review_required")),
                ),
            ]
            first_inventory.consumes_evidence_types = ["repo.workspace_root"]
            first_inventory.default_args = {**dict(first_inventory.default_args), "source_step": "checkout_repository_source"}
            return [*prefix, *workflow_steps]

        if source_locator:
            first_inventory.default_args = {**dict(first_inventory.default_args), "repo_root": source_locator}
        return workflow_steps

    def _first_repository_locator(self, text: str) -> str | None:
        url_match = REPOSITORY_SOURCE_URL_RE.search(text)
        if url_match:
            return str(url_match.group("url")).strip()
        return None

    def _first_workspace_path(self, text: str) -> str | None:
        for match in REPOSITORY_PATH_RE.finditer(text):
            candidate = str(match.group("path")).strip().rstrip(").,]")
            pure_path = PurePosixPath(candidate)
            if candidate.lower().endswith(REPOSITORY_ARCHIVE_SUFFIXES):
                continue
            if len(pure_path.parts) >= 1:
                return candidate
        return None

    def list_capabilities_for_step_kind(
        self,
        step_kind: str,
        *,
        context: CapabilityContext | None = None,
        include_disabled: bool = False,
        allowed_capability_types: list[CapabilityType] | None = None,
    ) -> list[CapabilityDefinition]:
        binding = self._bindings.get(step_kind)
        if binding is None:
            return []
        capabilities = [
            self._capabilities[capability_id]
            for capability_id in binding.capability_ids
            if capability_id in self._capabilities
        ]
        if not include_disabled:
            capabilities = [item for item in capabilities if item.enabled]
        if context is not None:
            capabilities = [
                item
                for item in capabilities
                if context in item.allowed_contexts and context in binding.allowed_contexts
            ]
        if allowed_capability_types:
            allowed = set(allowed_capability_types)
            capabilities = [item for item in capabilities if item.capability_type in allowed]
        return capabilities

    def resolve_reusable_selection_candidate(
        self,
        *,
        step_kind: str,
        context: CapabilityContext,
        explicit_hints: list[str] | None = None,
        preferred_execution_kind: str | None = None,
        preferred_runtime_adapter: str | None = None,
        template_execution_kind: str | None = None,
    ) -> dict[str, object] | None:
        binding = self._bindings.get(step_kind)
        step_definition = self.get_step_kind(step_kind)
        if binding is None or step_definition is None:
            return None
        candidates = self.list_capabilities_for_step_kind(
            step_kind,
            context=context,
            include_disabled=False,
            allowed_capability_types=step_definition.allowed_capability_types,
        )
        if not candidates:
            return None

        hints = [item.strip() for item in (explicit_hints or []) if item and item.strip()]
        static_preferred_order = {capability_id: index for index, capability_id in enumerate(binding.preferred_capability_ids)}
        candidate_rows = self._build_realization_candidate_rows(
            candidates=candidates,
            context=context,
            hints=hints,
            static_preferred_order=static_preferred_order,
            preferred_execution_kind=preferred_execution_kind,
            preferred_runtime_adapter=preferred_runtime_adapter,
            template_execution_kind=template_execution_kind,
        )

        if hints:
            exact_static = [row for row in candidate_rows if row["static_exact_canonical_match"]]
            exact_reusable = [row for row in candidate_rows if row["exact_hint_match"] and row["source_kind"] == "materialization"]
            if exact_static and exact_reusable:
                review_only_static = [row for row in exact_static if row["review_needed"]]
                if review_only_static and template_execution_kind:
                    selected = sorted(
                        exact_reusable,
                        key=lambda row: (-int(row["reusable_priority"]), str(row["capability"].capability_id)),
                    )[0]
                    return self._selection_result(
                        step_kind=step_kind,
                        hints=hints,
                        candidate_rows=candidate_rows,
                        selected=selected,
                        reason=(
                            f"Selected runtime-materialized template candidate '{selected['capability'].capability_id}' "
                            f"instead of review-only static template alias because it matches requested template execution kind '{template_execution_kind}'."
                        ),
                        template_execution_kind=template_execution_kind,
                        policy_notes=[
                            "Canonical static built-ins keep deterministic priority for exact matches, except when the exact static match is still review-only and a reviewed materialized template candidate matches the requested template runtime shape.",
                        ],
                    )
            if exact_static:
                selected = sorted(exact_static, key=lambda row: str(row["capability"].capability_id))[0]
                return self._selection_result(
                    step_kind=step_kind,
                    hints=hints,
                    candidate_rows=candidate_rows,
                    selected=selected,
                    reason=f"Selected exact canonical static capability '{selected['capability'].capability_id}' for requested hint.",
                    template_execution_kind=template_execution_kind,
                    policy_notes=[
                        "Canonical static built-ins retain highest deterministic priority for exact requested matches.",
                    ],
                )
            if exact_reusable:
                selected = sorted(
                    exact_reusable,
                    key=lambda row: (-int(row["reusable_priority"]), str(row["capability"].capability_id)),
                )[0]
                return self._selection_result(
                    step_kind=step_kind,
                    hints=hints,
                    candidate_rows=candidate_rows,
                    selected=selected,
                    reason=(
                        f"Selected reusable materialized capability '{selected['capability'].capability_id}' "
                        "because it exactly matches the requested project-scoped capability hint."
                    ),
                    template_execution_kind=template_execution_kind,
                    policy_notes=[
                        "Project-scoped reusable materializations can win when the requested hint matches exactly.",
                    ],
                )
            exact_active_internal = self._resolve_active_internal_candidate(
                step_kind=step_kind,
                context=context,
                hints=hints,
            )
            if exact_active_internal is not None:
                return self._active_internal_selection_result(
                    step_kind=step_kind,
                    hints=hints,
                    candidate_rows=candidate_rows,
                    selected=exact_active_internal,
                    reason=(
                        f"Selected active internal capability '{exact_active_internal['capability_id']}' "
                        "because it exactly matches the requested project-scoped capability hint, but it is not runtime-materialized yet."
                    ),
                    policy_notes=[
                        "Active internal capabilities can be preserved for explainability, but they remain non-runnable until runtime materialization exists.",
                    ],
                )
            return None
        if template_execution_kind:
            template_matches = [row for row in candidate_rows if row["template_runtime_match"]]
            if template_matches:
                selected = sorted(
                    template_matches,
                    key=lambda row: (
                        0 if row["source_kind"] == "materialization" else 1,
                        -int(row["reusable_priority"]),
                        int(row["static_preferred_rank"]),
                        str(row["capability"].capability_id),
                    ),
                )[0]
                return self._selection_result(
                    step_kind=step_kind,
                    hints=hints,
                    candidate_rows=candidate_rows,
                    selected=selected,
                    reason=(
                        f"Selected template-compatible realization '{selected['capability'].capability_id}' "
                        f"because it matches requested template execution kind '{template_execution_kind}'."
                    ),
                    template_execution_kind=template_execution_kind,
                    policy_notes=[
                        "Template-backed candidates may win when their reviewed template runtime shape explicitly matches the requested template execution kind.",
                    ],
                )

        static_preferred = [
            row for row in candidate_rows if row["source_kind"] == "static_registry" and row["static_preferred_rank"] < len(static_preferred_order)
        ]
        if static_preferred:
            selected = sorted(
                static_preferred,
                key=lambda row: (int(row["static_preferred_rank"]), str(row["capability"].capability_id)),
            )[0]
            return self._selection_result(
                step_kind=step_kind,
                hints=hints,
                candidate_rows=candidate_rows,
                selected=selected,
                reason=(
                    f"Selected preferred static capability '{selected['capability'].capability_id}' "
                    f"for logical step kind '{step_kind}'."
                ),
                template_execution_kind=template_execution_kind,
                policy_notes=[
                    "Preferred static capabilities retain deterministic priority when no stronger exact or template-compatible signal is present.",
                ],
            )

        reusable_candidates = [row for row in candidate_rows if row["source_kind"] == "materialization"]
        if reusable_candidates:
            selected = sorted(
                reusable_candidates,
                key=lambda row: (-int(row["reusable_priority"]), str(row["capability"].capability_id)),
            )[0]
            return self._selection_result(
                step_kind=step_kind,
                hints=hints,
                candidate_rows=candidate_rows,
                selected=selected,
                reason=(
                    f"Selected reusable materialized capability '{selected['capability'].capability_id}' "
                    f"for logical step kind '{step_kind}' because no higher-priority static exact match was available."
                ),
                template_execution_kind=template_execution_kind,
                policy_notes=[
                    "Reusable materializations are considered after higher-priority exact static matches and preferred static bindings.",
                ],
            )

        selected = sorted(candidate_rows, key=lambda row: str(row["capability"].capability_id))[0]
        return self._selection_result(
            step_kind=step_kind,
            hints=hints,
            candidate_rows=candidate_rows,
            selected=selected,
            reason=f"Selected remaining valid capability '{selected['capability'].capability_id}'.",
            template_execution_kind=template_execution_kind,
            policy_notes=[
                "Deterministic fallback selected the remaining valid registered capability.",
            ],
        )

    def validate_capability_usage(
        self,
        *,
        capability_id: str,
        context: CapabilityContext,
        step_kind: str | None = None,
    ) -> CapabilityUsageValidationResult:
        errors: list[str] = []
        capability = self.get_capability(capability_id)
        if capability is None:
            return CapabilityUsageValidationResult(
                valid=False,
                capability_id=capability_id,
                context=context,
                step_kind=step_kind,
                errors=["Capability is not registered."],
            )
        if not capability.enabled:
            errors.append("Capability is registered but currently disabled.")
        if context not in capability.allowed_contexts:
            errors.append(f"Capability is not allowed in context '{context}'.")
        if step_kind is not None:
            if step_kind not in capability.supported_step_kinds:
                errors.append(f"Capability does not support logical step kind '{step_kind}'.")
            binding = self._bindings.get(step_kind)
            if binding is None:
                errors.append(f"No binding is registered for logical step kind '{step_kind}'.")
            else:
                if capability_id not in binding.capability_ids:
                    errors.append(f"Capability is not bound to logical step kind '{step_kind}'.")
                if context not in binding.allowed_contexts:
                    errors.append(f"Logical step kind '{step_kind}' is not allowed in context '{context}'.")
        return CapabilityUsageValidationResult(
            valid=len(errors) == 0,
            capability_id=capability_id,
            context=context,
            step_kind=step_kind,
            errors=errors,
        )

    def detect_capability_gap(
        self,
        *,
        step_kind: str,
        context: CapabilityContext,
    ) -> CapabilityGap | None:
        step_definition = self.get_step_kind(step_kind)
        if step_definition is None:
            return CapabilityGap(
                step_kind=step_kind,
                context=context,
                reason="unknown_step_kind",
                notes=["The requested logical step kind is not registered."],
            )
        all_candidates = self.list_capabilities_for_step_kind(step_kind, include_disabled=True)
        available = self.list_capabilities_for_step_kind(
            step_kind,
            context=context,
            include_disabled=False,
            allowed_capability_types=step_definition.allowed_capability_types,
        )
        if available:
            return None
        if not all_candidates:
            return CapabilityGap(
                step_kind=step_kind,
                context=context,
                reason="no_registered_capability",
                allowed_capability_types=list(step_definition.allowed_capability_types),
                notes=["No concrete capability is registered for this logical step kind."],
            )
        context_allowed = self.list_capabilities_for_step_kind(step_kind, context=context, include_disabled=True)
        if not context_allowed:
            return CapabilityGap(
                step_kind=step_kind,
                context=context,
                reason="no_capability_allowed_in_context",
                allowed_capability_types=list(step_definition.allowed_capability_types),
                candidate_capability_ids=[item.capability_id for item in all_candidates],
                blocked_capability_ids=[item.capability_id for item in all_candidates],
                notes=["Capabilities exist for this step kind, but none are allowed in the requested context."],
            )
        disabled = [item.capability_id for item in context_allowed if not item.enabled]
        if disabled and len(disabled) == len(context_allowed):
            return CapabilityGap(
                step_kind=step_kind,
                context=context,
                reason="all_candidate_capabilities_disabled",
                allowed_capability_types=list(step_definition.allowed_capability_types),
                candidate_capability_ids=[item.capability_id for item in context_allowed],
                blocked_capability_ids=disabled,
                notes=["Capabilities exist for this step kind and context, but all are disabled."],
            )
        return CapabilityGap(
            step_kind=step_kind,
            context=context,
            reason="no_capability_satisfies_requested_policy",
            allowed_capability_types=list(step_definition.allowed_capability_types),
            candidate_capability_ids=[item.capability_id for item in context_allowed],
            blocked_capability_ids=[item.capability_id for item in context_allowed],
            notes=["Capabilities exist, but none satisfy the current policy filters."],
        )

    def suggest_alternative_capabilities(
        self,
        *,
        step_kind: str | None,
        context: CapabilityContext,
        exclude_capability_ids: list[str] | None = None,
        limit: int = 3,
    ) -> list[CapabilityAlternativeResponse]:
        if not step_kind:
            return []
        step_definition = self.get_step_kind(step_kind)
        binding = self._bindings.get(step_kind)
        if step_definition is None or binding is None:
            return []
        exclude = {item for item in (exclude_capability_ids or []) if item}
        candidates = self.list_capabilities_for_step_kind(
            step_kind,
            context=context,
            include_disabled=False,
            allowed_capability_types=step_definition.allowed_capability_types,
        )
        preferred_order = {capability_id: index for index, capability_id in enumerate(binding.preferred_capability_ids)}
        ordered = sorted(
            [item for item in candidates if item.capability_id not in exclude],
            key=lambda item: (preferred_order.get(item.capability_id, len(preferred_order)), item.capability_id),
        )
        return [
            CapabilityAlternativeResponse(
                capability_id=item.capability_id,
                name=item.name,
                capability_type=item.capability_type,
                reason=f"Registered alternative for logical step kind '{step_kind}' in context '{context}'.",
                requires_review=item.requires_review or item.template_backed_only,
                enabled=item.enabled,
            )
            for item in ordered[:limit]
        ]

    def build_request_suggestion(
        self,
        *,
        stage: CapabilityContext,
        logical_step_kind: str | None,
        requested_capability_id_or_name: str | None,
        reason: str,
        review_required: bool = False,
        request_kind: CapabilityRequestKind | None = None,
    ) -> CapabilityRequestSuggestionResponse:
        resolved_request_kind = request_kind
        if resolved_request_kind is None:
            if review_required:
                resolved_request_kind = "manual_review"
            elif logical_step_kind:
                step_definition = self.get_step_kind(logical_step_kind)
                if step_definition is not None and "code_template" in step_definition.allowed_capability_types:
                    resolved_request_kind = "template_request"
                else:
                    resolved_request_kind = "capability_request"
            else:
                resolved_request_kind = "capability_request"
        requested_purpose = requested_capability_id_or_name or logical_step_kind or "unmapped capability need"
        return CapabilityRequestSuggestionResponse(
            request_kind=resolved_request_kind,
            requested_logical_step_kind=logical_step_kind,
            requested_capability_purpose=f"Support '{requested_purpose}' safely within registered capability boundaries.",
            example_use_context=f"{stage}:{logical_step_kind or 'general'}",
            originating_stage=stage,
            review_reason=reason,
        )

    def build_gap_response(
        self,
        *,
        gap_kind: CapabilityGapKind,
        stage: CapabilityContext,
        reason: str,
        logical_step_kind: str | None = None,
        requested_capability_id_or_name: str | None = None,
        normalized_requested_capability_id: str | None = None,
        context_ref: str | None = None,
        severity: str = "warning",
        review_required: bool = False,
        notes: list[str] | None = None,
        alternative_capabilities: list[CapabilityAlternativeResponse] | None = None,
        suggested_request: CapabilityRequestSuggestionResponse | None = None,
    ) -> CapabilityGapResponse:
        normalized_requested = normalized_requested_capability_id
        if normalized_requested is None and requested_capability_id_or_name:
            capability = self.get_capability(requested_capability_id_or_name)
            if capability is not None:
                normalized_requested = capability.capability_id
        alternatives = alternative_capabilities
        if alternatives is None:
            exclude = [item for item in [normalized_requested, requested_capability_id_or_name] if item]
            alternatives = self.suggest_alternative_capabilities(
                step_kind=logical_step_kind,
                context=stage,
                exclude_capability_ids=exclude,
            )
        if suggested_request is None and not alternatives:
            suggested_request = self.build_request_suggestion(
                stage=stage,
                logical_step_kind=logical_step_kind,
                requested_capability_id_or_name=requested_capability_id_or_name,
                reason=reason,
                review_required=review_required,
            )
        gap_id_parts = [stage, gap_kind, logical_step_kind or "general", context_ref or requested_capability_id_or_name or reason]
        return CapabilityGapResponse(
            gap_id=":".join(part.replace(" ", "_") for part in gap_id_parts if part),
            gap_kind=gap_kind,
            stage=stage,
            logical_step_kind=logical_step_kind,
            requested_capability_id_or_name=requested_capability_id_or_name,
            normalized_requested_capability_id=normalized_requested,
            context_ref=context_ref,
            reason=reason,
            severity=severity,
            review_required=review_required,
            alternative_capabilities=alternatives,
            suggested_request=suggested_request,
            notes=list(notes or []),
        )

    def build_gap_response_from_registry_gap(
        self,
        *,
        registry_gap: CapabilityGap,
        stage: CapabilityContext,
        gap_kind: CapabilityGapKind,
        requested_capability_id_or_name: str | None = None,
        context_ref: str | None = None,
        severity: str = "warning",
        review_required: bool = False,
    ) -> CapabilityGapResponse:
        alternatives = self.suggest_alternative_capabilities(
            step_kind=registry_gap.step_kind,
            context=stage,
            exclude_capability_ids=[item for item in [requested_capability_id_or_name] if item],
        )
        suggested_request = None
        if not alternatives:
            suggested_request = self.build_request_suggestion(
                stage=stage,
                logical_step_kind=registry_gap.step_kind,
                requested_capability_id_or_name=requested_capability_id_or_name,
                reason=registry_gap.reason,
                review_required=review_required,
            )
        notes = list(registry_gap.notes)
        if registry_gap.blocked_capability_ids:
            notes.append(f"Blocked registered capabilities: {', '.join(registry_gap.blocked_capability_ids)}")
        if registry_gap.candidate_capability_ids:
            notes.append(f"Registered candidates: {', '.join(registry_gap.candidate_capability_ids)}")
        return self.build_gap_response(
            gap_kind=gap_kind,
            stage=stage,
            reason=registry_gap.reason,
            logical_step_kind=registry_gap.step_kind,
            requested_capability_id_or_name=requested_capability_id_or_name,
            context_ref=context_ref,
            severity=severity,
            review_required=review_required,
            notes=notes,
            alternative_capabilities=alternatives,
            suggested_request=suggested_request,
        )

    def summarize_capability_gaps(self, gaps: list[CapabilityGapResponse]) -> CapabilityGapSummaryResponse:
        stage_counts: dict[str, int] = {}
        warning_count = 0
        review_required_count = 0
        with_alternatives_count = 0
        with_request_suggestion_count = 0
        for gap in gaps:
            stage_counts[gap.stage] = stage_counts.get(gap.stage, 0) + 1
            if gap.severity == "warning":
                warning_count += 1
            if gap.review_required:
                review_required_count += 1
            if gap.alternative_capabilities:
                with_alternatives_count += 1
            if gap.suggested_request is not None:
                with_request_suggestion_count += 1
        if not gaps:
            summary = "No capability gaps detected."
        else:
            summary = f"Detected {len(gaps)} capability gap(s) across {len(stage_counts)} stage(s)."
        return CapabilityGapSummaryResponse(
            summary=summary,
            total_count=len(gaps),
            warning_count=warning_count,
            review_required_count=review_required_count,
            with_alternatives_count=with_alternatives_count,
            with_request_suggestion_count=with_request_suggestion_count,
            stage_counts=stage_counts,
        )

    def _validate_registry(self) -> None:
        for binding in CAPABILITY_BINDINGS:
            if binding.step_kind not in self._step_kinds:
                raise RuntimeError(
                    f"Capability binding references unknown logical step kind '{binding.step_kind}'."
                )
            for capability_id in binding.capability_ids:
                capability = self._capabilities.get(capability_id)
                if capability is None:
                    raise RuntimeError(f"Capability binding references unknown capability '{capability_id}'.")
                if binding.step_kind not in capability.supported_step_kinds:
                    raise RuntimeError(
                        f"Capability '{capability_id}' does not declare support for logical step kind "
                        f"'{binding.step_kind}'."
                    )

    def _capability_for_reusable_candidate(self, candidate: CapabilityReusableCandidateResponse) -> CapabilityDefinition:
        capability_type: CapabilityType = "code_template" if candidate.origin_request_kind == "template_backed_internal" else "deterministic"
        inherited = self._capabilities.get(str(candidate.linked_static_registry_capability_id or "").strip())
        return CapabilityDefinition(
            capability_id=candidate.capability_id,
            name=candidate.display_name,
            description=candidate.purpose_summary or candidate.selection_explanation,
            supported_step_kinds=list(candidate.supported_step_kinds),
            capability_type=capability_type,
            input_contract_summary=candidate.config_contract_summary or candidate.purpose_summary,
            output_contract_summary=candidate.selection_explanation,
            execution_kind=candidate.runtime_shape.execution_kind,
            runtime_adapter=candidate.runtime_shape.runtime_adapter,
            runtime_requirements=[],
            provider_requirements=[],
            allowed_contexts=list(candidate.enabled_contexts),
            requires_review=False,
            enabled=candidate.runtime_ready and candidate.reusable,
            template_backed_only=candidate.origin_request_kind == "template_backed_internal",
            semantic_purpose=inherited.semantic_purpose if inherited is not None else candidate.purpose_summary,
            semantic_role=inherited.semantic_role if inherited is not None else None,
            consumes_evidence_types=list(inherited.consumes_evidence_types) if inherited is not None else [],
            produces_evidence_types=list(inherited.produces_evidence_types) if inherited is not None else [],
            deliverable_intents=list(inherited.deliverable_intents) if inherited is not None else [],
            preferred_upstream_capability_ids=list(inherited.preferred_upstream_capability_ids) if inherited is not None else [],
            preferred_downstream_capability_ids=list(inherited.preferred_downstream_capability_ids) if inherited is not None else [],
            source_modalities_supported=list(inherited.source_modalities_supported) if inherited is not None else [],
            coverage_semantics=inherited.coverage_semantics if inherited is not None else None,
            confidence_semantics=inherited.confidence_semantics if inherited is not None else None,
            quality_gate_relevance=inherited.quality_gate_relevance if inherited is not None else None,
            planning_visibility=(
                inherited.planning_visibility
                if inherited is not None and inherited.planning_visibility == "reusable_allowed"
                else "static_only"
            ),
            tags=[candidate.origin_request_kind, "runtime_materialized", "reusable"],
        )

    def _unique(self, items: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in items:
            normalized = str(item).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    def _build_realization_candidate_rows(
        self,
        *,
        candidates: list[CapabilityDefinition],
        context: CapabilityContext,
        hints: list[str],
        static_preferred_order: dict[str, int],
        preferred_execution_kind: str | None,
        preferred_runtime_adapter: str | None,
        template_execution_kind: str | None,
    ) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for item in candidates:
            source_kind, source_ref = self.get_capability_source(item.capability_id)
            reusable = self._reusable_candidates.get(item.capability_id)
            aliases = set(reusable.alias_ids if reusable is not None else [item.capability_id])
            exact_hint_match = any(hint in aliases or hint == item.capability_id for hint in hints)
            static_exact_canonical_match = source_kind == "static_registry" and any(hint == item.capability_id for hint in hints)
            static_preferred_rank = static_preferred_order.get(item.capability_id, len(static_preferred_order) + 100)
            reusable_priority = reusable.selection_priority if reusable is not None else 0
            execution_kind = reusable.runtime_shape.execution_kind if reusable is not None else item.execution_kind
            runtime_adapter = reusable.runtime_shape.runtime_adapter if reusable is not None else item.runtime_adapter
            handler_ref = reusable.runtime_shape.handler_ref if reusable is not None else None
            template_runtime_match = bool(template_execution_kind and execution_kind == "template_runtime")
            execution_kind_match = bool(preferred_execution_kind and execution_kind == preferred_execution_kind)
            runtime_adapter_match = bool(preferred_runtime_adapter and runtime_adapter == preferred_runtime_adapter)
            review_needed = bool(item.requires_review or item.template_backed_only)
            runnable = bool(item.enabled and context in item.allowed_contexts)
            rows.append(
                {
                    "capability": item,
                    "source_kind": source_kind,
                    "source_ref": source_ref,
                    "reusable": reusable,
                    "exact_hint_match": exact_hint_match,
                    "static_exact_canonical_match": static_exact_canonical_match,
                    "static_preferred_rank": static_preferred_rank,
                    "reusable_priority": reusable_priority,
                    "execution_kind": execution_kind,
                    "runtime_adapter": runtime_adapter,
                    "handler_ref": handler_ref,
                    "template_runtime_match": template_runtime_match,
                    "execution_kind_match": execution_kind_match,
                    "runtime_adapter_match": runtime_adapter_match,
                    "review_needed": review_needed,
                    "runnable": runnable,
                }
            )
        return rows

    def _candidate_policy_flags(self, row: dict[str, object]) -> list[str]:
        capability = row["capability"]
        reusable = row["reusable"]
        policy_flags: list[str] = []
        if capability.requires_review or capability.template_backed_only:
            policy_flags.append("review_needed")
        if capability.requires_confirmation:
            policy_flags.append("requires_confirmation")
        if capability.blocked_by_default:
            policy_flags.append("blocked_by_default")
        if capability.default_timeout_seconds is not None:
            policy_flags.append(f"default_timeout:{capability.default_timeout_seconds}")
        if reusable is not None:
            if reusable.runtime_shape.runtime_constraints.get("policy_scope"):
                policy_flags.append(f"policy_scope:{reusable.runtime_shape.runtime_constraints['policy_scope']}")
            if reusable.origin_request_kind == "template_backed_internal":
                policy_flags.append("template_backed_internal")
        return policy_flags

    def _candidate_selection_constraints(self, row: dict[str, object]) -> list[str]:
        reusable = row["reusable"]
        if reusable is not None:
            return list(reusable.selection_constraints)
        constraints = ["must remain within registered runtime shapes"]
        if row["source_kind"] == "static_registry":
            constraints.append("canonical static built-ins keep deterministic priority for exact matches")
        return constraints

    def _candidate_fit_reason(self, row: dict[str, object], *, template_execution_kind: str | None) -> str:
        capability = row["capability"]
        reusable = row["reusable"]
        if row["static_exact_canonical_match"]:
            return "Matches an exact canonical static capability hint."
        if row["exact_hint_match"] and row["source_kind"] == "materialization":
            return "Matches a requested project-scoped reusable capability hint."
        if row["template_runtime_match"] and template_execution_kind:
            return f"Matches requested template execution kind '{template_execution_kind}'."
        if row["execution_kind_match"] and row["runtime_adapter_match"]:
            return "Matches requested execution kind and runtime adapter preferences."
        if row["execution_kind_match"]:
            return "Matches requested execution kind preference."
        if row["runtime_adapter_match"]:
            return "Matches requested runtime adapter preference."
        if row["source_kind"] == "materialization" and reusable is not None:
            return reusable.selection_explanation or "Reusable runtime-materialized internal capability."
        if row["static_preferred_rank"] < 100:
            return "Preferred static capability for this logical step kind."
        return capability.description or "Registered bounded realization candidate."

    def _candidate_summary(
        self,
        row: dict[str, object],
        *,
        determinism_rank: int,
        template_execution_kind: str | None,
        blocked_reason: str | None = None,
    ) -> BuildRealizationCandidate:
        capability = row["capability"]
        return BuildRealizationCandidate(
            candidate_id=capability.capability_id,
            source_kind=row["source_kind"],
            source_ref=row["source_ref"],
            execution_kind=row["execution_kind"],
            runtime_adapter=row["runtime_adapter"],
            handler_ref=row["handler_ref"],
            fit_reason=self._candidate_fit_reason(row, template_execution_kind=template_execution_kind),
            fit_score_components={
                "exact_hint_match": row["exact_hint_match"],
                "static_exact_canonical_match": row["static_exact_canonical_match"],
                "static_preferred_rank": row["static_preferred_rank"],
                "reusable_priority": row["reusable_priority"],
                "template_runtime_match": row["template_runtime_match"],
                "execution_kind_match": row["execution_kind_match"],
                "runtime_adapter_match": row["runtime_adapter_match"],
            },
            selection_constraints=self._candidate_selection_constraints(row),
            policy_flags=self._candidate_policy_flags(row),
            runnable=bool(row["runnable"]),
            review_needed=bool(row["review_needed"]),
            blocked_reason=blocked_reason,
            requires_confirmation=bool(capability.requires_confirmation),
            determinism_rank=determinism_rank,
        )

    def _rejection_reason(
        self,
        *,
        selected: dict[str, object],
        candidate: dict[str, object],
        reason: str,
    ) -> str:
        if selected["source_kind"] == "lifecycle_entry":
            return "Requested active internal capability hint was preserved for explainability, but runtime materialization is still required."
        if selected["static_exact_canonical_match"]:
            return "Rejected because a higher-priority exact canonical static match was selected."
        if selected["exact_hint_match"] and selected["source_kind"] == "materialization":
            return "Rejected because a requested project-scoped reusable capability hint matched another candidate exactly."
        if selected["template_runtime_match"] and not candidate["template_runtime_match"]:
            return "Rejected because another candidate matched the requested template runtime shape more closely."
        if selected["source_kind"] == "static_registry" and selected["static_preferred_rank"] < candidate["static_preferred_rank"]:
            return "Rejected because a preferred static capability retained deterministic priority."
        if selected["source_kind"] == "materialization" and candidate["source_kind"] != "materialization":
            return "Rejected because a reusable runtime-materialized candidate ranked higher under deterministic policy."
        return f"Rejected because another bounded realization candidate ranked higher: {reason}"

    def _selection_result(
        self,
        *,
        step_kind: str,
        hints: list[str],
        candidate_rows: list[dict[str, object]],
        selected: dict[str, object],
        reason: str,
        template_execution_kind: str | None = None,
        policy_notes: list[str] | None = None,
    ) -> dict[str, object]:
        selected_capability = selected["capability"]
        ordered_rows = sorted(
            candidate_rows,
            key=lambda row: (
                0 if row["capability"].capability_id == selected_capability.capability_id else 1,
                0 if row["static_exact_canonical_match"] else 1,
                0 if row["exact_hint_match"] else 1,
                0 if row["template_runtime_match"] else 1,
                int(row["static_preferred_rank"]),
                -int(row["reusable_priority"]),
                str(row["capability"].capability_id),
            ),
        )
        considered_candidates = [
            self._candidate_summary(
                row,
                determinism_rank=index + 1,
                template_execution_kind=template_execution_kind,
            )
            for index, row in enumerate(ordered_rows)
        ]
        selected_candidate = next(
            candidate
            for candidate in considered_candidates
            if candidate.candidate_id == selected_capability.capability_id
        )
        rejected_candidates = [
            BuildRealizationRejectedCandidate(
                candidate_id=row["capability"].capability_id,
                source_kind=row["source_kind"],
                source_ref=row["source_ref"],
                rejection_reason=self._rejection_reason(selected=selected, candidate=row, reason=reason),
            )
            for row in ordered_rows
            if row["capability"].capability_id != selected_capability.capability_id
        ]
        return {
            "step_kind": step_kind,
            "selected_capability_id": selected_capability.capability_id,
            "selected_source_kind": selected["source_kind"],
            "selected_source_ref": selected["source_ref"],
            "selected_materialization_id": selected["reusable"].source_materialization_id if selected["reusable"] is not None else None,
            "selection_reason": reason,
            "considered_candidate_ids": [candidate.candidate_id for candidate in considered_candidates],
            "rejected_candidate_ids": [candidate.candidate_id for candidate in rejected_candidates],
            "requested_hints": list(hints),
            "realization_candidates": [candidate.model_dump(mode="python") for candidate in considered_candidates],
            "rejected_candidates": [candidate.model_dump(mode="python") for candidate in rejected_candidates],
            "selected_candidate": selected_candidate.model_dump(mode="python"),
            "flexibility_explanation": BuildRealizationFlexibilityExplanation(
                summary=(
                    f"Considered {len(considered_candidates)} bounded realization candidate(s) "
                    f"and selected '{selected_capability.capability_id}'."
                ),
                selection_result=BuildRealizationSelectionResult(
                    selected_candidate_id=selected_capability.capability_id,
                    selected_source_kind=selected["source_kind"],
                    final_reason=reason,
                    considered_candidate_ids=[candidate.candidate_id for candidate in considered_candidates],
                    rejected_candidate_ids=[candidate.candidate_id for candidate in rejected_candidates],
                ),
                selected_candidate=selected_candidate,
                considered_candidates=considered_candidates,
                rejected_candidates=rejected_candidates,
                deterministic_policy_notes=list(policy_notes or []),
            ).model_dump(mode="python"),
        }

    def _resolve_active_internal_candidate(
        self,
        *,
        step_kind: str,
        context: CapabilityContext,
        hints: list[str],
    ) -> dict[str, object] | None:
        matches: list[dict[str, object]] = []
        for hint in hints:
            capability_id = self._active_internal_aliases.get(hint)
            if capability_id is None:
                continue
            candidate = self._active_internal_candidates.get(capability_id)
            if candidate is None:
                continue
            if step_kind not in list(candidate["supported_step_kinds"] or []):
                continue
            if context not in list(candidate["enabled_contexts"] or []):
                continue
            matches.append(candidate)
        if not matches:
            return None
        return sorted(matches, key=lambda item: str(item["capability_id"]))[0]

    def _active_internal_selection_result(
        self,
        *,
        step_kind: str,
        hints: list[str],
        candidate_rows: list[dict[str, object]],
        selected: dict[str, object],
        reason: str,
        policy_notes: list[str] | None = None,
    ) -> dict[str, object]:
        selected_capability_id = str(selected["capability_id"])
        considered_candidate_ids = [row["capability"].capability_id for row in candidate_rows]
        if selected_capability_id not in considered_candidate_ids:
            considered_candidate_ids.append(selected_capability_id)
        request_kind_origin = str(selected.get("request_kind_origin") or "")
        blocked_reason = (
            "active_template_capability_not_materialized"
            if request_kind_origin == "template_backed_internal"
            else "active_internal_capability_not_materialized"
        )
        selected_candidate = BuildRealizationCandidate(
            candidate_id=selected_capability_id,
            source_kind="lifecycle_entry",
            source_ref=selected["source_ref"],
            execution_kind=None,
            runtime_adapter=None,
            handler_ref=None,
            fit_reason="Matches a requested active internal capability hint, but runtime materialization is still missing.",
            fit_score_components={"exact_hint_match": True, "runtime_materialized": False},
            selection_constraints=["must be runtime-materialized before it can become runnable", "project-scoped only"],
            policy_flags=["review_needed", "not_runtime_materialized"],
            runnable=False,
            review_needed=True,
            blocked_reason=blocked_reason,
            requires_confirmation=False,
            determinism_rank=1,
        )
        considered_candidates = [selected_candidate]
        considered_candidates.extend(
            self._candidate_summary(row, determinism_rank=index + 2, template_execution_kind=None)
            for index, row in enumerate(
                sorted(candidate_rows, key=lambda row: str(row["capability"].capability_id))
            )
        )
        rejected_candidates = [
            BuildRealizationRejectedCandidate(
                candidate_id=candidate.candidate_id,
                source_kind=candidate.source_kind,
                source_ref=candidate.source_ref,
                rejection_reason="Rejected because the explicitly requested active internal capability hint was preserved, even though runtime materialization is still required.",
            )
            for candidate in considered_candidates
            if candidate.candidate_id != selected_capability_id
        ]
        return {
            "step_kind": step_kind,
            "selected_capability_id": selected_capability_id,
            "selected_source_kind": "lifecycle_entry",
            "selected_source_ref": selected["source_ref"],
            "selected_materialization_id": None,
            "selection_reason": reason,
            "considered_candidate_ids": [candidate.candidate_id for candidate in considered_candidates],
            "rejected_candidate_ids": [candidate.candidate_id for candidate in rejected_candidates],
            "requested_hints": list(hints),
            "realization_candidates": [candidate.model_dump(mode="python") for candidate in considered_candidates],
            "rejected_candidates": [candidate.model_dump(mode="python") for candidate in rejected_candidates],
            "selected_candidate": selected_candidate.model_dump(mode="python"),
            "flexibility_explanation": BuildRealizationFlexibilityExplanation(
                summary=(
                    f"Preserved requested active internal capability '{selected_capability_id}' for explainability, "
                    "but runtime materialization is still required before it can become runnable."
                ),
                selection_result=BuildRealizationSelectionResult(
                    selected_candidate_id=selected_capability_id,
                    selected_source_kind="lifecycle_entry",
                    final_reason=reason,
                    considered_candidate_ids=[candidate.candidate_id for candidate in considered_candidates],
                    rejected_candidate_ids=[candidate.candidate_id for candidate in rejected_candidates],
                ),
                selected_candidate=selected_candidate,
                considered_candidates=considered_candidates,
                rejected_candidates=rejected_candidates,
                deterministic_policy_notes=list(policy_notes or []),
            ).model_dump(mode="python"),
        }
