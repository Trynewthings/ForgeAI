from __future__ import annotations

from collections.abc import Sequence

from fastapi.testclient import TestClient

import app.services.requirement.compiler.llm_assisted as compiler_llm_assisted
import app.services.requirement.requirement_compiler_service as requirement_compiler_service
from app.schemas.capability_registry import CapabilityGap, CapabilityUsageValidationResult
from app.services.provider_runtime import ProviderResponse, ResolvedModelConfig


def canonical_constraints(items=None, *, summary: str = "") -> dict:
    if isinstance(items, dict):
        if {"version", "summary", "items"} <= set(items.keys()):
            return items
        if "items" in items and isinstance(items["items"], list):
            summary = str(items.get("summary", summary))
            items = items["items"]
    normalized_items = []
    for index, item in enumerate(items or []):
        if isinstance(item, dict):
            normalized_items.append(item)
            continue
        normalized_items.append(
            {
                "id": f"constraint_{index + 1}",
                "type": "policy",
                "level": "hard",
                "rule": str(item),
                "target": None,
                "value": None,
            }
        )
    return {
        "version": "1.0",
        "summary": summary,
        "items": normalized_items,
    }


def canonical_acceptance_criteria(items=None, *, summary: str = "") -> dict:
    if isinstance(items, dict):
        if {"version", "summary", "items"} <= set(items.keys()):
            return items
        if "items" in items and isinstance(items["items"], list):
            summary = str(items.get("summary", summary))
            items = items["items"]
    normalized_items = []
    for index, item in enumerate(items or []):
        if isinstance(item, dict):
            normalized_items.append(item)
            continue
        normalized_items.append(
            {
                "id": f"criterion_{index + 1}",
                "type": "functional",
                "level": "hard",
                "description": str(item),
                "measurement": None,
                "threshold": None,
            }
        )
    return {
        "version": "1.0",
        "summary": summary,
        "items": normalized_items,
    }


def create_project(client: TestClient) -> dict:
    response = client.post(
        "/api/v1/projects",
        json={"name": "Compiler Controls Project", "description": "test project"},
    )
    assert response.status_code == 201
    return response.json()


def create_requirement(client: TestClient, project_id: str, **overrides) -> dict:
    acceptance_payload = canonical_acceptance_criteria(
        overrides.pop("acceptance_criteria_json", canonical_acceptance_criteria(["Output is returned"]))
    )
    constraints_payload = canonical_constraints(overrides.pop("constraints_json", canonical_constraints()))
    payload = {
        "project_id": project_id,
        "title": "Default Requirement",
        "goal": "Handle a single incoming task.",
        "input_description": "One task payload.",
        "output_description": "One response payload.",
        "constraints_json": constraints_payload,
        "acceptance_criteria_json": acceptance_payload,
        **overrides,
    }
    response = client.post("/api/v1/requirements", json=payload)
    assert response.status_code == 201
    return response.json()


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
            raw_metadata={"mode": "real", "source": "test"},
        )


def install_fake_compiler_provider(
    monkeypatch,
    responses: Sequence[str],
    *,
    endpoint: str = "https://example.test/v1",
    api_key: str | None = "test-key",
) -> None:
    queued_responses = list(responses)

    def fake_get_provider_adapter(model_config=None, overrides=None):  # noqa: ANN001, ARG001
        return _SequentialFakeAdapter(queued_responses), ResolvedModelConfig(
            provider="openai-compatible",
            model_name="test-model",
            endpoint=endpoint,
            api_key=api_key,
            temperature=0.2,
            max_tokens=512,
            source="test",
            llm_mode="real",
        )

    monkeypatch.setattr(requirement_compiler_service.settings, "llm_mode", "real")
    monkeypatch.setattr(requirement_compiler_service, "get_provider_adapter", fake_get_provider_adapter)
    monkeypatch.setattr(compiler_llm_assisted, "get_provider_adapter", fake_get_provider_adapter)


