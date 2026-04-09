from __future__ import annotations

import json
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import app.services.requirement.compiler.llm_assisted as compiler_llm_assisted
import app.services.requirement.requirement_compiler_service as requirement_compiler_service
from app.services.provider_runtime import ProviderResponse, ResolvedModelConfig
from tests.test_requirement_compiler_v251 import canonical_acceptance_criteria, canonical_constraints, create_project

REPORT_VERSION = "1.0"
VALIDATION_STAGES = ("requirement_analysis", "family_recommendation", "skeleton_slot_filling")


@dataclass(frozen=True)
class GoldenRequirementCase:
    key: str
    title: str
    goal: str
    input_description: str
    output_description: str
    constraints_json: dict[str, Any]
    acceptance_criteria_json: dict[str, Any]
    expected_family: str | None = None
    expected_completion_state: str | None = None
    expected_clarification: bool | None = None
    expected_human_review_signal: bool | None = None
    domain: str | None = None
    interactivity_level: str | None = None
    human_review_required: bool = False
    recurrence_mode: str | None = None
    external_tools_needed_json: dict[str, Any] = field(default_factory=lambda: {"items": []})
    clarification_answers: dict[str, Any] = field(default_factory=dict)
    expected_recompiled_family: str | None = None
    expected_recompiled_completion_state: str | None = None
    expected_llm_recompiled_completion_state: str | None = None
    llm_family_behavior: str = "accepted"
    llm_analysis_behavior: str = "accepted"
    llm_slot_fill_behavior: str = "accepted"


