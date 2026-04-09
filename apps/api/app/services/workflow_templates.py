"""Workflow scenario templates for end-to-end delivery.

Factory functions that return complete blueprint creation payloads.
Each template is designed for a specific domain and produces a markdown artifact.

Usage:
    from app.services.workflow_templates import research_briefing_blueprint
    payload = research_briefing_blueprint(topic="AI safety 2025")
    # POST /api/v1/blueprints with payload
"""
from __future__ import annotations

from typing import Any


def research_briefing_blueprint(
    *,
    project_id: str,
    requirement_id: str | None = None,
    topic: str = "AI developments",
) -> dict[str, Any]:
    """Blueprint for a research briefing workflow.

    Pipeline: gather_sources → analyze_sources → produce_briefing (linear)
    Output: Structured markdown briefing with evidence citations.
    """
    return {
        "project_id": project_id,
        "requirement_id": requirement_id,
        "blueprint_type": "workflow",
        "name": f"Research Briefing: {topic}",
        "execution_steps_json": {
            "steps": [
                {
                    "step_name": "gather_sources",
                    "node_name": "gatherer",
                    "logical_step_kind": "acquire",
                    "purpose": f"Search for and fetch recent sources on: {topic}",
                    "task_prompt": (
                        f"You are a research assistant. Your task is to find relevant sources on: {topic}.\n\n"
                        "IMPORTANT: web_search only searches a small bounded corpus. For real content, use fetch_url "
                        "to retrieve actual web pages by URL. Try fetching relevant URLs you know about the topic.\n\n"
                        "Strategy:\n"
                        "1. Use web_search to check the bounded corpus for any relevant items.\n"
                        "2. Use fetch_url to retrieve real web pages (e.g., Wikipedia, news sites, research archives).\n"
                        "3. Collect at least 3 distinct sources. For each source, record: title, URL, key excerpt.\n"
                        "Return a JSON object with a 'sources' array containing the collected items."
                    ),
                },
                {
                    "step_name": "analyze_sources",
                    "node_name": "analyzer",
                    "logical_step_kind": "analyze",
                    "purpose": "Extract key claims and evidence from gathered sources",
                    "task_prompt": (
                        "You are an evidence analyst. Review the gathered sources and extract:\n"
                        "1. Key claims (with source attribution)\n"
                        "2. Supporting evidence for each claim\n"
                        "3. Contradictions or gaps between sources\n"
                        "4. Confidence level for each finding (high/medium/low)\n\n"
                        "Return a structured JSON with 'findings' array, each containing: "
                        "claim, evidence, source_ref, confidence."
                    ),
                },
                {
                    "step_name": "produce_briefing",
                    "node_name": "writer",
                    "logical_step_kind": "summarize",
                    "step_type": "artifact_writer",
                    "writer_kind": "markdown_report",
                    "purpose": "Produce the final research briefing as markdown",
                    "output_filename": "briefing.md",
                    "task_prompt": (
                        f"Write a structured research briefing on: {topic}\n\n"
                        "Format as markdown with these sections:\n"
                        "# Research Briefing: {topic}\n"
                        "## Executive Summary\n"
                        "## Key Findings\n"
                        "## Evidence & Sources\n"
                        "## Gaps & Unknowns\n"
                        "## Recommendations\n\n"
                        "Each finding must cite its source. Use [Source: title](url) format."
                    ),
                },
            ],
        },
        "runtime_constraints_json": {
            "auto_propose_tools": True,
            "semantic_interpretation": {
                "semantic_core": {"target_object": "web_resource"},
            },
        },
    }