def test_compile_result_includes_decision_trace_and_draft_strategy(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Weekly Research Digest",
        goal="Collect and summarize research every week.",
        input_description="Theme and sources.",
        output_description="A weekly markdown and JSON report.",
        domain="research",
        interactivity_level="medium",
        recurrence_mode="weekly",
        external_tools_needed_json={"items": ["rss", "search"]},
        acceptance_criteria_json={"items": ["Report includes top findings"]},
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["selection_reason"] in {
        "Recurring execution maps best to a workflow skeleton.",
        "Semantic interpretation identified workflow shape 'bounded_workflow', which requires a bounded workflow graph.",
    }
    assert payload["draft_strategy"]["action"] == "created_new_compiler_draft"
    assert payload["draft_strategy"]["reused_existing_draft"] is False
    assert payload["decision_trace"]["normalized_requirement_summary"]["recurrence_mode"] == "weekly"
    assert payload["decision_trace"]["family_selection"]["selected_family"] == "workflow"
    assert payload["decision_trace"]["skeleton"]["factory_key"] == "deterministic.workflow.v1"
    assert payload["decision_trace"]["template_selection"]["selected_template_id"] == "workflow.research_briefing"
    assert payload["decision_trace"]["template_selection"]["selected_family"] == "workflow"
    assert payload["decision_trace"]["validation_summary"]["valid"] is True
    assert payload["decision_trace"]["repair_summary"]["applied"] is False
    assert any(signal["key"] == "recurring_execution" and signal["matched"] for signal in payload["decision_trace"]["heuristic_signals"])
    assert payload["blueprint_draft"]["template_tags_json"]["template_id"] == "workflow.research_briefing"
    assert payload["blueprint_draft"]["template_tags_json"]["template_source"] == "registry"
    assert payload["explainability"]["family"]["selected_family"] == "workflow"
    assert payload["explainability"]["family"]["selection_reason"] == payload["selection_reason"]
    assert payload["explainability"]["template"]["selected_template_id"] == "workflow.research_briefing"
    assert payload["explainability"]["readiness"]["completion_state"] in {"ready_to_compile", "compilable_with_warnings"}
    assert payload["explainability"]["clarification"]["outstanding_clarification_count"] == payload["clarification_summary"]["outstanding_count"]
    assert payload["explainability"]["warnings"]["warning_count"] == len(payload["warnings"])
    assert payload["explainability"]["presentation"]["summary"]["stage"] == "compile"
    assert payload["explainability"]["presentation"]["summary"]["selected_family"] == "workflow"
    assert any(section["section_key"] == "blueprint_summary" for section in payload["explainability"]["presentation"]["sections"])
    assert payload["explainability"]["blueprint"]["structure_source"] == "typed_workflow_proposal"
    assert payload["explainability"]["blueprint"]["template_overlay_applied"] is True
    assert payload["explainability"]["blueprint"]["slot_fill_applied"] is False
    assert any(
        item["section"] == "execution_steps_json.steps" and item["source"] == "typed_workflow_proposal"
        for item in payload["explainability"]["blueprint"]["section_provenance"]
    )
    assert payload["explainability"]["next_actions"]
    assert [step["step_name"] for step in payload["blueprint_draft"]["execution_steps_json"]["steps"]] == [
        "collect_research_sources",
        "synthesize_research_briefing",
        "render_research_briefing",
        "persist_research_briefing",
    ]

    inspect_response = client.get(f"/api/v1/requirements/{requirement['id']}/compile-result")
    assert inspect_response.status_code == 200
    assert inspect_response.json()["decision_trace"]["skeleton"]["factory_key"] == "deterministic.workflow.v1"
    assert inspect_response.json()["decision_trace"]["template_selection"]["selected_template_id"] == "workflow.research_briefing"


def test_compile_family_hint_can_promote_default_family_when_override_allowed(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(client, project["id"], title="Support Reply Service", domain="support", interactivity_level="low")

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={
            "persist_draft": True,
            "family_hint": "workflow",
            "allow_family_override": True,
        },
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["family_selection"]["recommended_family"] == "single_service"
    assert payload["family_selection"]["selected_family"] == "workflow"
    assert payload["family_selection"]["override_applied"] is True
    assert "family_hint promoted the draft to workflow" in payload["selection_reason"]


def test_compile_family_hint_can_be_locked_against_heuristic_recommendation(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Weekly Research Digest",
        goal="Collect and summarize research every week.",
        input_description="Theme and sources.",
        output_description="A weekly report artifact.",
        domain="research",
        interactivity_level="medium",
        recurrence_mode="weekly",
        external_tools_needed_json={"items": ["rss", "search"]},
        acceptance_criteria_json={"items": ["Report includes top findings"]},
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={
            "persist_draft": True,
            "family_hint": "single_service",
            "allow_family_override": False,
        },
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["selected_blueprint_type"] == "single_service"
    assert payload["family_selection"]["recommended_family"] == "workflow"
    assert payload["family_selection"]["override_applied"] is True
    assert payload["decision_trace"]["template_selection"]["selected_family"] == "single_service"
    assert any("family_hint locked selection" in warning for warning in payload["warnings"])


def test_compile_force_new_draft_controls_reuse_behavior(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Weekly Research Digest",
        goal="Collect and summarize research every week.",
        input_description="Theme and sources.",
        output_description="A weekly report artifact.",
        domain="research",
        interactivity_level="medium",
        recurrence_mode="weekly",
        external_tools_needed_json={"items": ["rss", "search"]},
        acceptance_criteria_json={"items": ["Report includes top findings"]},
    )

    first_compile = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": True})
    assert first_compile.status_code == 200
    first_payload = first_compile.json()

    second_compile = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": True})
    assert second_compile.status_code == 200
    second_payload = second_compile.json()

    forced_compile = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True, "force_new_draft": True},
    )
    assert forced_compile.status_code == 200
    forced_payload = forced_compile.json()

    assert second_payload["draft_strategy"]["action"] == "reused_latest_compiler_draft"
    assert second_payload["blueprint_draft"]["id"] == first_payload["blueprint_draft"]["id"]
    assert forced_payload["draft_strategy"]["action"] == "created_new_compiler_draft"
    assert forced_payload["blueprint_draft"]["id"] != first_payload["blueprint_draft"]["id"]


def test_compile_default_mode_remains_deterministic(client: TestClient):
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Multi-Agent Review Copilot",
        goal="Coordinate planner, specialist, and reviewer roles.",
        input_description="Complex task packet.",
        output_description="Reviewed final output.",
        domain="multi-agent analysis",
        interactivity_level="collaborative",
        external_tools_needed_json={"items": ["search", "filesystem"]},
        acceptance_criteria_json={"items": ["Each role contributes"]},
        human_review_required=True,
    )

    compile_response = client.post(f"/api/v1/requirements/{requirement['id']}/compile", json={"persist_draft": True})
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["compiler_mode"] == "deterministic"
    assert payload["selected_blueprint_type"] == "multi_agent"
    assert payload["decision_trace"]["skeleton"]["factory_key"] == "deterministic.multi_agent.v1"