GOLDEN_REQUIREMENT_CASES: list[GoldenRequirementCase] = [
    GoldenRequirementCase(
        key="markdown_blog_post_polisher",
        title="Markdown Blog Post Polisher",
        goal="Improve a raw markdown blog post by correcting grammar and improving clarity while preserving the author's original tone.",
        input_description="A markdown document written by a human author.",
        output_description="A polished markdown document with corrected grammar and improved readability.",
        constraints_json=canonical_constraints(
            [
                {
                    "id": "preserve_structure",
                    "type": "content_preservation",
                    "level": "hard",
                    "rule": "Do not change the article structure.",
                    "target": None,
                    "value": None,
                },
                {
                    "id": "no_new_sections",
                    "type": "content_addition",
                    "level": "hard",
                    "rule": "Do not add new sections.",
                    "target": None,
                    "value": None,
                },
                {
                    "id": "preserve_tone",
                    "type": "style_preservation",
                    "level": "hard",
                    "rule": "Preserve the author's original tone.",
                    "target": None,
                    "value": None,
                },
            ],
            summary="Preserve the original article structure and tone.",
        ),
        acceptance_criteria_json=canonical_acceptance_criteria(
            [
                {
                    "id": "valid_markdown",
                    "type": "format",
                    "level": "hard",
                    "description": "Output must remain valid markdown.",
                    "measurement": None,
                    "threshold": None,
                },
                {
                    "id": "grammar_corrected",
                    "type": "quality",
                    "level": "hard",
                    "description": "Grammar errors should be corrected.",
                    "measurement": None,
                    "threshold": None,
                },
                {
                    "id": "meaning_preserved",
                    "type": "semantic",
                    "level": "hard",
                    "description": "Original meaning must remain unchanged.",
                    "measurement": None,
                    "threshold": None,
                },
            ],
            summary="Output should be clean, readable, and faithful to the original.",
        ),
        expected_family="single_service",
        expected_completion_state=None,
        expected_clarification=False,
        domain="content_editing",
        interactivity_level="low",
        llm_analysis_behavior="repair_success",
    ),
    GoldenRequirementCase(
        key="csv_sales_report_generator",
        title="CSV Sales Report Generator",
        goal="Convert raw sales CSV data into a summarized report showing top products, revenue trends, and regional breakdown.",
        input_description="A CSV file containing transaction records with fields: product_id, region, quantity, revenue, timestamp.",
        output_description="A markdown report summarizing top products, revenue by region, and weekly revenue trends.",
        constraints_json=canonical_constraints(
            [
                {
                    "id": "input_integrity",
                    "type": "input_integrity",
                    "level": "hard",
                    "rule": "Input CSV data must not be modified.",
                    "target": "input_csv",
                    "value": None,
                },
                {
                    "id": "calculation_scope",
                    "type": "calculation_scope",
                    "level": "hard",
                    "rule": "All calculations must be derived only from the provided CSV.",
                    "target": "report_generation",
                    "value": None,
                },
            ],
            summary="Reporting must be based only on the input CSV.",
        ),
        acceptance_criteria_json=canonical_acceptance_criteria(
            [
                {
                    "id": "top_products_section",
                    "type": "completeness",
                    "level": "hard",
                    "description": "The report must include a top 10 products section.",
                    "measurement": None,
                    "threshold": None,
                },
                {
                    "id": "regional_breakdown_section",
                    "type": "completeness",
                    "level": "hard",
                    "description": "The report must include a revenue by region section.",
                    "measurement": None,
                    "threshold": None,
                },
                {
                    "id": "weekly_trend_section",
                    "type": "completeness",
                    "level": "hard",
                    "description": "The report must include a weekly revenue trend section.",
                    "measurement": None,
                    "threshold": None,
                },
                {
                    "id": "statistical_accuracy",
                    "type": "quality",
                    "level": "hard",
                    "description": "Aggregated statistics must accurately reflect the input CSV.",
                    "measurement": None,
                    "threshold": None,
                },
            ],
            summary="The report must contain all required sections and accurate statistics.",
        ),
        expected_family="workflow",
        expected_completion_state=None,
        expected_clarification=False,
        domain="sales_analytics",
        interactivity_level="medium",
    ),
    GoldenRequirementCase(
        key="weekly_ai_research_digest",
        title="Weekly AI Research Digest",
        goal="Collect important AI research updates and summarize them into a weekly digest for engineers.",
        input_description="Recent AI-related articles from the web, including arXiv papers, AI lab blogs, and major product release posts.",
        output_description="A weekly markdown digest with sections for research highlights, notable releases, and commentary.",
        constraints_json=canonical_constraints(
            [
                {
                    "id": "source_quality",
                    "type": "source_quality",
                    "level": "hard",
                    "rule": "Sources must be credible and directly relevant to AI research or engineering.",
                    "target": "source_selection",
                    "value": None,
                },
                {
                    "id": "duplication_control",
                    "type": "content_preservation",
                    "level": "soft",
                    "rule": "Avoid repeating the same article across weekly digests.",
                    "target": "digest_output",
                    "value": None,
                },
                {
                    "id": "weekly_schedule",
                    "type": "schedule",
                    "level": "soft",
                    "rule": "This workflow is intended to run weekly.",
                    "target": "runtime_schedule",
                    "value": "weekly",
                },
            ],
            summary="Use credible sources and avoid repeated content.",
        ),
        acceptance_criteria_json=canonical_acceptance_criteria(
            [
                {
                    "id": "digest_sections_present",
                    "type": "completeness",
                    "level": "hard",
                    "description": "The digest must contain research highlights, notable releases, and commentary sections.",
                    "measurement": None,
                    "threshold": None,
                },
                {
                    "id": "source_link_included",
                    "type": "format",
                    "level": "hard",
                    "description": "Each digest item must include a source link.",
                    "measurement": None,
                    "threshold": None,
                },
                {
                    "id": "engineer_readable",
                    "type": "quality",
                    "level": "soft",
                    "description": "The digest should be concise and informative for an engineering audience.",
                    "measurement": None,
                    "threshold": None,
                },
            ],
            summary="The digest must be complete, concise, and source-backed.",
        ),
        expected_family="workflow",
        expected_completion_state="compilable_with_warnings",
        expected_clarification=True,
        interactivity_level="medium",
        recurrence_mode="weekly",
        clarification_answers={
            "domain": "research_engineering",
            "external_tools_needed_json": ["web_search", "rss_reader", "arxiv_fetcher"],
        },
        expected_recompiled_completion_state="compilable_with_warnings",
        expected_llm_recompiled_completion_state="compilable_with_warnings",
    ),
    GoldenRequirementCase(
        key="company_news_monitor",
        title="Company News Monitor",
        goal="Track important news about selected companies.",
        input_description="News articles from the internet.",
        output_description="Important updates about companies.",
        constraints_json=canonical_constraints(
            [],
            summary="No additional constraints provided.",
        ),
        acceptance_criteria_json=canonical_acceptance_criteria(
            [
                {
                    "id": "relevance",
                    "type": "quality",
                    "level": "hard",
                    "description": "Returned updates should be relevant to the monitored companies.",
                    "measurement": None,
                    "threshold": None,
                }
            ],
            summary="Returned updates should be relevant.",
        ),
        expected_completion_state="clarification_required",
        expected_clarification=True,
        clarification_answers={
            "domain": "company_intelligence",
            "recurrence_mode": "daily",
            "external_tools_needed_json": ["news_api", "web_search"],
        },
        expected_recompiled_family="workflow",
        expected_recompiled_completion_state="compilable_with_warnings",
        expected_llm_recompiled_completion_state="compilable_with_warnings",
        llm_family_behavior="fallback_invalid",
    ),
    GoldenRequirementCase(
        key="customer_support_response_draft_generator",
        title="Customer Support Response Draft Generator",
        goal="Generate draft responses to customer support tickets that must be reviewed by a human before sending.",
        input_description="Customer support ticket text submitted by end users.",
        output_description="A draft response message suitable for review by a human support operator.",
        constraints_json=canonical_constraints(
            [
                {
                    "id": "professional_tone",
                    "type": "tone_policy",
                    "level": "hard",
                    "rule": "Responses must be polite and professional.",
                    "target": "response_text",
                    "value": None,
                },
                {
                    "id": "no_auto_send",
                    "type": "automation_boundary",
                    "level": "hard",
                    "rule": "Responses must never be sent automatically.",
                    "target": "delivery",
                    "value": False,
                },
            ],
            summary="Responses must remain draft-only and professional.",
        ),
        acceptance_criteria_json=canonical_acceptance_criteria(
            [
                {
                    "id": "draft_only",
                    "type": "policy",
                    "level": "hard",
                    "description": "The system must produce a draft response only.",
                    "measurement": None,
                    "threshold": None,
                },
                {
                    "id": "human_review_required",
                    "type": "human_review",
                    "level": "hard",
                    "description": "Every generated response must be reviewed by a human operator.",
                    "measurement": None,
                    "threshold": True,
                },
                {
                    "id": "professional_output",
                    "type": "quality",
                    "level": "hard",
                    "description": "The response must maintain a professional tone.",
                    "measurement": None,
                    "threshold": None,
                },
            ],
            summary="Every output must be human reviewed before use.",
        ),
        expected_family="workflow",
        expected_completion_state=None,
        expected_clarification=False,
        expected_human_review_signal=True,
        domain="support_operations",
        interactivity_level="medium",
        human_review_required=True,
        external_tools_needed_json={"items": ["ticketing_system", "knowledge_base"]},
    ),
    GoldenRequirementCase(
        key="startup_pitch_deck_reviewer",
        title="Startup Pitch Deck Reviewer",
        goal="Evaluate a startup pitch deck from different perspectives including product, market, and financial analysis.",
        input_description="A startup pitch deck provided as a PDF file.",
        output_description="A structured evaluation report with separate feedback sections.",
        constraints_json=canonical_constraints(
            [
                {
                    "id": "perspective_separation",
                    "type": "perspective_separation",
                    "level": "hard",
                    "rule": "Each perspective must provide independent feedback.",
                    "target": "evaluation_sections",
                    "value": ["product", "market", "financial"],
                },
                {
                    "id": "deck_only_scope",
                    "type": "input_scope",
                    "level": "hard",
                    "rule": "Analysis must be based on the provided pitch deck.",
                    "target": "source_material",
                    "value": "pitch_deck_pdf",
                },
            ],
            summary="Evaluation must stay grounded in the deck and separated by perspective.",
        ),
        acceptance_criteria_json=canonical_acceptance_criteria(
            [
                {
                    "id": "product_section",
                    "type": "completeness",
                    "level": "hard",
                    "description": "The final report must contain a product evaluation section.",
                    "measurement": None,
                    "threshold": None,
                },
                {
                    "id": "market_section",
                    "type": "completeness",
                    "level": "hard",
                    "description": "The final report must contain a market evaluation section.",
                    "measurement": None,
                    "threshold": None,
                },
                {
                    "id": "financial_section",
                    "type": "completeness",
                    "level": "hard",
                    "description": "The final report must contain a financial evaluation section.",
                    "measurement": None,
                    "threshold": None,
                },
            ],
            summary="The final report must cover all key perspectives.",
        ),
        expected_family="multi_agent",
        expected_completion_state=None,
        expected_clarification=False,
        domain="investment_review",
        interactivity_level="collaborative",
        human_review_required=True,
    ),
]