def repo_analysis_blueprint(
    *,
    project_id: str,
    requirement_id: str | None = None,
    repo_path: str = ".",
    modules: list[str] | None = None,
) -> dict[str, Any]:
    """Blueprint for repository architecture analysis.

    Pipeline: scan_repo → [analyze_module × N] → merge_report (DAG fan-out)
    Output: Markdown architecture report.
    """
    if modules is None:
        modules = ["api", "worker", "desktop"]

    analysis_steps = []
    for mod in modules:
        analysis_steps.append({
            "step_name": f"analyze_{mod}",
            "node_name": f"{mod}_analyzer",
            "logical_step_kind": "analyze",
            "purpose": f"Analyze the {mod} module architecture independently from scan results",
            "task_prompt": (
                f"You are a code architecture analyst. Analyze the '{mod}' module.\n\n"
                "Use read_file to examine key files (entry points, configs, main modules).\n"
                "Report on:\n"
                "1. Module purpose and responsibility\n"
                "2. Key dependencies (internal and external)\n"
                "3. Architecture patterns used\n"
                "4. Entry points and public API surface\n"
                "5. Potential concerns or technical debt\n\n"
                "Return a structured JSON with: module_name, purpose, dependencies, patterns, concerns."
            ),
        })

    module_list = ", ".join(modules)
    return {
        "project_id": project_id,
        "requirement_id": requirement_id,
        "blueprint_type": "workflow",
        "name": f"Repository Analysis: {repo_path}",
        "execution_steps_json": {
            "steps": [
                {
                    "step_name": "scan_repo",
                    "node_name": "scanner",
                    "logical_step_kind": "acquire",
                    "purpose": "Scan repository structure and identify key modules",
                    "task_prompt": (
                        f"You are a repository scanner. Scan the repository at: {repo_path}\n\n"
                        "Use read_file to examine the root directory structure, README, and key config files.\n"
                        f"Focus on identifying these modules: {module_list}.\n"
                        "For each module, report: directory path, language, main config file.\n"
                        "Return JSON with: repo_root, modules (array), language_distribution, build_system."
                    ),
                },
                *analysis_steps,
                {
                    "step_name": "merge_report",
                    "node_name": "merger",
                    "logical_step_kind": "summarize",
                    "step_type": "artifact_writer",
                    "writer_kind": "markdown_report",
                    "purpose": "Merge all module analyses into a unified architecture report",
                    "output_filename": "architecture_report.md",
                    "task_prompt": (
                        "Write a comprehensive architecture report combining all module analyses.\n\n"
                        "Format as markdown:\n"
                        "# Architecture Report\n"
                        "## Overview\n"
                        "## Module Breakdown\n"
                        f"### {chr(10).join('### ' + m for m in modules)}\n"
                        "## Cross-Module Dependencies\n"
                        "## Architecture Patterns\n"
                        "## Recommendations\n"
                    ),
                },
            ],
        },
        "runtime_constraints_json": {
            "auto_propose_tools": True,
            "semantic_interpretation": {
                "semantic_core": {"target_object": "repository"},
            },
        },
    }


def dataset_assessment_blueprint(
    *,
    project_id: str,
    requirement_id: str | None = None,
    file_path: str = "data.csv",
) -> dict[str, Any]:
    """Blueprint for dataset quality assessment.

    Pipeline: ingest_data → [profile_schema, detect_quality] → produce_report (diamond DAG)
    Output: Markdown quality assessment report.
    """
    return {
        "project_id": project_id,
        "requirement_id": requirement_id,
        "blueprint_type": "workflow",
        "name": f"Dataset Assessment: {file_path}",
        "execution_steps_json": {
            "steps": [
                {
                    "step_name": "ingest_data",
                    "node_name": "ingester",
                    "logical_step_kind": "acquire",
                    "purpose": "Load and sample the dataset file",
                    "task_prompt": (
                        f"Load the dataset from: {file_path}\n\n"
                        "Use read_file to load the data. Report:\n"
                        "1. File format and size\n"
                        "2. Row count and column names\n"
                        "3. First 5 sample rows\n"
                        "4. Inferred data types per column\n\n"
                        "Return JSON with: file_path, format, row_count, columns (with types), sample_rows."
                    ),
                },
                {
                    "step_name": "profile_schema",
                    "node_name": "profiler",
                    "logical_step_kind": "analyze",
                    "purpose": "Profile data schema and field distributions independently from ingested data",
                    "task_prompt": (
                        "From the ingested data, profile each column:\n"
                        "1. Data type distribution\n"
                        "2. Null/missing rate\n"
                        "3. Unique value count\n"
                        "4. Min/max/mean for numeric fields\n"
                        "5. Most common values for categorical fields\n\n"
                        "Return JSON with: columns array, each with name, type, null_rate, unique_count, stats."
                    ),
                },
                {
                    "step_name": "detect_quality",
                    "node_name": "quality_checker",
                    "logical_step_kind": "analyze",
                    "purpose": "Detect data quality issues and anomalies independently from ingested data",
                    "task_prompt": (
                        "From the ingested data, detect quality issues:\n"
                        "1. Missing values (columns with >10% nulls)\n"
                        "2. Type inconsistencies (mixed types in same column)\n"
                        "3. Outliers (values beyond 3 std deviations)\n"
                        "4. Duplicate rows\n"
                        "5. Format inconsistencies (date formats, encoding)\n\n"
                        "Return JSON with: issues array, each with issue_type, severity, affected_columns, description."
                    ),
                },
                {
                    "step_name": "produce_report",
                    "node_name": "reporter",
                    "logical_step_kind": "summarize",
                    "step_type": "artifact_writer",
                    "writer_kind": "markdown_report",
                    "purpose": "Merge schema profiling and quality detection into assessment report",
                    "output_filename": "quality_report.md",
                    "task_prompt": (
                        f"Write a data quality assessment report for: {file_path}\n\n"
                        "Format as markdown:\n"
                        "# Data Quality Assessment\n"
                        "## Dataset Overview\n"
                        "## Schema Profile\n"
                        "## Quality Issues\n"
                        "## Severity Summary\n"
                        "## Recommendations\n"
                    ),
                },
            ],
        },
        "runtime_constraints_json": {
            "auto_propose_tools": True,
            "semantic_interpretation": {
                "semantic_core": {"target_object": "dataset"},
            },
        },
    }