def test_llm_assisted_compile_uses_valid_analysis_family_and_slot_fills(client: TestClient, monkeypatch):
    install_fake_compiler_provider(
        monkeypatch,
        responses=[
            """
            {
              "domain": "research",
              "complexity": "medium",
              "expected_artifacts": ["workflow_report.json", "brief.md"],
              "interactivity_level": "medium",
              "requires_external_tools": true,
              "requires_human_review": false,
              "recurrence_mode": "weekly",
              "knowledge_scope": "narrow",
              "recommended_family": "workflow",
              "confidence": 86,
              "unresolved_questions": ["Should the report include source citations?"]
            }
            """,
            """
            {
              "recommended_family": "workflow",
              "confidence": 84,
              "rationale": "The requirement asks for staged collection, synthesis, and publishing."
            }
            """,
            """
            {
              "status": "planned",
              "confidence": 83,
              "proposed_steps": [
                {
                  "step_id": "collect",
                  "order": 1,
                  "logical_step_kind": "ingest",
                  "purpose": "Collect source material for the weekly briefing.",
                  "input_intent": "Theme and configured source list.",
                  "output_intent": "Normalized source bundle.",
                  "required_capabilities": ["fetch_url"],
                  "optional_capabilities": ["rss_fetch"],
                  "artifact_expectations": ["workflow_report.json"]
                },
                {
                  "step_id": "summarize",
                  "order": 2,
                  "logical_step_kind": "summarize",
                  "purpose": "Synthesize the collected material into a concise briefing.",
                  "input_intent": "Normalized research bundle.",
                  "output_intent": "Draft research synthesis.",
                  "required_capabilities": ["summarize_text_llm"]
                },
                {
                  "step_id": "persist",
                  "order": 3,
                  "logical_step_kind": "persist",
                  "purpose": "Write the final package into durable workflow artifacts.",
                  "input_intent": "Final research package.",
                  "output_intent": "Persisted report artifacts.",
                  "required_capabilities": ["write_artifact"]
                }
              ]
            }
            """,
            """
            {
              "blueprint_description": "Weekly research workflow with controlled collection, synthesis, and publishing.",
              "input_schema_description": "Theme, source list, and publishing window.",
              "output_schema_description": "Compiled research package with markdown and JSON outputs.",
              "step_fills": [
                {"step_name": "collect_inputs", "description": "Gather source material and normalize the input bundle.", "purpose": "Prepare sources for downstream processing."},
                {"step_name": "filter_and_process", "description": "Filter sources and synthesize the report.", "prompt_block_draft": "Summarize the most relevant items and extract supporting evidence."},
                {"step_name": "publish_outputs", "description": "Write the final workflow artifacts.", "warnings": ["Requires artifact review before distribution."]}
              ],
              "artifact_fills": [
                {"artifact_name": "workflow_report.json", "intent": "Structured research output", "description": "Machine-readable report payload."}
              ],
              "variable_fills": [
                {"name": "domain", "hint": "research"}
              ],
              "assumptions": ["Source feeds are available at compile time."],
              "warnings": ["LLM content still requires operator review."]
            }
            """,
        ],
    )
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Weekly Package Service",
        goal="Normalize weekly status items and publish a weekly package.",
        input_description="Status items and editorial constraints.",
        output_description="Markdown status digest plus JSON workflow package.",
        domain="operations",
        acceptance_criteria_json={"items": ["Research package is complete"]},
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True, "compiler_mode": "llm_assisted"},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["compiler_mode"] == "llm_assisted"
    assert payload["llm_assistance_used"] is True
    assert payload["llm_analysis_summary"]["domain"] == "research"
    assert payload["family_selection"]["deterministic_recommended_family"] == "single_service"
    assert payload["family_selection"]["llm_recommended_family"] == "workflow"
    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["slot_fill_summary"]["used"] is True
    assert payload["decision_trace"]["step_planning"]["used"] is True
    assert payload["decision_trace"]["step_planning"]["planned_steps"][0]["logical_step_kind"] == "ingest"
    assert payload["explainability"]["step_planning"]["referenced_capabilities"] == [
        "fetch_url",
        "summarize_text_llm",
        "write_artifact",
    ]
    analysis_trace = next(item for item in payload["decision_trace"]["llm_stage_traces"] if item["stage"] == "requirement_analysis")
    assert analysis_trace["prompt_contract_id"] == "compiler.requirement_analysis.v2_6_0"
    step_planning_trace = next(item for item in payload["decision_trace"]["llm_stage_traces"] if item["stage"] == "step_planning")
    assert step_planning_trace["prompt_contract_id"] == "compiler.step_planning.v4_4"
    assert "execution_steps_json.steps.collect_inputs.description" in payload["slot_fill_summary"]["accepted_fields"]
    assert payload["blueprint_draft"]["execution_nodes_json"]["nodes"][0]["name"] == "collector"
    assert payload["blueprint_draft"]["execution_steps_json"]["steps"][0]["description"] == "Gather source material and normalize the input bundle."
    assert payload["blueprint_draft"]["execution_steps_json"]["steps"][0]["logical_step_kind"] == "ingest"
    assert payload["blueprint_draft"]["execution_steps_json"]["steps"][1]["required_capabilities"] == ["summarize_text_llm"]
    assert payload["blueprint_draft"]["runtime_constraints_json"]["compiler_assumptions"] == ["Source feeds are available at compile time."]
    assert payload["decision_trace"]["reliability_metrics"]["analyzer"]["successes"] == 1
    assert payload["decision_trace"]["reliability_metrics"]["recommender"]["successes"] == 1
    assert payload["decision_trace"]["reliability_metrics"]["filler"]["successes"] == 2
    assert payload["explainability"]["blueprint"]["slot_fill_applied"] is True
    assert payload["explainability"]["step_planning"]["used"] is True
    assert payload["explainability"]["step_planning"]["missing_capabilities"] == []
    assert "slot_fill" in payload["explainability"]["blueprint"]["descriptive_sources"]
    assert "step_plan" in payload["explainability"]["blueprint"]["descriptive_sources"]
    assert payload["explainability"]["readiness"]["completion_state"] in {"ready_to_compile", "compilable_with_warnings"}
    assert payload["explainability"]["warnings"]["warning_count"] >= 0
    assert any(section["section_key"] == "step_planning" for section in payload["explainability"]["presentation"]["sections"])
    assert any(section["section_key"] == "next_actions" for section in payload["explainability"]["presentation"]["sections"])
    assert any(
        item["section"] == "step_descriptions_and_purpose" and item["source"] == "slot_fill"
        for item in payload["explainability"]["blueprint"]["section_provenance"]
    )