class _SequentialFakeAdapter:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses

    def chat(self, messages):  # noqa: ANN001
        if not self.responses:
            raise AssertionError(f"Unexpected provider call: {messages}")
        content = self.responses.pop(0)
        return ProviderResponse(
            content=content,
            prompt_tokens=32,
            completion_tokens=48,
            total_tokens=80,
            raw_metadata={"mode": "real", "source": "golden_validation"},
        )


@contextmanager
def patched_fake_compiler_provider(responses: list[str]):
    queued_responses = list(responses)

    def fake_get_provider_adapter(model_config=None, overrides=None):  # noqa: ANN001, ARG001
        return _SequentialFakeAdapter(queued_responses), ResolvedModelConfig(
            provider="openai-compatible",
            model_name="golden-validation-model",
            endpoint="https://example.test/v1",
            api_key="test-key",
            temperature=0.2,
            max_tokens=512,
            source="golden-validation",
            llm_mode="real",
        )

    with patch.object(requirement_compiler_service.settings, "llm_mode", "real"), patch.object(
        requirement_compiler_service, "get_provider_adapter", fake_get_provider_adapter
    ), patch.object(compiler_llm_assisted, "get_provider_adapter", fake_get_provider_adapter):
        yield


def build_requirement_payload(case: GoldenRequirementCase, project_id: str) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "title": case.title,
        "goal": case.goal,
        "input_description": case.input_description,
        "output_description": case.output_description,
        "constraints_json": case.constraints_json,
        "acceptance_criteria_json": case.acceptance_criteria_json,
        "domain": case.domain,
        "interactivity_level": case.interactivity_level,
        "human_review_required": case.human_review_required,
        "recurrence_mode": case.recurrence_mode,
        "external_tools_needed_json": case.external_tools_needed_json,
    }


