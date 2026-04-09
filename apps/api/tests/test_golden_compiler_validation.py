from __future__ import annotations

from tests.golden_requirement_validation_support import GOLDEN_REQUIREMENT_CASES, execute_validation_suite


def test_golden_compiler_validation_suite_is_stable(client):
    report = execute_validation_suite(client)

    assert report["total_runs"] == 16
    assert report["metrics"]["analyzer_validation"]["executions"] == 16
    assert report["metrics"]["family_recommendation"]["executions"] == 16
    assert report["metrics"]["filler_validation"]["executions"] == 16
    assert report["metrics"]["deterministic_fallback_events_count"] >= 1
    assert report["metrics"]["repair_attempts_count"] >= 1
    assert report["metrics"]["repair_success_count"] >= 1

    for case in GOLDEN_REQUIREMENT_CASES:
        case_runs = report["per_case"][case.key]["runs"]
        deterministic_compile = next(run for run in case_runs if run["mode"] == "deterministic" and run["operation"] == "compile")
        llm_compile = next(run for run in case_runs if run["mode"] == "llm_assisted" and run["operation"] == "compile")

        if case.expected_family is not None:
            assert deterministic_compile["selected_family"] == case.expected_family
        if case.expected_completion_state is not None:
            assert deterministic_compile["completion_state"] == case.expected_completion_state

        assert llm_compile["validation_valid"] is True
        assert llm_compile["llm_assistance_used"] is True

    company_news_recompile = next(
        run
        for run in report["per_case"]["company_news_monitor"]["runs"]
        if run["mode"] == "deterministic" and run["operation"] == "recompile"
    )
    assert company_news_recompile["selected_family"] == "workflow"
    assert company_news_recompile["completion_state"] == "compilable_with_warnings"