def test_llm_assisted_filler_receives_template_hint_without_changing_structure(client: TestClient, monkeypatch):
    captured_slot_fill_prompt: dict[str, str] = {}
    responses = [
        """
        {
          "domain": "research",
          "complexity": "medium",
          "expected_artifacts": ["workflow_report.json"],
          "interactivity_level": "medium",
          "requires_external_tools": true,
          "requires_human_review": false,
          "recurrence_mode": "weekly",
          "knowledge_scope": "narrow",
          "recommended_family": "workflow",
          "confidence": 80,
          "unresolved_questions": []
        }
        """,
        """
        {
          "recommended_family": "workflow",
          "confidence": 82,
          "rationale": "The requirement is a staged recurring research workflow."
        }
        """,
        """
        {
          "status": "planned",
          "confidence": 79,
          "proposed_steps": [
            {
              "step_id": "collect",
              "order": 1,
              "logical_step_kind": "ingest",
              "purpose": "Collect template-aligned research inputs.",
              "required_capabilities": ["fetch_url"]
            }
          ]
        }
        """,
        """
        {
          "blueprint_description": "Research workflow with template-aware slot filling.",
          "step_fills": [
            {"step_name": "collect_inputs", "description": "Collect template-aligned research inputs."}
          ],
          "artifact_fills": [],
          "variable_fills": [],
          "assumptions": [],
          "warnings": []
        }
        """,
    ]

    class FakeAdapter:
        def chat(self, messages):  # noqa: ANN001
            if not responses:
                raise AssertionError(f"Unexpected provider call: {messages}")
            if len(responses) == 1:
                captured_slot_fill_prompt["content"] = messages[1]["content"]
            content = responses.pop(0)
            return ProviderResponse(
                content=content,
                prompt_tokens=32,
                completion_tokens=48,
                total_tokens=80,
                raw_metadata={"mode": "real", "source": "test"},
            )

    def fake_get_provider_adapter(model_config=None, overrides=None):  # noqa: ANN001, ARG001
        return FakeAdapter(), ResolvedModelConfig(
            provider="openai-compatible",
            model_name="test-model",
            endpoint="https://example.test/v1",
            api_key="test-key",
            temperature=0.2,
            max_tokens=512,
            source="test",
            llm_mode="real",
        )

    monkeypatch.setattr(requirement_compiler_service.settings, "llm_mode", "real")
    monkeypatch.setattr(requirement_compiler_service, "get_provider_adapter", fake_get_provider_adapter)
    monkeypatch.setattr(compiler_llm_assisted, "get_provider_adapter", fake_get_provider_adapter)

    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Weekly Package Pipeline",
        goal="Normalize weekly updates every week.",
        input_description="Update list and editorial constraints.",
        output_description="A weekly workflow package artifact.",
        domain="operations",
        interactivity_level="medium",
        recurrence_mode="weekly",
        external_tools_needed_json={"items": ["rss", "search"]},
        acceptance_criteria_json={"items": ["Report includes top findings"]},
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True, "compiler_mode": "llm_assisted"},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert '"step_plan_summary"' in captured_slot_fill_prompt["content"]
    assert payload["decision_trace"]["template_selection"]["selected_template_id"] == "workflow.monitoring_digest"
    assert [step["step_name"] for step in payload["blueprint_draft"]["execution_steps_json"]["steps"]] == [
        "collect_inputs",
        "filter_and_process",
        "publish_outputs",
    ]
    assert payload["blueprint_draft"]["execution_nodes_json"]["nodes"][0]["name"] == "collector"