def execute_validation_suite(client) -> dict[str, Any]:  # noqa: ANN001
    project = create_project(client)
    runs: list[dict[str, Any]] = []

    for case in GOLDEN_REQUIREMENT_CASES:
        requirement_response = client.post("/api/v1/requirements", json=build_requirement_payload(case, project["id"]))
        assert requirement_response.status_code == 201, requirement_response.text
        requirement = requirement_response.json()

        deterministic_payload = _compile_case(client, requirement["id"], mode="deterministic", case=case)
        runs.append(
            _record_run(
                case=case,
                mode="deterministic",
                operation="compile",
                payload=deterministic_payload,
                used_mock_llm=False,
            )
        )

        llm_responses = build_mock_compiler_responses(case, operation="compile")
        with patched_fake_compiler_provider(llm_responses):
            llm_payload = _compile_case(client, requirement["id"], mode="llm_assisted", case=case)
        runs.append(
            _record_run(
                case=case,
                mode="llm_assisted",
                operation="compile",
                payload=llm_payload,
                used_mock_llm=True,
            )
        )

        if case.clarification_answers:
            deterministic_requirement_response = client.post(
                "/api/v1/requirements",
                json=build_requirement_payload(case, project["id"]),
            )
            assert deterministic_requirement_response.status_code == 201, deterministic_requirement_response.text
            deterministic_requirement = deterministic_requirement_response.json()
            _compile_case(client, deterministic_requirement["id"], mode="deterministic", case=case)
            deterministic_recompile = _recompile_case(client, deterministic_requirement["id"], case=case, mode="deterministic")
            if deterministic_recompile is not None:
                runs.append(
                    _record_run(
                        case=case,
                        mode="deterministic",
                        operation="recompile",
                        payload=deterministic_recompile,
                        used_mock_llm=False,
                    )
                )
            llm_requirement_response = client.post(
                "/api/v1/requirements",
                json=build_requirement_payload(case, project["id"]),
            )
            assert llm_requirement_response.status_code == 201, llm_requirement_response.text
            llm_requirement = llm_requirement_response.json()
            with patched_fake_compiler_provider(build_mock_compiler_responses(case, operation="compile")):
                _compile_case(client, llm_requirement["id"], mode="llm_assisted", case=case)
            llm_recompile = _recompile_case(client, llm_requirement["id"], case=case, mode="llm_assisted")
            if llm_recompile is not None:
                runs.append(
                    _record_run(
                        case=case,
                        mode="llm_assisted",
                        operation="recompile",
                        payload=llm_recompile,
                        used_mock_llm=True,
                    )
                )

    return build_validation_report(runs)


def build_validation_report(runs: list[dict[str, Any]]) -> dict[str, Any]:
    total_runs = len(runs)
    stage_metrics: dict[str, dict[str, Any]] = {}
    fallback_events_total = sum(len(run["fallback_events"]) for run in runs)
    fallback_runs_count = sum(1 for run in runs if run["fallback_events"])
    repair_attempts = 0
    repair_successes = 0
    failure_reason_index: dict[tuple[str, str], list[str]] = defaultdict(list)

    for stage in VALIDATION_STAGES:
        successes = 0
        for run in runs:
            diagnostic = next(item for item in run["stage_diagnostics"] if item["stage"] == stage)
            if diagnostic["success"]:
                successes += 1
            if diagnostic["repair_attempted"]:
                repair_attempts += 1
            if diagnostic["repair_succeeded"]:
                repair_successes += 1
            for reason in diagnostic["fallback_reasons"]:
                failure_reason_index[(stage, reason)].append(run["run_key"])
        stage_metrics[stage] = {
            "executions": total_runs,
            "success_count": successes,
            "success_rate": round(successes / total_runs, 4) if total_runs else 0.0,
        }

    per_case: dict[str, dict[str, Any]] = {}
    for case in GOLDEN_REQUIREMENT_CASES:
        case_runs = [run for run in runs if run["case_key"] == case.key]
        per_case[case.key] = {
            "title": case.title,
            "expected_family": case.expected_family,
            "expected_completion_state": case.expected_completion_state,
            "runs": case_runs,
        }

    failure_reasons = [
        {
            "stage": stage,
            "reason": reason,
            "count": len(run_keys),
            "run_keys": sorted(run_keys),
        }
        for (stage, reason), run_keys in sorted(failure_reason_index.items())
    ]

    report = {
        "report_version": REPORT_VERSION,
        "total_runs": total_runs,
        "mocked_llm_runs": sum(1 for run in runs if run["used_mock_llm"]),
        "metrics": {
            "analyzer_validation": stage_metrics["requirement_analysis"],
            "family_recommendation": stage_metrics["family_recommendation"],
            "filler_validation": stage_metrics["skeleton_slot_filling"],
            "repair_attempts_count": repair_attempts,
            "repair_success_count": repair_successes,
            "repair_success_rate": round(repair_successes / repair_attempts, 4) if repair_attempts else 0.0,
            "deterministic_fallback_events_count": fallback_events_total,
            "deterministic_fallback_events_rate": round(fallback_events_total / total_runs, 4) if total_runs else 0.0,
            "fallback_runs_count": fallback_runs_count,
            "fallback_runs_rate": round(fallback_runs_count / total_runs, 4) if total_runs else 0.0,
        },
        "per_case": per_case,
        "failure_reasons": failure_reasons,
        "runs": runs,
    }
    return report


