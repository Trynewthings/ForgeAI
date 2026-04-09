from __future__ import annotations

from app.core.database import get_db
from app.db.models import Requirement
from app.main import app
from tests.test_requirement_compiler_v251 import canonical_acceptance_criteria, canonical_constraints, create_project


def test_requirement_create_accepts_strict_constraints_and_acceptance_payloads(client):
    project = create_project(client)
    response = client.post(
        "/api/v1/requirements",
        json={
            "project_id": project["id"],
            "title": "Strict Payload Requirement",
            "goal": "Respect explicit requirement payload schemas.",
            "input_description": "One structured request.",
            "output_description": "One structured response.",
            "constraints_json": canonical_constraints(
                [
                    {
                        "id": "constraint_latency",
                        "type": "performance",
                        "level": "hard",
                        "rule": "The end-to-end latency must stay below target.",
                        "target": "runtime",
                        "value": {"p95_ms": 1500},
                    }
                ],
                summary="Delivery constraints",
            ),
            "acceptance_criteria_json": canonical_acceptance_criteria(
                [
                    {
                        "id": "criterion_accuracy",
                        "type": "quality",
                        "level": "hard",
                        "description": "The response references the request context.",
                        "measurement": "manual_review",
                        "threshold": {"pass_rate": 1.0},
                    }
                ],
                summary="Release acceptance",
            ),
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["constraints_json"]["version"] == "1.0"
    assert payload["constraints_json"]["summary"] == "Delivery constraints"
    assert payload["constraints_json"]["items"][0]["type"] == "performance"
    assert payload["acceptance_criteria_json"]["version"] == "1.0"
    assert payload["acceptance_criteria_json"]["items"][0]["measurement"] == "manual_review"


def test_requirement_create_rejects_top_level_array_for_strict_payloads(client):
    project = create_project(client)
    response = client.post(
        "/api/v1/requirements",
        json={
            "project_id": project["id"],
            "title": "Invalid Array Requirement",
            "constraints_json": ["must be secure"],
            "acceptance_criteria_json": canonical_acceptance_criteria(),
        },
    )

    assert response.status_code == 422


def test_requirement_create_rejects_missing_required_strict_fields(client):
    project = create_project(client)
    response = client.post(
        "/api/v1/requirements",
        json={
            "project_id": project["id"],
            "title": "Missing Shape Requirement",
            "constraints_json": {"items": []},
            "acceptance_criteria_json": canonical_acceptance_criteria(),
        },
    )

    assert response.status_code == 422


def test_requirement_create_normalizes_loose_authoring_fields(client):
    project = create_project(client)
    response = client.post(
        "/api/v1/requirements",
        json={
            "project_id": project["id"],
            "title": "  Normalized Requirement  ",
            "summary": "   ",
            "goal": "Ship a stable result.",
            "domain": "   ",
            "interactivity_level": " HIGH ",
            "recurrence_mode": " Weekly ",
            "external_tools_needed_json": [" Slack ", "", "Notion", "slack"],
            "acceptance_criteria_json": canonical_acceptance_criteria(),
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["title"] == "Normalized Requirement"
    assert payload["summary"] is None
    assert payload["domain"] is None
    assert payload["interactivity_level"] == "high"
    assert payload["recurrence_mode"] == "weekly"
    assert payload["external_tools_needed_json"] == {"items": ["Slack", "Notion"]}


def test_requirement_patch_normalizes_blank_optional_fields(client):
    project = create_project(client)
    create_response = client.post(
        "/api/v1/requirements",
        json={
            "project_id": project["id"],
            "title": "Patch Normalization Requirement",
            "goal": "Keep authoring inputs normalized.",
            "input_description": "A request payload.",
            "output_description": "A response payload.",
            "domain": "Support",
            "interactivity_level": "Medium",
            "recurrence_mode": "Daily",
            "external_tools_needed_json": {"items": ["Email"]},
            "acceptance_criteria_json": canonical_acceptance_criteria(),
        },
    )
    requirement_id = create_response.json()["id"]

    patch_response = client.patch(
        f"/api/v1/requirements/{requirement_id}",
        json={
            "domain": "  ",
            "interactivity_level": " collaborative ",
            "recurrence_mode": "",
            "external_tools_needed_json": {"slack": True, "notion": True},
        },
    )

    assert patch_response.status_code == 200
    payload = patch_response.json()
    assert payload["domain"] is None
    assert payload["interactivity_level"] == "collaborative"
    assert payload["recurrence_mode"] is None
    assert payload["external_tools_needed_json"] == {"items": ["slack", "notion"]}


def test_requirement_create_rejects_invalid_enum_values(client):
    project = create_project(client)
    response = client.post(
        "/api/v1/requirements",
        json={
            "project_id": project["id"],
            "title": "Invalid Enum Requirement",
            "constraints_json": canonical_constraints(
                [
                    {
                        "id": "constraint_1",
                        "type": "invalid_type",
                        "level": "hard",
                        "rule": "Broken enum",
                        "target": None,
                        "value": None,
                    }
                ]
            ),
            "acceptance_criteria_json": canonical_acceptance_criteria(),
        },
    )

    assert response.status_code == 422


def test_requirement_read_normalizes_legacy_payloads_without_dropping_data(client):
    project = create_project(client)
    create_response = client.post(
        "/api/v1/requirements",
        json={
            "project_id": project["id"],
            "title": "Legacy Payload Requirement",
            "acceptance_criteria_json": canonical_acceptance_criteria(),
        },
    )
    requirement_id = create_response.json()["id"]

    override = app.dependency_overrides[get_db]
    db = next(override())
    try:
        requirement = db.get(Requirement, requirement_id)
        requirement.constraints_json = ["no outbound network", {"rule": "Use internal APIs first", "level": "soft"}]
        requirement.acceptance_criteria_json = {"items": ["Output is complete"], "summary": "legacy summary"}
        db.commit()
    finally:
        db.close()

    response = client.get(f"/api/v1/requirements/{requirement_id}")
    assert response.status_code == 200
    payload = response.json()

    assert payload["constraints_json"]["version"] == "1.0"
    assert payload["constraints_json"]["items"][0]["type"] == "legacy"
    assert payload["constraints_json"]["items"][0]["rule"] == "no outbound network"
    assert payload["acceptance_criteria_json"]["version"] == "1.0"
    assert payload["acceptance_criteria_json"]["summary"] == "legacy summary"
    assert payload["acceptance_criteria_json"]["items"][0]["description"] == "Output is complete"


def test_compiler_consumes_canonical_acceptance_payload_shape(client):
    project = create_project(client)
    requirement_response = client.post(
        "/api/v1/requirements",
        json={
            "project_id": project["id"],
            "title": "Compiler Payload Requirement",
            "goal": "Compile a strict payload requirement.",
            "input_description": "One request.",
            "output_description": "One response.",
            "domain": "support",
            "acceptance_criteria_json": canonical_acceptance_criteria(["Response is acceptable"]),
        },
    )
    requirement_id = requirement_response.json()["id"]

    compile_response = client.post(f"/api/v1/requirements/{requirement_id}/compile", json={"persist_draft": False})
    assert compile_response.status_code == 200
    payload = compile_response.json()

    assert payload["decision_trace"]["normalized_requirement_summary"]["acceptance_criteria_present"] is True
    assert payload["normalized_requirement"]["acceptance_criteria"]["version"] == "1.0"