def test_step_planning_rejects_unregistered_step_kinds_and_unsupported_capabilities_safely(
    client: TestClient,
    monkeypatch,
):
    install_fake_compiler_provider(
        monkeypatch,
        responses=[
            """
            {
              "domain": "research",
              "complexity": "medium",
              "expected_artifacts": ["workflow_report.json"],
              "interactivity_level": "medium",
              "requires_external_tools": true,
              "requires_human_review": false,
              "recurrence_mode": "weekly",
              "knowledge_scope": "narrow",
              "recommended_family": "workflow",
              "confidence": 80,
              "unresolved_questions": []
            }
            """,
            """
            {
              "recommended_family": "workflow",
              "confidence": 82,
              "rationale": "The requirement is a staged recurring research workflow."
            }
            """,
            """
            {
              "status": "planned",
              "confidence": 75,
              "proposed_steps": [
                {
                  "step_id": "bad_kind",
                  "order": 1,
                  "logical_step_kind": "invented_kind",
                  "purpose": "Illegal step kind.",
                  "required_capabilities": ["fetch_url"]
                },
                {
                  "step_id": "bad_capability",
                  "order": 2,
                  "logical_step_kind": "summarize",
                  "purpose": "Summarize collected research.",
                  "required_capabilities": ["made_up_capability"]
                }
              ]
            }
            """,
            "{}",
        ],
    )
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Package Planner",
        goal="Normalize weekly updates every week.",
        input_description="Update list and workflow constraints.",
        output_description="Weekly workflow package artifacts.",
        domain="operations",
        acceptance_criteria_json={"items": ["Research package is complete"]},
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True, "compiler_mode": "llm_assisted"},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["decision_trace"]["step_planning"]["used"] is True
    assert payload["decision_trace"]["step_planning"]["planned_steps"][0]["logical_step_kind"] == "summarize"
    assert payload["decision_trace"]["step_planning"]["unsupported_capability_mentions"] == ["made_up_capability"]
    assert any("invented_kind" in warning for warning in payload["decision_trace"]["step_planning"]["warnings"])
    assert payload["explainability"]["step_planning"]["capability_gaps"][0]["gap_kind"] == "unsupported_mention"
    assert payload["explainability"]["capability_gap_summary"]["total_count"] >= 1
    assert any(section["section_key"] == "capability_gaps" for section in payload["explainability"]["presentation"]["sections"])
    assert payload["blueprint_draft"]["execution_steps_json"]["steps"][1]["logical_step_kind"] == "summarize"
    assert "required_capabilities" not in payload["blueprint_draft"]["execution_steps_json"]["steps"][1]