def render_markdown_report(report: dict[str, Any]) -> str:
    lines = [
        "# Golden Compiler Reliability Report",
        "",
        f"- total runs: {report['total_runs']}",
        f"- mocked llm runs: {report['mocked_llm_runs']}",
        f"- analyzer validation success: {report['metrics']['analyzer_validation']['success_count']}/{report['metrics']['analyzer_validation']['executions']} ({report['metrics']['analyzer_validation']['success_rate']:.2%})",
        f"- family recommendation success: {report['metrics']['family_recommendation']['success_count']}/{report['metrics']['family_recommendation']['executions']} ({report['metrics']['family_recommendation']['success_rate']:.2%})",
        f"- filler validation success: {report['metrics']['filler_validation']['success_count']}/{report['metrics']['filler_validation']['executions']} ({report['metrics']['filler_validation']['success_rate']:.2%})",
        f"- repair attempts: {report['metrics']['repair_attempts_count']}",
        f"- repair successes: {report['metrics']['repair_success_count']} ({report['metrics']['repair_success_rate']:.2%})",
        f"- fallback events: {report['metrics']['deterministic_fallback_events_count']} ({report['metrics']['deterministic_fallback_events_rate']:.2%} per run)",
        f"- fallback runs: {report['metrics']['fallback_runs_count']} ({report['metrics']['fallback_runs_rate']:.2%} of runs)",
        "",
        "## Per-Case Summary",
        "",
    ]

    for case_key, case_summary in report["per_case"].items():
        lines.append(f"### {case_summary['title']}")
        lines.append("")
        for run in case_summary["runs"]:
            lines.append(
                f"- `{run['mode']}` `{run['operation']}`: family=`{run['selected_family']}`, completion=`{run['completion_state']}`, "
                f"draft_readiness=`{run['draft_readiness_status']}`, validation_valid=`{str(run['validation_valid']).lower()}`, "
                f"clarification_triggered=`{str(run['clarification_triggered']).lower()}`, fallback_count={len(run['fallback_events'])}"
            )
        lines.append("")

    if report["failure_reasons"]:
        lines.append("## Failure Reasons")
        lines.append("")
        for item in report["failure_reasons"]:
            lines.append(
                f"- stage=`{item['stage']}` reason=`{item['reason']}` count={item['count']} runs={', '.join(item['run_keys'])}"
            )
        lines.append("")

    return "\n".join(lines)


def build_mock_compiler_responses(case: GoldenRequirementCase, *, operation: str) -> list[str]:
    responses: list[str] = []

    if case.llm_analysis_behavior == "repair_success" and operation == "compile":
        responses.append("not valid json")
        responses.append(json.dumps(_analysis_payload(case)))
    else:
        responses.append(json.dumps(_analysis_payload(case)))

    responses.append(json.dumps(_semantic_judgement_payload(case, operation=operation)))

    if case.llm_family_behavior == "fallback_invalid" and operation == "compile":
        responses.append(json.dumps({"recommended_family": "invalid_family", "confidence": 80, "rationale": "invalid"}))
        responses.append(json.dumps({"recommended_family": "still_invalid", "confidence": 80, "rationale": "still invalid"}))
    else:
        responses.append(json.dumps(_family_payload(case, operation=operation)))

    responses.append(json.dumps(_problem_formulation_payload(case, operation=operation)))
    responses.append(json.dumps(_step_plan_payload(case, operation=operation)))
    responses.append(json.dumps(_slot_fill_payload(case, operation=operation)))
    return responses