def test_step_planning_surfaces_missing_capabilities_without_corrupting_blueprint(
    client: TestClient,
    monkeypatch,
):
    real_registry_cls = compiler_llm_assisted.CapabilityRegistryService

    class FakeCapabilityRegistryService:
        def list_step_kinds(self):
            return real_registry_cls().list_step_kinds()

        def snapshot(self, *, context=None, include_disabled=False):
            return real_registry_cls().snapshot(
                context=context,
                include_disabled=include_disabled,
            )

        def validate_capability_usage(self, *, capability_id, context, step_kind=None):
            return CapabilityUsageValidationResult(
                valid=False,
                capability_id=capability_id,
                context=context,
                step_kind=step_kind,
                errors=["Capability intentionally blocked for test."],
            )

        def detect_capability_gap(self, *, step_kind, context):
            return CapabilityGap(
                step_kind=step_kind,
                context=context,
                reason="no_capability_allowed_in_context",
                allowed_capability_types=["llm_assisted"],
                candidate_capability_ids=["summarize_text_llm"],
                blocked_capability_ids=["summarize_text_llm"],
                notes=["Test-injected capability gap."],
            )

    monkeypatch.setattr(compiler_llm_assisted, "CapabilityRegistryService", FakeCapabilityRegistryService)
    install_fake_compiler_provider(
        monkeypatch,
        responses=[
            """
            {
              "domain": "research",
              "complexity": "medium",
              "expected_artifacts": ["workflow_report.json"],
              "interactivity_level": "medium",
              "requires_external_tools": true,
              "requires_human_review": false,
              "recurrence_mode": "weekly",
              "knowledge_scope": "narrow",
              "recommended_family": "workflow",
              "confidence": 80,
              "unresolved_questions": []
            }
            """,
            """
            {
              "recommended_family": "workflow",
              "confidence": 82,
              "rationale": "The requirement is a staged recurring research workflow."
            }
            """,
            """
            {
              "status": "planned",
              "confidence": 74,
              "proposed_steps": [
                {
                  "step_id": "summarize",
                  "order": 2,
                  "logical_step_kind": "summarize",
                  "purpose": "Synthesize the collected research.",
                  "required_capabilities": ["summarize_text_llm"]
                }
              ]
            }
            """,
            "{}",
        ],
    )
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Package Gap Probe",
        goal="Normalize weekly updates every week.",
        input_description="Update list and workflow constraints.",
        output_description="Weekly workflow package artifacts.",
        domain="operations",
        acceptance_criteria_json={"items": ["Research package is complete"]},
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True, "compiler_mode": "llm_assisted"},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["explainability"]["step_planning"]["missing_capabilities"][0]["reason"] == "no_capability_allowed_in_context"
    assert payload["explainability"]["step_planning"]["capability_gaps"][0]["gap_kind"] == "missing_capability"
    assert payload["explainability"]["step_planning"]["capability_gap_summary"]["with_request_suggestion_count"] >= 1
    assert payload["explainability"]["capability_resolution"]["operator"]["linked_request_count"] >= 1
    assert payload["explainability"]["step_planning"]["capability_resolution"]["end_user"]["resolution_state"] in {
        "submitted_for_platform_resolution",
        "blocked_requires_external_setup",
    }
    assert payload["decision_trace"]["step_planning"]["unsupported_capability_mentions"] == ["summarize_text_llm"]
    assert any(section["section_key"] == "step_planning" for section in payload["explainability"]["presentation"]["sections"])
    assert any(section["section_key"] == "capability_resolution" for section in payload["explainability"]["presentation"]["sections"])
    assert payload["selected_blueprint_type"] == "workflow"
    assert payload["blueprint_draft"]["execution_steps_json"]["steps"][1]["logical_step_kind"] == "summarize"


def test_llm_assisted_invalid_analysis_response_falls_back_cleanly(client: TestClient, monkeypatch):
    install_fake_compiler_provider(
        monkeypatch,
        responses=[
            '{"domain": "research", "recommended_family": "not-a-family"}',
            '{"still_invalid": true}',
            '{"recommended_family": "single_service", "confidence": 15, "rationale": "Default fallback."}',
            '{}',
            '{}',
        ],
    )
    project = create_project(client)
    requirement = create_requirement(
        client,
        project["id"],
        title="Support Reply Service",
        goal="Handle a single incoming task.",
        input_description="One task payload.",
        output_description="One response payload.",
    )

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True, "compiler_mode": "llm_assisted"},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["selected_blueprint_type"] == "single_service"
    assert payload["llm_analysis_summary"] is None
    assert any(event["stage"] == "requirement_analysis" for event in payload["fallback_events"])
    assert all("Repair output still failed schema validation." not in warning for warning in payload["warnings"])
    analysis_trace = next(item for item in payload["decision_trace"]["llm_stage_traces"] if item["stage"] == "requirement_analysis")
    assert analysis_trace["repair_attempted"] is True
    assert analysis_trace["repair_succeeded"] is False
    assert analysis_trace["fallback_reason"] == "repair_invalid"
    assert payload["decision_trace"]["reliability_metrics"]["analyzer"]["fallback_count"] == 1


def test_llm_assisted_invalid_output_can_repair_and_records_validation_outcomes(client: TestClient, monkeypatch):
    install_fake_compiler_provider(
        monkeypatch,
        responses=[
            "not valid json",
            """
            {
              "domain": "ops",
              "complexity": "low",
              "expected_artifacts": ["service_output.json"],
              "interactivity_level": "low",
              "requires_external_tools": false,
              "requires_human_review": false,
              "recurrence_mode": "once",
              "knowledge_scope": "bounded",
              "recommended_family": "single_service",
              "confidence": 60,
              "unresolved_questions": []
            }
            """,
            '{"recommended_family": "single_service", "confidence": 40, "rationale": "Single bounded interaction."}',
            '{}',
            '{}',
        ],
    )
    project = create_project(client)
    requirement = create_requirement(client, project["id"], title="Ops Command Helper")

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True, "compiler_mode": "llm_assisted"},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    analysis_outcomes = [item for item in payload["validation_outcomes"] if item["stage"] == "requirement_analysis"]
    assert any(item["status"] == "rejected" for item in analysis_outcomes)
    assert any(item["status"] == "repaired" for item in analysis_outcomes)
    assert payload["llm_analysis_summary"]["domain"] == "ops"
    assert payload["selected_blueprint_type"] == "single_service"
    analysis_trace = next(item for item in payload["decision_trace"]["llm_stage_traces"] if item["stage"] == "requirement_analysis")
    assert analysis_trace["extraction_success"] is False
    assert analysis_trace["repair_attempted"] is True
    assert analysis_trace["repair_succeeded"] is True
    assert payload["decision_trace"]["reliability_metrics"]["repair_successes"] >= 1