def _problem_formulation_payload(case: GoldenRequirementCase, *, operation: str) -> dict[str, Any]:
    del operation
    text = " ".join(filter(None, [case.goal, case.input_description, case.output_description, case.domain or ""])).lower()
    target_object = "generic_request"
    if any(token in text for token in ("repository", "repo", "仓库", "代码仓库")):
        target_object = "repository"
    elif any(token in text for token in ("dataset", "csv", "spreadsheet", "xlsx", "数据集", "表格")):
        target_object = "dataset"
    elif any(token in text for token in ("web", "article", "blog", "paper", "briefing", "research", "网页")):
        target_object = "web_resource"
    return {
        "interpreted_problem_kind": f"{target_object}_bounded_problem",
        "target_object": target_object,
        "current_system_fit": "in_scope" if target_object != "generic_request" else "partially_in_scope",
        "planning_intent_hints": {
            "objective_family": "analysis" if target_object in {"repository", "dataset"} else "research" if target_object == "web_resource" else "generic",
            "preferred_graph_shape": "dag" if case.expected_family in {"workflow", "multi_agent"} else "linear",
            "parallelism_hint": "preferred" if case.expected_family in {"workflow", "multi_agent"} else "none",
            "acquisition_strategy": "checkout_then_inventory" if target_object == "repository" else "search_then_collect" if target_object == "web_resource" else "fetch_then_ingest" if target_object == "dataset" else "unknown",
            "evidence_strategy": "repository_evidence" if target_object == "repository" else "web_evidence" if target_object == "web_resource" else "dataset_evidence" if target_object == "dataset" else "generic",
            "side_effect_budget": "read_mostly",
            "required_capability_themes": [],
            "reasoning_summary": "Golden validation problem formulation candidate.",
        },
        "required_decisions": [],
        "blocking_axes": [],
        "review_axes": [],
        "blocking_reasons": [],
        "reasoning_summary": "Golden validation problem formulation candidate.",
    }


def _requires_semantic_judgement(case: GoldenRequirementCase) -> bool:
    text = " ".join(
        filter(
            None,
            [
                case.domain or "",
                case.goal,
                case.input_description,
                case.output_description,
            ],
        )
    ).lower()
    data_tokens = ("csv", "excel", "spreadsheet", "xlsx", "structured data", "sales_analytics")
    return any(token in text for token in data_tokens)


def _semantic_judgement_payload(case: GoldenRequirementCase, *, operation: str) -> dict[str, Any]:
    if not _requires_semantic_judgement(case):
        return {
            "status": "no_changes",
            "refined_global_core": None,
            "refined_domain_packs": None,
            "candidate_skeleton_ids": [],
            "dominant_skeleton_id": None,
            "clarification_axes": [],
            "semantic_uncertainties": [],
            "judgement_confidence": 0,
            "reasoning_summary": "No bounded semantic judgement refinement needed for this case.",
        }

    family = case.expected_recompiled_family if operation == "recompile" and case.expected_recompiled_family else case.expected_family or "single_service"
    lower_text = " ".join([case.goal, case.input_description, case.output_description]).lower()
    is_validation = any(token in lower_text for token in ("validate", "schema", "required field", "required column"))
    is_comparison = any(token in lower_text for token in ("compare", "comparison", "across"))
    dominant = "dataset_summary_report"
    if is_validation:
        dominant = "dataset_validation_report"
    elif is_comparison:
        dominant = "dataset_comparison_report"
    if family != "workflow":
        dominant = "dataset_summary_report"

    validation_need = "schema" if dominant == "dataset_validation_report" else "none"
    aggregation_mode = "compare" if dominant == "dataset_comparison_report" else "group"
    analysis_frame = "audit" if dominant == "dataset_validation_report" else "summary"
    objective = "validate" if dominant == "dataset_validation_report" else "synthesize"

    return {
        "status": "judged",
        "refined_global_core": {
            "target_object": "dataset",
            "primary_objective": objective,
            "workflow_shape": "bounded_workflow",
            "source_modality": "local_path",
            "deliverable_form": "report",
            "analysis_frame": analysis_frame,
            "decision_posture": "descriptive",
            "evidence_basis": ["structured_data"],
            "ambiguities": [],
            "confidence": 83,
        },
        "refined_domain_packs": {
            "data_pack": {
                "data_source_kind": "csv",
                "aggregation_mode": aggregation_mode,
                "metric_focus": "explicit",
                "output_granularity": "grouped",
                "validation_need": validation_need,
            }
        },
        "candidate_skeleton_ids": [dominant],
        "dominant_skeleton_id": dominant,
        "clarification_axes": [],
        "semantic_uncertainties": [],
        "judgement_confidence": 82,
        "reasoning_summary": f"Structured-data prompt resolved to the bounded data skeleton '{dominant}'.",
    }