def test_llm_assisted_structured_uncertainty_returns_machine_readable_status(client: TestClient, monkeypatch):
    install_fake_compiler_provider(
        monkeypatch,
        responses=[
            """
            {
              "status": "needs_information",
              "domain": null,
              "complexity": "medium",
              "expected_artifacts": [],
              "interactivity_level": null,
              "requires_external_tools": false,
              "requires_human_review": false,
              "recurrence_mode": null,
              "knowledge_scope": "unknown",
              "recommended_family": null,
              "confidence": 0,
              "unresolved_questions": ["What business domain should the compiler target?"],
              "missing_information": ["domain", "artifact examples"],
              "uncertainty_reasons": ["The requirement does not specify target domain or expected artifact examples."]
            }
            """,
            """
            {
              "status": "needs_information",
              "recommended_family": null,
              "confidence": 0,
              "rationale": "The available requirement details are not sufficient to promote beyond deterministic fallback.",
              "missing_information": ["runtime cadence", "coordination scope"],
              "uncertainty_reasons": ["No explicit coordination or recurrence details were supplied."]
            }
            """,
            """
            {
              "status": "needs_information",
              "proposed_steps": [],
              "warnings": [],
              "ambiguities": [],
              "unresolved_points": ["semantic step shaping is underspecified"],
              "confidence": 0
            }
            """,
            """
            {
              "status": "needs_information",
              "step_fills": [],
              "artifact_fills": [],
              "variable_fills": [],
              "assumptions": [],
              "warnings": [],
              "missing_information": ["artifact tone", "review policy"],
              "uncertainty_reasons": ["The deterministic skeleton exists, but descriptive slot content is underspecified."]
            }
            """,
        ],
    )
    project = create_project(client)
    requirement = create_requirement(client, project["id"], title="Ambiguous Helper")

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True, "compiler_mode": "llm_assisted"},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["selected_blueprint_type"] == "single_service"
    assert payload["llm_analysis_summary"]["status"] == "needs_information"
    assert payload["llm_analysis_summary"]["missing_information"] == ["domain", "artifact examples"]
    assert payload["family_selection"]["llm_status"] == "needs_information"
    assert payload["family_selection"]["llm_recommended_family"] is None
    assert payload["slot_fill_summary"]["llm_status"] == "needs_information"
    assert payload["slot_fill_summary"]["missing_information"] == ["artifact tone", "review policy"]
    assert "Missing information: domain" in payload["open_questions"]
    assert payload["decision_trace"]["reliability_metrics"]["fallback_count"] == 0


def test_llm_family_recommendation_rejects_illegal_family_and_falls_back_to_deterministic(client: TestClient, monkeypatch):
    install_fake_compiler_provider(
        monkeypatch,
        responses=[
            """
            {
              "domain": "ops",
              "complexity": "low",
              "expected_artifacts": ["service_output.json"],
              "interactivity_level": "low",
              "requires_external_tools": false,
              "requires_human_review": false,
              "recurrence_mode": "once",
              "knowledge_scope": "bounded",
              "recommended_family": "single_service",
              "confidence": 60,
              "unresolved_questions": []
            }
            """,
            '{"status": "recommended", "recommended_family": "orchestra", "confidence": 91, "rationale": "Illegal family."}',
            '{"status": "recommended", "recommended_family": "still_illegal", "confidence": 91, "rationale": "Still illegal."}',
            '{}',
            '{}',
        ],
    )
    project = create_project(client)
    requirement = create_requirement(client, project["id"], title="Illegal Family Probe")

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True, "compiler_mode": "llm_assisted"},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["selected_blueprint_type"] == "single_service"
    assert payload["family_selection"]["llm_recommended_family"] is None
    assert any(
        event["stage"] == "family_recommendation" and event["reason"] == "repair_invalid"
        for event in payload["fallback_events"]
    )
    family_trace = next(item for item in payload["decision_trace"]["llm_stage_traces"] if item["stage"] == "family_recommendation")
    assert family_trace["repair_attempted"] is True
    assert family_trace["fallback_reason"] == "repair_invalid"
    assert payload["decision_trace"]["reliability_metrics"]["recommender"]["fallback_count"] == 1


def test_llm_assisted_missing_provider_config_is_safe(client: TestClient, monkeypatch):
    install_fake_compiler_provider(monkeypatch, responses=[], endpoint="")
    project = create_project(client)
    requirement = create_requirement(client, project["id"], title="Safe Fallback Service")

    compile_response = client.post(
        f"/api/v1/requirements/{requirement['id']}/compile",
        json={"persist_draft": True, "compiler_mode": "llm_assisted"},
    )
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["compiler_mode"] == "llm_assisted"
    assert payload["llm_assistance_used"] is False
    assert payload["selected_blueprint_type"] == "single_service"
    assert any("no provider endpoint is configured" in warning.lower() for warning in payload["warnings"])
    assert all(event["reason"] == "llm_unavailable" for event in payload["fallback_events"])