def _compile_case(client, requirement_id: str, *, mode: str, case: GoldenRequirementCase) -> dict[str, Any]:  # noqa: ANN001
    response = client.post(
        f"/api/v1/requirements/{requirement_id}/compile",
        json={
            "persist_draft": True,
            "compiler_mode": mode,
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    _assert_expected_outcome(case, payload, operation="compile", mode=mode)
    return payload


def _recompile_case(client, requirement_id: str, *, case: GoldenRequirementCase, mode: str) -> dict[str, Any] | None:  # noqa: ANN001
    clarification_state = client.get(f"/api/v1/requirements/{requirement_id}/clarifications")
    assert clarification_state.status_code == 200, clarification_state.text
    question_ids = {
        item["target_field"]: item["id"]
        for item in clarification_state.json()["items"]
        if item["target_field"] in case.clarification_answers
    }
    if not question_ids:
        return None
    submit_payload = {
        "answers": [
            {"id": question_ids[target_field], "answer": answer}
            for target_field, answer in case.clarification_answers.items()
            if target_field in question_ids
        ]
    }
    submit_response = client.post(
        f"/api/v1/requirements/{requirement_id}/clarifications/answers",
        json=submit_payload,
    )
    assert submit_response.status_code == 200, submit_response.text

    if mode == "llm_assisted":
        with patched_fake_compiler_provider(build_mock_compiler_responses(case, operation="recompile")):
            response = client.post(
                f"/api/v1/requirements/{requirement_id}/recompile",
                json={"persist_draft": True, "compiler_mode": mode, "use_clarifications": True},
            )
    else:
        response = client.post(
            f"/api/v1/requirements/{requirement_id}/recompile",
            json={"persist_draft": True, "compiler_mode": mode, "use_clarifications": True},
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    _assert_expected_outcome(case, payload, operation="recompile", mode=mode)
    return payload


def _assert_expected_outcome(case: GoldenRequirementCase, payload: dict[str, Any], *, operation: str, mode: str) -> None:
    expected_family = case.expected_recompiled_family if operation == "recompile" and case.expected_recompiled_family else case.expected_family
    if operation == "recompile" and mode == "llm_assisted":
        expected_completion = case.expected_llm_recompiled_completion_state
    else:
        expected_completion = (
            case.expected_recompiled_completion_state
            if operation == "recompile" and case.expected_recompiled_completion_state
            else case.expected_completion_state
        )

    if expected_family is not None:
        assert payload["selected_blueprint_type"] == expected_family, (case.key, mode, operation, payload)
    if expected_completion is not None:
        assert payload["completion_state"] == expected_completion, (case.key, mode, operation, payload)

    assert payload["validation"]["valid"] is True, (case.key, mode, operation, payload)
    assert payload["normalized_requirement"]["constraints"]["version"] == "1.0"
    assert payload["normalized_requirement"]["acceptance_criteria"]["version"] == "1.0"

    if case.expected_clarification is True and operation == "compile":
        assert payload["clarification_summary"]["items"], (case.key, mode, operation, payload)
    if case.expected_completion_state == "clarification_required" and operation == "compile":
        assert payload["draft_readiness_status"] == "blocked_by_clarification"
    if case.expected_completion_state == "ready_to_compile" and operation == "compile":
        assert payload["draft_readiness_status"] == "ready_for_build"
    if case.expected_human_review_signal is True:
        assert payload["compiler_metadata"]["human_review_required"] is True, (case.key, mode, operation, payload)


def _record_run(
    *,
    case: GoldenRequirementCase,
    mode: str,
    operation: str,
    payload: dict[str, Any],
    used_mock_llm: bool,
) -> dict[str, Any]:
    stage_diagnostics = [_stage_diagnostic(payload, stage) for stage in VALIDATION_STAGES]
    return {
        "run_key": f"{case.key}:{mode}:{operation}",
        "case_key": case.key,
        "title": case.title,
        "mode": mode,
        "operation": operation,
        "used_mock_llm": used_mock_llm,
        "completion_state": payload["completion_state"],
        "selected_family": payload["selected_blueprint_type"],
        "draft_readiness_status": payload["draft_readiness_status"],
        "validation_valid": payload["validation"]["valid"],
        "clarification_triggered": bool(payload["clarification_summary"]["items"]),
        "clarifications_used": bool(payload["clarification_summary"]["used_answer_ids"]),
        "llm_assistance_used": payload.get("llm_assistance_used", False),
        "fallback_events": payload.get("fallback_events", []),
        "validation_outcomes": payload.get("validation_outcomes", []),
        "repair_applied": payload.get("repair_applied", False),
        "compiler_status": payload["compiler_status"],
        "stage_diagnostics": stage_diagnostics,
    }


def _stage_diagnostic(payload: dict[str, Any], stage: str) -> dict[str, Any]:
    outcomes = [item for item in payload.get("validation_outcomes", []) if item["stage"] == stage]
    fallback_events = [item for item in payload.get("fallback_events", []) if item["stage"] == stage]
    repair_attempted = any(
        item["message"].startswith("Initial LLM output") or item["message"].startswith("Repair output")
        for item in outcomes
    )
    repair_succeeded = any(item["status"] == "repaired" for item in outcomes)
    accepted = any(item["status"] in {"accepted", "repaired", "skipped"} for item in outcomes) or not outcomes
    success = accepted and not fallback_events
    return {
        "stage": stage,
        "success": success,
        "validation_statuses": [item["status"] for item in outcomes],
        "failure_messages": [item["message"] for item in outcomes if item["status"] == "rejected"],
        "fallback_reasons": [item["reason"] for item in fallback_events],
        "repair_attempted": repair_attempted,
        "repair_succeeded": repair_succeeded,
    }


def _analysis_payload(case: GoldenRequirementCase) -> dict[str, Any]:
    tools = (case.external_tools_needed_json or {}).get("items") if isinstance(case.external_tools_needed_json, dict) else []
    recommended_family = case.expected_recompiled_family or case.expected_family or "single_service"
    return {
        "domain": case.domain,
        "complexity": "medium",
        "expected_artifacts": ["workflow_report.json" if recommended_family == "workflow" else "service_output.json"],
        "interactivity_level": case.interactivity_level,
        "requires_external_tools": bool(tools) or any(token in case.input_description.lower() for token in ("web", "internet", "news")),
        "requires_human_review": case.human_review_required,
        "recurrence_mode": case.recurrence_mode,
        "knowledge_scope": "bounded",
        "recommended_family": recommended_family,
        "confidence": 84,
        "unresolved_questions": ["Which source connectors should be enabled?"] if case.expected_clarification else [],
    }


def _family_payload(case: GoldenRequirementCase, *, operation: str) -> dict[str, Any]:
    family = case.expected_recompiled_family if operation == "recompile" and case.expected_recompiled_family else case.expected_family or "single_service"
    return {
        "recommended_family": family,
        "confidence": 82,
        "rationale": f"{family} matches the bounded requirement shape.",
    }


def _slot_fill_payload(case: GoldenRequirementCase, *, operation: str) -> dict[str, Any]:
    family = case.expected_recompiled_family if operation == "recompile" and case.expected_recompiled_family else case.expected_family or "single_service"
    artifact_name = {
        "single_service": "service_output.json",
        "workflow": "workflow_report.json",
        "multi_agent": "agent_review.json",
    }[family]
    step_name = {
        "single_service": "process_request",
        "workflow": "filter_and_process",
        "multi_agent": "specialist_agent",
    }[family]
    return {
        "blueprint_description": f"{case.title} compiled in {family} mode.",
        "input_schema_description": case.input_description,
        "output_schema_description": case.output_description,
        "step_fills": [
            {
                "step_name": step_name,
                "description": f"Execute the core transformation for {case.title.lower()}.",
                "purpose": "Preserve deterministic skeleton while filling descriptive slots.",
            }
        ],
        "artifact_fills": [
            {
                "artifact_name": artifact_name,
                "intent": "Primary compiled output",
                "description": f"Primary artifact for {case.title}.",
            }
        ],
        "variable_fills": [{"name": "domain", "hint": case.domain or "general"}],
        "assumptions": ["Inputs are available at runtime."],
        "warnings": [],
    }


def _step_plan_payload(case: GoldenRequirementCase, *, operation: str) -> dict[str, Any]:
    family = case.expected_recompiled_family if operation == "recompile" and case.expected_recompiled_family else case.expected_family or "single_service"
    if family == "workflow":
        steps = [
            {
                "step_id": "collect",
                "order": 1,
                "logical_step_kind": "ingest",
                "purpose": f"Collect inputs relevant to {case.title.lower()}.",
                "required_capabilities": ["fetch_url"] if case.external_tools_needed_json else [],
            },
            {
                "step_id": "transform",
                "order": 2,
                "logical_step_kind": "summarize",
                "purpose": f"Synthesize the workflow result for {case.title.lower()}.",
                "required_capabilities": ["summarize_text_llm"],
            },
            {
                "step_id": "persist",
                "order": 3,
                "logical_step_kind": "persist",
                "purpose": f"Write the final artifact package for {case.title.lower()}.",
                "required_capabilities": ["write_artifact"],
            },
        ]
    elif family == "multi_agent":
        steps = [
            {
                "step_id": "plan",
                "order": 1,
                "logical_step_kind": "review",
                "purpose": f"Coordinate specialist work for {case.title.lower()}.",
                "required_capabilities": ["human_review_gate"],
            },
            {
                "step_id": "specialize",
                "order": 2,
                "logical_step_kind": "summarize",
                "purpose": f"Produce the specialized synthesis for {case.title.lower()}.",
                "required_capabilities": ["summarize_text_llm"],
            },
            {
                "step_id": "validate",
                "order": 3,
                "logical_step_kind": "validate",
                "purpose": f"Validate the compiled result for {case.title.lower()}.",
                "required_capabilities": ["validate_schema"],
            },
        ]
    else:
        steps = [
            {
                "step_id": "ingest",
                "order": 1,
                "logical_step_kind": "transform",
                "purpose": f"Normalize the request payload for {case.title.lower()}.",
                "required_capabilities": ["parse_json"],
            },
            {
                "step_id": "core",
                "order": 2,
                "logical_step_kind": "summarize",
                "purpose": f"Execute the core single-service transformation for {case.title.lower()}.",
                "required_capabilities": ["summarize_text_llm"],
            },
            {
                "step_id": "emit",
                "order": 3,
                "logical_step_kind": "persist",
                "purpose": f"Write the single-service output for {case.title.lower()}.",
                "required_capabilities": ["write_artifact"],
            },
        ]
    return {
        "status": "planned",
        "confidence": 78,
        "proposed_steps": steps,
        "warnings": [],
        "ambiguities": [],
        "unresolved_points": [],
    }
